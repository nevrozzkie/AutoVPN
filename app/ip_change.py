from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, TypeVar

from app.aeza import AezaClient
from app.config import settings
from app.db import get_db, now_iso, set_setting, update_operation
from app.deep_protocol_checks import DeepCheckResult, run_deep_protocol_checks
from app.eu_install import build_eu_config_apply_script, run_remote_command
from app.health import tcp_check
from app.operation_coordinator import TERMINAL_STATUSES
from app.runtime_config import (
    aeza_api_base,
    aeza_ipv4_after_purchase_delay_seconds,
    aeza_ipv4_domain,
    aeza_ipv4_payment_method,
    aeza_service_id,
    aeza_token,
    eu_ssh_port,
    eu_ssh_password,
    server_command_timeout_seconds,
    server_ssh_probe_timeout_seconds,
    server_status_timeout_seconds,
)
from app.secret_sanitization import sanitize_error
from app.vpn_config import CapturedVpnConfig
from app.vpn_state import load_ip_change_snapshot, publish_ip_change


class IpChangeError(RuntimeError):
    pass


@dataclass(frozen=True)
class IpRotationTiming:
    provider_timeout: float
    after_purchase_delay: float
    ip_appear_timeout: float
    ip_appear_interval: float
    ssh_timeout: float
    ssh_interval: float
    ssh_probe_timeout: float
    command_timeout: float

    @classmethod
    def configured(cls) -> IpRotationTiming:
        return cls(
            provider_timeout=float(server_status_timeout_seconds()),
            after_purchase_delay=float(aeza_ipv4_after_purchase_delay_seconds()),
            ip_appear_timeout=float(settings.ip_appear_timeout_seconds),
            ip_appear_interval=float(settings.ip_appear_interval_seconds),
            ssh_timeout=float(settings.healthcheck_timeout_seconds),
            ssh_interval=float(settings.healthcheck_interval_seconds),
            ssh_probe_timeout=float(server_ssh_probe_timeout_seconds()),
            command_timeout=float(server_command_timeout_seconds()),
        )


T = TypeVar("T")
Sleeper = Callable[[float], Awaitable[None]]


def _clean(value: object, limit: int = 1000) -> str:
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or "")).strip()[:limit]


def _safe_error(exc: BaseException) -> str:
    return sanitize_error(
        _clean(exc) or exc.__class__.__name__,
        aeza_token().strip(),
        eu_ssh_password(),
        limit=1000,
    )


def _find_main_ip(ipv4_list: list[dict[str, Any]], current_ip: str) -> dict[str, Any]:
    for item in ipv4_list:
        if item.get("is_main"):
            if current_ip and item.get("ip") != current_ip:
                raise IpChangeError(
                    "Aeza main IPv4 does not match the published current_ip; "
                    "manual reconciliation is required"
                )
            return item
    for item in ipv4_list:
        if current_ip and item.get("ip") == current_ip:
            return item
    raise IpChangeError("Cannot find current main IPv4")


def _find_new_ip(
    ipv4_list: list[dict[str, Any]],
    old_ip_id: str,
    old_ip: str,
    created_ip_id: str,
    created_ip_address: str,
    known_ids: set[str],
    known_addresses: set[str],
) -> dict[str, Any] | None:
    if created_ip_id:
        return next(
            (item for item in ipv4_list if item.get("id") == created_ip_id),
            None,
        )
    if created_ip_address and created_ip_address not in known_addresses:
        return next(
            (item for item in ipv4_list if item.get("ip") == created_ip_address),
            None,
        )
    candidates = [
        item
        for item in ipv4_list
        if item.get("id") != old_ip_id
        and item.get("ip") != old_ip
        and str(item.get("id") or "") not in known_ids
        and str(item.get("ip") or "") not in known_addresses
    ]
    return candidates[0] if len(candidates) == 1 else None


