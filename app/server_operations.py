from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from app.aeza import AezaClient
from app.db import get_db, get_setting, now_iso
from app.eu_install import resolve_eu_host, run_remote_command
from app.health import tcp_check
from app.operation_coordinator import (
    TERMINAL_STATUSES,
    acquire_vps_lease,
    heartbeat_vps_lease,
    release_vps_lease,
)
from app.protocol_status import refresh_protocol_statuses
from app.runtime_config import (
    aeza_api_base,
    aeza_service_id,
    aeza_token,
    amnezia_enabled,
    eu_ssh_port,
    eu_ssh_password,
    hysteria_enabled,
    server_command_timeout_seconds,
    server_poll_interval_seconds,
    server_reboot_timeout_seconds,
    server_status_timeout_seconds,
    server_ssh_probe_timeout_seconds,
    vless_enabled,
)
from app.secret_sanitization import sanitize_error


SERVICE_STATUS_COMMAND = """for service in xray hysteria-server awg-quick@awg0; do
  if systemctl is-active --quiet "$service"; then
    printf '%s=active\\n' "$service"
  else
    printf '%s=inactive\\n' "$service"
  fi
done
"""

SERVER_FIELDS = {
    "status",
    "current_step",
    "action_state",
    "provider_status",
    "provider_ip",
    "ssh_status",
    "services_json",
    "protocol_health_json",
    "result_message",
    "warning_message",
    "error_message",
    "started_at",
    "completed_at",
}


@dataclass(frozen=True)
class ServerTiming:
    request_timeout: float
    reboot_timeout: float
    poll_interval: float
    ssh_probe_timeout: float
    command_timeout: float


def configured_server_timing() -> ServerTiming:
    return ServerTiming(
        request_timeout=float(server_status_timeout_seconds()),
        reboot_timeout=float(server_reboot_timeout_seconds()),
        poll_interval=float(server_poll_interval_seconds()),
        ssh_probe_timeout=float(server_ssh_probe_timeout_seconds()),
        command_timeout=float(server_command_timeout_seconds()),
    )


def _clean(value: object, limit: int = 4000) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))
    return " ".join(text.split())[:limit]


def _safe_error(value: object) -> str:
    return sanitize_error(value, aeza_token(), eu_ssh_password())


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def create_server_operation(kind: str) -> int:
    if kind not in {"STATUS", "REBOOT"}:
        raise ValueError(f"Unsupported server operation kind: {kind}")
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """
            INSERT INTO server_operations(
                kind, status, current_step, action_state, created_at, updated_at
            ) VALUES (?, 'PENDING', 'queued', 'NOT_STARTED', ?, ?)
            """,
            (kind, timestamp, timestamp),
        )
        operation_id = int(cursor.lastrowid)
        acquire_vps_lease(db, "SERVER", operation_id)
        return operation_id


def update_server_operation(operation_id: int, **fields: object) -> None:
    if not fields:
        return
    unknown = set(fields) - SERVER_FIELDS
    if unknown:
        raise ValueError(f"Unsupported server operation fields: {sorted(unknown)}")
    sanitized = {}
    for key, value in fields.items():
        if key in {"status", "action_state", "started_at", "completed_at"}:
            sanitized[key] = value
        elif key in {"error_message", "warning_message"}:
            sanitized[key] = _safe_error(value)
        else:
            sanitized[key] = _clean(value)
    sanitized["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in sanitized)
    values = [*sanitized.values(), operation_id]
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            f"UPDATE server_operations SET {assignments} WHERE id = ?",
            values,
        )
        if sanitized.get("status") in TERMINAL_STATUSES:
            release_vps_lease(db, "SERVER", operation_id)
        else:
            heartbeat_vps_lease(db, "SERVER", operation_id)


def mark_reboot_sending(operation_id: int) -> bool:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """
            UPDATE server_operations
            SET status = 'RUNNING', current_step = 'send_reboot',
                action_state = 'SENDING', started_at = COALESCE(started_at, ?),
                updated_at = ?
            WHERE id = ? AND kind = 'REBOOT'
              AND status IN ('PENDING', 'RUNNING') AND action_state = 'NOT_STARTED'
            """,
            (timestamp, timestamp, operation_id),
        )
        if cursor.rowcount == 1:
            heartbeat_vps_lease(db, "SERVER", operation_id)
            return True
        return False


