from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from dataclasses import replace
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from app.db import get_db, now_iso, set_setting, update_operation
from app.eu_install import build_eu_deploy_script, run_remote_command
from app.ip_change import (
    IpChangeError, IpRotationTiming, Sleeper, _ensure_bound_revision_current,
    _sleep, _step, _verify_protocol_health, _wait_for_ssh,
)
from app.runtime_config import (
    eu_ssh_host, eu_ssh_key_path, eu_ssh_password, eu_ssh_port, eu_ssh_user,
    transactional_vpn_apply_enabled, vpn_deploy_timeout_seconds,
)
from app.secret_sanitization import sanitize_error
from app.sprintbox import SprintboxApiError, SprintboxClient, box_ipv4
from app.vps_provider import bound_provider, provider_name, sprintbox_config
from app.vpn_state import load_ip_change_snapshot, publish_ip_change
from app.operation_coordinator import TERMINAL_STATUSES
from app.vpn_config import CapturedVpnConfig
from app.sprintbox_review import context, retained_boxes, validate_target


def _public_key() -> str:
    if eu_ssh_user() != "root" or eu_ssh_port() != 22 or eu_ssh_password() or not eu_ssh_key_path():
        raise IpChangeError("New SprintBOX servers require SSH root:22 with a private key and no SSH password; configure Setup first")
    raw = Path(eu_ssh_key_path()).expanduser().read_bytes()
    try:
        key = serialization.load_ssh_private_key(raw, password=None)
    except ValueError:
        key = serialization.load_pem_private_key(raw, password=None)
    return key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
    ).decode()


def _check_binding(bound: dict[str, str]) -> None:
    current = sprintbox_config()
    if provider_name() != "sprintbox" or any(
        current[key] != bound[key] for key in ("login", "box_name", "service", "distributive")
    ) or hashlib.sha256(current["key"].encode()).hexdigest() != bound["credential_hash"]:
        raise IpChangeError("SprintBOX settings changed; prepare a new operation")


async def _wait_task(client: SprintboxClient, task: str, operation_id: int, step: str,
                     timing: IpRotationTiming, sleeper: Sleeper) -> None:
    attempts = max(1, math.ceil(timing.ip_appear_timeout / max(.01, timing.ip_appear_interval)))
    for attempt in range(attempts):
        await _step(operation_id, step)
        status = await client.task_status(task)
        if status == "finished":
            return
        if status == "error":
            raise SprintboxApiError("TASK_FAILED", "Asynchronous task failed; inspect the SprintBOX panel")
        if attempt + 1 < attempts:
            await sleeper(timing.ip_appear_interval)
    raise TimeoutError("SprintBOX task did not finish before timeout; inspect the panel before retrying")