async def _bounded(awaitable: Awaitable[T], timeout: float) -> T:
    return await asyncio.wait_for(awaitable, timeout=max(0.01, timeout))


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _desired_revision() -> int:
    with get_db() as db:
        row = db.execute(
            "SELECT desired_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone()
    return int(row["desired_revision"])


def _ensure_bound_revision_current(revision: int) -> None:
    if _desired_revision() != revision:
        raise IpChangeError(
            "VPN configuration changed during IP rotation; the bound operation "
            "must be retried from the new desired revision"
        )


async def _step(operation_id: int, step: str, **fields: Any) -> None:
    update_operation(
        operation_id,
        status="RUNNING",
        current_step=step,
        **fields,
    )
    await asyncio.sleep(0)


async def _wait_for_new_ip(
    client: AezaClient,
    service_id: str,
    old_ip_id: str,
    old_ip: str,
    created_ip_id: str,
    created_ip_address: str,
    known_ids: set[str],
    known_addresses: set[str],
    timing: IpRotationTiming,
    sleeper: Sleeper,
) -> dict[str, Any]:
    attempts = max(1, int(timing.ip_appear_timeout / max(0.01, timing.ip_appear_interval)) + 1)
    for attempt in range(attempts):
        ipv4_list = await _bounded(
            client.get_ipv4_list(service_id), timing.provider_timeout
        )
        new_ip = _find_new_ip(
            ipv4_list,
            old_ip_id,
            old_ip,
            created_ip_id,
            created_ip_address,
            known_ids,
            known_addresses,
        )
        if new_ip and new_ip.get("id") and new_ip.get("ip"):
            return new_ip
        if attempt + 1 < attempts:
            await sleeper(timing.ip_appear_interval)
    raise IpChangeError("New IPv4 did not appear in Aeza list before timeout")


async def _wait_for_main_ip(
    client: AezaClient,
    service_id: str,
    expected_ip_id: str,
    expected_ip: str,
    timing: IpRotationTiming,
    sleeper: Sleeper,
) -> None:
    attempts = max(1, int(timing.ip_appear_timeout / max(0.01, timing.ip_appear_interval)) + 1)
    for attempt in range(attempts):
        ipv4_list = await _bounded(
            client.get_ipv4_list(service_id), timing.provider_timeout
        )
        confirmed = any(
            item.get("is_main")
            and item.get("id") == expected_ip_id
            and item.get("ip") == expected_ip
            for item in ipv4_list
        )
        if confirmed:
            return
        if attempt + 1 < attempts:
            await sleeper(timing.ip_appear_interval)
    raise IpChangeError("Aeza did not confirm the new IPv4 as main before timeout")


async def _wait_for_ssh(
    host: str,
    timing: IpRotationTiming,
    sleeper: Sleeper,
) -> None:
    attempts = max(1, int(timing.ssh_timeout / max(0.01, timing.ssh_interval)) + 1)
    for attempt in range(attempts):
        if await tcp_check(
            host,
            eu_ssh_port(),
            timeout=max(0.01, timing.ssh_probe_timeout),
        ):
            return
        if attempt + 1 < attempts:
            await sleeper(timing.ssh_interval)
    raise IpChangeError("SSH did not become reachable on the new IPv4 before timeout")


def _protocol_health_payload(
    results: dict[str, DeepCheckResult],
    *,
    amnezia_enabled: bool,
) -> str:
    payload = {
        key: "VERIFIED" if result.verified else "FAILED"
        for key, result in sorted(results.items())
        if key != "amnezia"
    }
    if amnezia_enabled:
        payload["amnezia"] = "NEW_ENDPOINT_NOT_VERIFIED"
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _verify_protocol_health(
    host: str,
    config: CapturedVpnConfig,
    timing: IpRotationTiming,
) -> str:
    results = await run_deep_protocol_checks(
        host,
        command_timeout=timing.command_timeout,
        config=config,
        target_host=host,
    )
    required: list[tuple[str, str]] = []
    if config.vless.protocol.enabled:
        required.append(("vless", "VLESS"))
    if config.hysteria.protocol.enabled:
        required.append(("hysteria_salamander", "Hysteria"))
    failed = [
        label
        for key, label in required
        if key not in results or not results[key].verified
    ]
    if failed:
        raise IpChangeError(
            "New-endpoint protocol verification failed: " + ", ".join(failed)
        )
    return _protocol_health_payload(
        results, amnezia_enabled=config.amnezia.protocol.enabled
    )


async def _apply_bound_snapshot(
    operation_id: int,
    host: str,
    config: CapturedVpnConfig,
    timing: IpRotationTiming,
) -> None:
    await _step(
        operation_id,
        "sync_vpn_after_ip_change",
        action_state="APPLYING",
        apply_state="RUNNING",
    )
    script = build_eu_config_apply_script(config)
    try:
        exit_code, _ = await _bounded(
            run_remote_command(
                host,
                "bash -s",
                stdin_data=script,
                timeout=timing.command_timeout,
            ),
            timing.command_timeout + 1,
        )
    except Exception:
        update_operation(
            operation_id,
            action_state="AMBIGUOUS",
            apply_state="AMBIGUOUS",
        )
        raise
    if exit_code != 0:
        update_operation(operation_id, action_state="APPLY_FAILED", apply_state="FAILED")
        raise IpChangeError(f"Transactional VPN apply failed with exit code {exit_code}")
    update_operation(
        operation_id,
        action_state="APPLIED",
        apply_state="APPLIED",
        apply_completed_at=now_iso(),
    )


async def _rollback_old_main(
    operation_id: int,
    client: AezaClient,
    service_id: str,
    old_ip_id: str,
    timing: IpRotationTiming,
) -> str:
    update_operation(
        operation_id,
        action_state="ROLLING_BACK",
        rollback_outcome="SENDING",
    )
    try:
        await _bounded(
            client.make_main_ipv4(service_id, old_ip_id), timing.provider_timeout
        )
    except Exception as exc:
        outcome = f"AMBIGUOUS: {_safe_error(exc)}"
        update_operation(
            operation_id,
            action_state="AMBIGUOUS",
            rollback_outcome=outcome,
        )
        return outcome
    update_operation(
        operation_id,
        action_state="ROLLED_BACK",
        rollback_outcome="RESTORED_OLD_MAIN",
    )
    return "RESTORED_OLD_MAIN"


async def run_ip_change(
    operation_id: int,
    *,
    timing: IpRotationTiming | None = None,
    sleeper: Sleeper = _sleep,
) -> None:
    timing = timing or IpRotationTiming.configured()
    operation, config = load_ip_change_snapshot(operation_id)
    if operation["status"] in TERMINAL_STATUSES:
        return
    if any(
        operation[field] != "NOT_STARTED"
        for field in ("purchase_state", "make_main_state", "apply_state")
    ):
        update_operation(
            operation_id,
            status="AMBIGUOUS",
            current_step="manual_verification_required",
            action_state="AMBIGUOUS",
            error_message=(
                "Interrupted IP rotation may already have changed Aeza or the VPS; "
                "automatic external actions were not resent"
            ),
        )
        return

    client = AezaClient(
        aeza_api_base(), aeza_token(), timeout=timing.provider_timeout
    )
    service_id = aeza_service_id()
    old_ip_id = ""
    old_ip = ""
    make_main_started = False

    try:
        if not service_id:
            raise IpChangeError("AEZA_SERVICE_ID is not configured")
        _ensure_bound_revision_current(config.revision)

        await _step(operation_id, "get_ipv4_list")
        ipv4_list = await _bounded(
            client.get_ipv4_list(service_id), timing.provider_timeout
        )
        known_ids = {str(item.get("id") or "") for item in ipv4_list}
        known_addresses = {str(item.get("ip") or "") for item in ipv4_list}

        await _step(operation_id, "find_current_main_ip")
        main_ip = _find_main_ip(ipv4_list, config.current_ip)
        old_ip = str(main_ip["ip"])
        old_ip_id = str(main_ip["id"])
        update_operation(operation_id, old_ip=old_ip, old_ip_id=old_ip_id)

        _ensure_bound_revision_current(config.revision)
        await _step(
            operation_id,
            "create_new_ipv4",
            action_state="PURCHASE_SENDING",
            purchase_state="SENDING",
        )
        try:
            created_ip = await _bounded(
                client.add_ipv4(
                    service_id,
                    payment_method=aeza_ipv4_payment_method(),
                    domain=aeza_ipv4_domain(),
                ),
                timing.provider_timeout,
            )
        except Exception:
            update_operation(
                operation_id,
                action_state="AMBIGUOUS",
                purchase_state="AMBIGUOUS",
            )
            raise
        created_ip_id = str(created_ip.get("id") or "")
        created_ip_address = str(created_ip.get("ip") or "")
        update_operation(
            operation_id,
            action_state="PURCHASE_SENT",
            purchase_state="SENT",
        )

        await _step(operation_id, "wait_after_ipv4_purchase")
        await sleeper(timing.after_purchase_delay)

        await _step(operation_id, "wait_new_ipv4")
        new_ip = await _wait_for_new_ip(
            client,
            service_id,
            old_ip_id,
            old_ip,
            created_ip_id,
            created_ip_address,
            known_ids,
            known_addresses,
            timing,
            sleeper,
        )
        new_ip_id = str(new_ip["id"])
        new_ip_address = str(new_ip["ip"])
        update_operation(
            operation_id,
            new_ip=new_ip_address,
            new_ip_id=new_ip_id,
            action_state="PURCHASE_CONFIRMED",
            purchase_state="CONFIRMED",
            new_ip_created_at=now_iso(),
        )

        _ensure_bound_revision_current(config.revision)
        make_main_started = True
        await _step(
            operation_id,
            "make_new_ipv4_main",
            action_state="MAKE_MAIN_SENDING",
            make_main_state="SENDING",
        )
        try:
            await _bounded(
                client.make_main_ipv4(service_id, new_ip_id),
                timing.provider_timeout,
            )
        except Exception:
            update_operation(
                operation_id,
                action_state="AMBIGUOUS",
                make_main_state="AMBIGUOUS",
            )
            raise
        update_operation(
            operation_id,
            action_state="MAIN_CHANGED",
            make_main_state="SENT",
            new_main_at=now_iso(),
        )

        await _step(operation_id, "confirm_new_ipv4_main")
        await _wait_for_main_ip(
            client,
            service_id,
            new_ip_id,
            new_ip_address,
            timing,
            sleeper,
        )
        update_operation(
            operation_id,
            action_state="MAIN_CONFIRMED",
            make_main_state="CONFIRMED",
        )

        await _step(operation_id, "wait_ssh")
        await _wait_for_ssh(new_ip_address, timing, sleeper)
        update_operation(
            operation_id,
            server_reachable_at=now_iso(),
        )

        _ensure_bound_revision_current(config.revision)
        await _apply_bound_snapshot(
            operation_id, new_ip_address, config, timing
        )

        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "protocol_health")
        healthcheck_result = await _verify_protocol_health(
            new_ip_address, config, timing
        )

        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "update_current_ip")
        published_revision = publish_ip_change(
            operation_id, new_ip_address, healthcheck_result
        )

        cleanup_warning = ""
        cleanup_ambiguous = False
        requires_manual_verification = config.amnezia.protocol.enabled
        if requires_manual_verification:
            cleanup_warning = (
                "Old Aeza IPv4 was preserved because AutoVPN has no real "
                "AmneziaWG probe through the new public endpoint; verify a client "
                "handshake manually before cleanup"
            )
            set_setting("last_healthcheck_status", "WARNING")
        else:
            try:
                await _step(
                    operation_id,
                    "delete_old_ipv4",
                    action_state="CLEANUP_SENDING",
                )
                await _bounded(
                    client.delete_ipv4(service_id, old_ip_id), timing.provider_timeout
                )
                update_operation(operation_id, action_state="CLEANUP_CONFIRMED")
            except Exception as exc:
                cleanup_ambiguous = True
                cleanup_warning = (
                    "New IPv4 was safely published, but the Aeza old-IPv4 DELETE "
                    "outcome is ambiguous; check Aeza before any further cleanup "
                    f"({old_ip_id}): {_safe_error(exc)}"
                )
                update_operation(
                    operation_id, action_state="CLEANUP_AMBIGUOUS"
                )

        update_operation(
            operation_id,
            status="DONE",
            current_step=(
                "manual_verification_required"
                if requires_manual_verification
                else "done"
            ),
            action_state=(
                "PUBLISHED_AWAITING_AWG_CHECK"
                if requires_manual_verification
                else "CLEANUP_AMBIGUOUS" if cleanup_ambiguous else "DONE"
            ),
            published_revision=published_revision,
            cleanup_warning=cleanup_warning,
            error_message="",
        )
    except Exception as exc:
        rollback_outcome = ""
        if make_main_started and old_ip_id:
            rollback_outcome = await _rollback_old_main(
                operation_id, client, service_id, old_ip_id, timing
            )
        with get_db() as db:
            current = db.execute(
                "SELECT action_state FROM ip_change_operations WHERE id = ?",
                (operation_id,),
            ).fetchone()
        ambiguous = bool(
            current
            and (
                current["action_state"] == "AMBIGUOUS"
                or rollback_outcome.startswith("AMBIGUOUS")
            )
        )
        update_operation(
            operation_id,
            status="AMBIGUOUS" if ambiguous else "FAILED",
            current_step=(
                "manual_verification_required" if ambiguous else "failed"
            ),
            action_state="AMBIGUOUS" if ambiguous else "FAILED",
            error_message=_safe_error(exc),
        )
        set_setting("last_healthcheck_status", "FAILED")
        raise


async def apply_manual_main_ip(new_ip: str) -> None:
    del new_ip
    raise IpChangeError(
        "Manual Aeza make-main is disabled; use the durable safe IP rotation workflow"
    )