def create_protocol_refresh_operation() -> int:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """
            INSERT INTO server_operations(
                kind, status, current_step, action_state, created_at, updated_at
            ) VALUES (
                'STATUS', 'PENDING', 'queued_protocol_refresh',
                'NOT_STARTED', ?, ?
            )
            """,
            (timestamp, timestamp),
        )
        operation_id = int(cursor.lastrowid)
        acquire_vps_lease(db, "SERVER", operation_id)
        return operation_id


def get_server_operation(operation_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM server_operations WHERE id = ?", (operation_id,)
        ).fetchone()


def get_latest_server_operation(kind: str | None = None) -> dict[str, Any] | None:
    with get_db() as db:
        if kind:
            return db.execute(
                "SELECT * FROM server_operations WHERE kind = ? ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
        return db.execute(
            "SELECT * FROM server_operations ORDER BY id DESC LIMIT 1"
        ).fetchone()


def list_server_operations(limit: int = 100) -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM server_operations ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()


def get_active_vps_operation() -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT owner_type, owner_id, acquired_at, heartbeat_at, expires_at "
            "FROM operation_leases WHERE resource = 'vpn_vps'"
        ).fetchone()


async def _provider_status(
    client: AezaClient,
    service_id: str,
    timeout: float,
) -> dict[str, Any]:
    return await asyncio.wait_for(
        client.get_service(service_id), timeout=max(0.01, timeout)
    )


def _provider_is_up(status: str) -> bool | None:
    normalized = status.strip().lower()
    if normalized in {"running", "active", "online", "started"}:
        return True
    if normalized in {
        "stopped",
        "inactive",
        "offline",
        "down",
        "rebooting",
        "restarting",
    }:
        return False
    return None