async def _deploy(operation_id: int, host: str, config: CapturedVpnConfig) -> None:
    # A new box needs binaries and services as well as configuration files.
    timeout = float(vpn_deploy_timeout_seconds())
    task = asyncio.create_task(run_remote_command(
        host, "bash -s", stdin_data=build_eu_deploy_script(config, transactional=True), timeout=timeout,
    ))
    try:
        async with asyncio.timeout(timeout + 1):
            while True:
                done, _ = await asyncio.wait({task}, timeout=10)
                await _step(operation_id, "install_new_box", apply_state="RUNNING")
                if done:
                    code, _ = task.result()
                    if code:
                        raise IpChangeError(f"SprintBOX VPN installation failed (exit {code}); old box preserved")
                    return
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def run_sprintbox_rotation(operation_id: int, *, timing: IpRotationTiming | None = None,
                                 sleeper: Sleeper = _sleep) -> None:
    timing = timing or IpRotationTiming.configured()
    operation, config = load_ip_change_snapshot(operation_id)
    if operation["status"] in TERMINAL_STATUSES:
        return
    if any(operation[field] != "NOT_STARTED" for field in ("purchase_state", "make_main_state", "apply_state")):
        update_operation(operation_id, status="AMBIGUOUS", action_state="AMBIGUOUS",
                         current_step="manual_verification_required", error_message="Interrupted SprintBOX operation; external actions were not resent")
        return
    bound = bound_provider(operation_id)
    credentials = sprintbox_config()
    client = SprintboxClient(credentials["login"], credentials["key"], timeout=timing.provider_timeout)
    published = False
    creation_started = False
    new_name = ""
    try:
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        if not transactional_vpn_apply_enabled():
            raise IpChangeError("SprintBOX rotation requires transactional VPN apply")
        if not any((config.vless.protocol.enabled, config.hysteria.protocol.enabled, config.amnezia.protocol.enabled)):
            raise IpChangeError("Enable at least one VPN protocol before rotating SprintBOX")
        if not all(bound.get(key) for key in ("login", "box_name", "service", "distributive")) or not credentials["key"]:
            raise IpChangeError("Configure SprintBOX credentials, current box, service and distributive in Setup")
        if not config.current_ip or eu_ssh_host() not in ("", config.current_ip):
            raise IpChangeError("SprintBOX rotation requires SSH to use the current VPN IP")
        pubkey = _public_key()
        await _step(operation_id, "inspect_old_box")
        old = await client.get_box(bound["box_name"])
        if config.current_ip not in box_ipv4(old):
            raise IpChangeError("Configured SprintBOX box does not own current_ip; refusing rotation")
        before = await client.list_boxes()
        update_operation(operation_id, old_ip=config.current_ip, old_ip_id=bound["box_name"])
        marker = f"AutoVPN rotation {operation_id} {uuid.uuid4().hex}"
        operation_context = {"marker": marker, "retained_boxes": retained_boxes(bound["box_name"], config.current_ip, bound)}
        set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "create_new_box")
        with get_db() as db:
            claimed = db.execute(
                "UPDATE ip_change_operations SET purchase_state = 'SENDING', action_state = 'PURCHASE_SENDING' "
                "WHERE id = ? AND purchase_state = 'NOT_STARTED' AND status = 'RUNNING'",
                (operation_id,),
            ).rowcount
        if not claimed:
            return
        creation_started = True
        try:
            job = await client.create_box(bound["service"], bound["distributive"], pubkey, marker)
        except SprintboxApiError:
            creation_started = False  # explicit API rejection; no automatic retry
            update_operation(operation_id, purchase_state="REJECTED")
            raise
        operation_context["create_task"] = job
        set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        update_operation(operation_id, purchase_state="SENT")
        await _wait_task(client, job, operation_id, "wait_new_box", timing, sleeper)
        # Correlate by our unique description, never by the first newly appearing server.
        attempts = max(1, math.ceil(timing.ip_appear_timeout / max(.01, timing.ip_appear_interval)))
        for attempt in range(attempts):
            await _step(operation_id, "identify_new_box")
            boxes = await client.list_boxes()
            candidates = [(name, box) for name, box in boxes.items()
                          if name not in before and box.get("name") == name and box.get("description") == marker]
            if len(candidates) > 1:
                raise IpChangeError("Multiple SprintBOX boxes match this operation; manual verification required")
            if len(candidates) == 1 and len(box_ipv4(candidates[0][1])) == 1:
                new_name, box = candidates[0]
                new_ip = box_ipv4(box)[0]
                if new_ip != config.current_ip and all(new_ip not in box_ipv4(item) for item in before.values()):
                    break
            if attempt + 1 < attempts:
                await sleeper(timing.ip_appear_interval)
        else:
            raise IpChangeError("Cannot uniquely identify new SprintBOX IPv4; inspect the panel")
        update_operation(operation_id, new_ip_id=new_name, new_ip=new_ip,
                         purchase_state="CONFIRMED", new_ip_created_at=now_iso())
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "wait_ssh")
        await _wait_for_ssh(new_ip, timing, sleeper)
        update_operation(operation_id, server_reachable_at=now_iso())
        await _step(operation_id, "install_new_box", action_state="APPLYING", apply_state="RUNNING")
        new_config = replace(config, current_ip=new_ip)
        await _deploy(operation_id, new_ip, new_config)
        update_operation(operation_id, apply_state="APPLIED", apply_completed_at=now_iso())
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "protocol_health")
        health = await _verify_protocol_health(new_ip, new_config, timing)
        _check_binding(bound)
        revision = publish_ip_change(operation_id, new_ip, health, sprintbox_box_name=new_name)
        published = True
        update_operation(operation_id, status="DONE", current_step="awaiting_user_review",
                         action_state="AWAITING_USER_REVIEW", published_revision=revision, error_message="")
    except Exception as exc:
        message = sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password())
        if creation_started:
            message += f"; inspect SprintBOX before retrying (new box: {new_name or 'not identified'}). Old box was not deleted."
        update_operation(operation_id, status="AMBIGUOUS" if creation_started or published else "FAILED",
                         action_state="AMBIGUOUS" if creation_started or published else "FAILED",
                         current_step="manual_verification_required" if creation_started or published else "failed",
                         error_message=message)