def _parse_services(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        name, separator, state = line.partition("=")
        if separator and name in {"xray", "hysteria-server", "awg-quick@awg0"}:
            result[name] = "active" if state.strip() == "active" else "inactive"
    return result


def _enabled_services() -> tuple[str, ...]:
    services = []
    if vless_enabled():
        services.append("xray")
    if hysteria_enabled():
        services.append("hysteria-server")
    if amnezia_enabled():
        services.append("awg-quick@awg0")
    return tuple(services)


async def _service_and_protocol_health(
    operation_id: int,
    host: str,
    timing: ServerTiming,
) -> tuple[dict[str, str], list[dict[str, object]], str]:
    update_server_operation(operation_id, current_step="service_health")
    exit_code, output = await asyncio.wait_for(
        run_remote_command(
            host,
            SERVICE_STATUS_COMMAND,
            timeout=timing.command_timeout,
        ),
        timeout=max(0.01, timing.command_timeout + 1),
    )
    services = _parse_services(output) if exit_code == 0 else {}
    update_server_operation(operation_id, services_json=_json(services))
    unhealthy = [
        service for service in _enabled_services() if services.get(service) != "active"
    ]

    update_server_operation(operation_id, current_step="protocol_health")
    protocols = await asyncio.wait_for(
        refresh_protocol_statuses(
            host, command_timeout=timing.command_timeout
        ),
        timeout=max(0.01, timing.command_timeout + 1),
    )
    protocol_summary = [
        {"key": item.get("key"), "status": item.get("status")}
        for item in protocols
    ]
    update_server_operation(operation_id, protocol_health_json=_json(protocol_summary))
    failed_protocols = [
        str(item.get("key")) for item in protocol_summary if item.get("status") == "FAILED"
    ]
    problems = []
    if unhealthy:
        problems.append(f"inactive services: {', '.join(unhealthy)}")
    if failed_protocols:
        problems.append(f"failed protocols: {', '.join(failed_protocols)}")
    return services, protocol_summary, "; ".join(problems)


async def run_server_status(
    operation_id: int,
    *,
    timing: ServerTiming | None = None,
) -> None:
    timing = timing or configured_server_timing()
    timestamp = now_iso()
    try:
        update_server_operation(
            operation_id,
            status="RUNNING",
            current_step="provider_status",
            started_at=timestamp,
        )
        client = AezaClient(aeza_api_base(), aeza_token(), timeout=timing.request_timeout)
        service = await _provider_status(client, aeza_service_id(), timing.request_timeout)
        provider_status = _clean(service.get("provider_status"), 200)
        provider_ip = _clean(service.get("ip"), 200)
        update_server_operation(
            operation_id,
            provider_status=provider_status,
            provider_ip=provider_ip,
            current_step="ssh_reachability",
        )
        host = resolve_eu_host() or provider_ip or get_setting("current_ip")
        ssh_up = bool(host) and await tcp_check(
            host, eu_ssh_port(), timeout=timing.ssh_probe_timeout
        )
        update_server_operation(
            operation_id,
            ssh_status="UP" if ssh_up else "DOWN",
        )
        warning = ""
        if ssh_up:
            _, _, warning = await _service_and_protocol_health(
                operation_id, host, timing
            )
        else:
            warning = "SSH is unreachable; service and protocol checks were skipped"
        update_server_operation(
            operation_id,
            status="DONE",
            current_step="done",
            result_message="Aeza VPS status refreshed",
            warning_message=warning,
            completed_at=now_iso(),
        )
    except TimeoutError:
        update_server_operation(
            operation_id,
            status="TIMED_OUT",
            current_step="timed_out",
            error_message="Aeza VPS status refresh timed out",
            completed_at=now_iso(),
        )
    except Exception as exc:
        update_server_operation(
            operation_id,
            status="FAILED",
            current_step="failed",
            error_message=_safe_error(exc),
            completed_at=now_iso(),
        )


async def run_protocol_refresh(
    operation_id: int,
    *,
    timing: ServerTiming | None = None,
) -> None:
    timing = timing or configured_server_timing()
    operation = get_server_operation(operation_id)
    if operation is None or operation["current_step"] != "queued_protocol_refresh":
        return
    try:
        host = resolve_eu_host() or get_setting("current_ip")
        if not host:
            raise RuntimeError("VPN VPS host is not configured")
        update_server_operation(
            operation_id,
            status="RUNNING",
            current_step="protocol_health",
            started_at=now_iso(),
        )
        protocols = await asyncio.wait_for(
            refresh_protocol_statuses(
                host,
                command_timeout=timing.command_timeout,
            ),
            timeout=max(
                0.01,
                timing.command_timeout + timing.ssh_probe_timeout + 2,
            ),
        )
        protocol_summary = [
            {"key": item.get("key"), "status": item.get("status")}
            for item in protocols
        ]
        update_server_operation(
            operation_id,
            status="DONE",
            current_step="done",
            protocol_health_json=_json(protocol_summary),
            result_message="VPN protocol status refreshed",
            completed_at=now_iso(),
        )
    except TimeoutError:
        update_server_operation(
            operation_id,
            status="TIMED_OUT",
            current_step="timed_out",
            error_message="VPN protocol status refresh timed out",
            completed_at=now_iso(),
        )
    except Exception as exc:
        update_server_operation(
            operation_id,
            status="FAILED",
            current_step="failed",
            error_message=_safe_error(exc),
            completed_at=now_iso(),
        )


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def run_server_reboot(
    operation_id: int,
    *,
    timing: ServerTiming | None = None,
    sleeper: Callable[[float], Awaitable[None]] = _sleep,
) -> None:
    timing = timing or configured_server_timing()
    operation = get_server_operation(operation_id)
    if not operation:
        return
    if operation["action_state"] != "NOT_STARTED":
        if operation["status"] in TERMINAL_STATUSES:
            return
        update_server_operation(
            operation_id,
            status="AMBIGUOUS",
            action_state="AMBIGUOUS",
            current_step="manual_verification_required",
            error_message="Reboot may already have been sent; it was not sent again",
            completed_at=now_iso(),
        )
        return

    ambiguous_action = False
    warning_messages: list[str] = []
    try:
        client = AezaClient(aeza_api_base(), aeza_token(), timeout=timing.request_timeout)
        service_id = aeza_service_id()
        update_server_operation(
            operation_id,
            status="RUNNING",
            current_step="provider_status_before_reboot",
            started_at=now_iso(),
        )
        before = await _provider_status(client, service_id, timing.request_timeout)
        provider_status = _clean(before.get("provider_status"), 200)
        provider_ip = _clean(before.get("ip"), 200)
        update_server_operation(
            operation_id,
            provider_status=provider_status,
            provider_ip=provider_ip,
        )
        host = resolve_eu_host() or provider_ip or get_setting("current_ip")
        if not host:
            raise RuntimeError("VPN VPS host is not configured")
        if not mark_reboot_sending(operation_id):
            raise RuntimeError("Reboot action was already started and will not be sent again")
        try:
            await asyncio.wait_for(
                client.reboot_service(service_id),
                timeout=max(0.01, timing.request_timeout),
            )
            update_server_operation(operation_id, action_state="SENT", current_step="wait_reboot")
        except Exception as exc:
            ambiguous_action = True
            warning_messages.append(
                f"Aeza reboot request result is ambiguous: {_safe_error(exc)}"
            )
            update_server_operation(
                operation_id,
                action_state="AMBIGUOUS",
                current_step="verify_ambiguous_reboot",
                warning_message=warning_messages[-1],
            )

        attempts = max(1, math.ceil(timing.reboot_timeout / max(0.01, timing.poll_interval)))
        saw_ssh_down = False
        provider_waiting = False
        ssh_waiting = True
        for attempt in range(attempts):
            heartbeat = "verify_reboot" if not ambiguous_action else "verify_ambiguous_reboot"
            update_server_operation(operation_id, current_step=heartbeat)
            try:
                current = await _provider_status(client, service_id, timing.request_timeout)
                current_status = _clean(current.get("provider_status"), 200)
                current_ip = _clean(current.get("ip"), 200)
                update_server_operation(
                    operation_id,
                    provider_status=current_status,
                    provider_ip=current_ip,
                )
                provider_up = _provider_is_up(current_status)
                provider_waiting = provider_up is False
            except TimeoutError:
                provider_up = False
                provider_waiting = True

            ssh_up = await tcp_check(host, eu_ssh_port(), timeout=timing.ssh_probe_timeout)
            ssh_waiting = not ssh_up
            saw_ssh_down = saw_ssh_down or not ssh_up
            update_server_operation(
                operation_id,
                ssh_status="UP" if ssh_up else "DOWN",
            )
            if ssh_up and provider_up is not False:
                if not saw_ssh_down:
                    warning_messages.append(
                        "SSH-down transition was not observed before recovery"
                    )
                _, _, health_error = await _service_and_protocol_health(
                    operation_id, host, timing
                )
                if health_error:
                    warning_messages.append(health_error)
                    terminal = "AMBIGUOUS" if ambiguous_action else "FAILED"
                else:
                    terminal = "AMBIGUOUS" if ambiguous_action else "DONE"
                update_server_operation(
                    operation_id,
                    status=terminal,
                    current_step="done" if terminal == "DONE" else "verification_complete",
                    result_message="VPS reboot verification completed",
                    warning_message="; ".join(warning_messages),
                    error_message=(
                        "Reboot request outcome remains ambiguous"
                        if ambiguous_action
                        else health_error
                    ),
                    completed_at=now_iso(),
                )
                return
            if attempt + 1 < attempts:
                await sleeper(timing.poll_interval)

        if provider_waiting:
            message = "Aeza did not report VPS recovery before timeout"
            step = "wait_provider"
        elif ssh_waiting:
            message = "SSH did not become reachable before timeout"
            step = "wait_ssh"
        else:
            message = "VPS reboot verification timed out"
            step = "timed_out"
        terminal = "AMBIGUOUS" if ambiguous_action else "TIMED_OUT"
        update_server_operation(
            operation_id,
            status=terminal,
            current_step=step,
            error_message=message,
            warning_message="; ".join(warning_messages),
            completed_at=now_iso(),
        )
    except Exception as exc:
        current = get_server_operation(operation_id)
        may_have_sent = current and current["action_state"] in {
            "SENDING",
            "SENT",
            "AMBIGUOUS",
        }
        update_server_operation(
            operation_id,
            status="AMBIGUOUS" if may_have_sent else "FAILED",
            action_state="AMBIGUOUS" if may_have_sent else "NOT_STARTED",
            current_step="failed",
            error_message=_safe_error(exc),
            completed_at=now_iso(),
        )