async def run_sprintbox_cleanup(operation_id: int, *, timing: IpRotationTiming | None = None,
                               sleeper: Sleeper = _sleep) -> None:
    timing = timing or IpRotationTiming.configured()
    credentials = sprintbox_config()
    client = SprintboxClient(credentials["login"], credentials["key"], timeout=timing.provider_timeout)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        claimed = db.execute(
            "UPDATE ip_change_operations SET status = 'RUNNING', action_state = 'CLEANUP_RUNNING', updated_at = ? "
            "WHERE id = ? AND status = 'PENDING' AND action_state = 'CLEANUP_QUEUED'",
            (now_iso(), operation_id),
        ).rowcount
        if not claimed:
            return
        operation_context = context(db, operation_id)
    warnings = []
    ambiguous = False
    try:
        with get_db() as db:
            active = validate_target(db, operation_id)
        retained = operation_context.get("retained_boxes", {})
        results = operation_context.setdefault("cleanup_results", {})
        for name, expected_ip in dict(retained).items():
            sending = False
            try:
                with get_db() as db:
                    validate_target(db, operation_id)
                if name == active["new_ip_id"]:
                    raise IpChangeError("Refusing to delete the active VPN box")
                await _step(operation_id, "delete_old_box")
                if name not in await client.list_boxes():
                    results[name] = {"state": "ABSENT"}
                    continue
                box = await client.get_box(name)
                if expected_ip not in box_ipv4(box):
                    raise IpChangeError("Box IP changed; inspect it manually")
                if name in results:
                    raise IpChangeError("Deletion was already attempted; inspect it manually")
                results[name] = {"state": "SENDING"}
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
                sending = True
                job = await client.delete_box(name)
                results[name] = {"state": "SENT", "task": job}
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
                await _wait_task(client, job, operation_id, "wait_old_box_deletion", timing, sleeper)
                if name in await client.list_boxes():
                    raise IpChangeError("Delete task finished but box still exists")
                results[name]["state"] = "DELETED"
            except Exception as exc:
                uncertain = sending and not isinstance(exc, SprintboxApiError)
                ambiguous = ambiguous or uncertain
                message = sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password())
                results[name] = {**results.get(name, {}), "state": "AMBIGUOUS" if uncertain else "MANUAL", "error": message}
                warnings.append(f"{name}: {message}")
            finally:
                if results.get(name, {}).get("state") in {"DELETED", "ABSENT"}:
                    retained.pop(name, None)
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        warning = "Удалите или проверьте старые боксы вручную в кабинете SprintBOX: " + "; ".join(warnings) if warnings else ""
        update_operation(operation_id, status="DONE", current_step="done", error_message="",
                         action_state="CLEANUP_AMBIGUOUS" if ambiguous else "CLEANUP_REJECTED" if warnings else "DONE",
                         cleanup_warning=warning)
    except Exception as exc:
        message = sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password())
        update_operation(operation_id, status="DONE", current_step="manual_verification_required", action_state="CLEANUP_AMBIGUOUS",
                         cleanup_warning=f"Проверьте старые боксы вручную в кабинете SprintBOX: {message}")
