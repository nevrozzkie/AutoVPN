from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import secrets
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app.db import get_db, now_iso, set_setting, update_operation
from app.eu_install import (build_eu_deploy_script, run_remote_command,
                            SshHostKeyMismatch, SshConnectError, SshAuthenticationError)
from app.ip_change import (
    IpChangeError, IpRotationTiming, Sleeper, _ensure_bound_revision_current,
    _sleep, _step, _verify_protocol_health, _wait_for_ssh,
)
from app.runtime_config import (
    eu_ssh_host, eu_ssh_password,
    transactional_vpn_apply_enabled, vpn_deploy_timeout_seconds,
)
from app.secret_sanitization import sanitize_error
from app.sprintbox import SprintboxApiError, SprintboxClient, SprintboxTaskIdMissing, box_ipv4
from app.vps_provider import bound_provider, provider_name, sprintbox_config
from app.vpn_state import load_ip_change_snapshot, publish_ip_change
from app.operation_coordinator import TERMINAL_STATUSES, release_vps_lease
from app.port_forwarding import reconcile as reconcile_port_forwarding
from app.vpn_config import CapturedVpnConfig
from app.sprintbox_review import context, retained_boxes, validate_target, validate_ssh_decision
from app.sprintbox_ssh import SprintboxSsh, prepare_sprintbox_ssh
from app.ssh_credentials import use_ssh_credentials
from app.sprintbox_accounts import (bound_credentials, box_owner, box_ref, record_box, current_id)


def _new_box_label(boxes: dict) -> str:
    existing = set(boxes) | {str(box.get("description") or "") for box in boxes.values()}
    start = secrets.randbelow(90_000)
    for offset in range(90_000):
        label = f"бокс-{10_000 + (start + offset) % 90_000}"
        if label not in existing:
            return label
    raise IpChangeError("No free five-digit SprintBOX name is available")


def _check_binding(bound: dict[str, str]) -> None:
    current = sprintbox_config()
    if bound.get('account_id'):
        with get_db() as db:
            old = bound_credentials(db, bound, old=True)
            if (current.get('account_id') != bound['account_id'] or current_id(db) != bound['old_account_id']
                    or (bound.get('old_login') and old['login'] != bound['old_login'])
                    or (bound.get('old_credential_hash') and hashlib.sha256(old['key'].encode()).hexdigest() != bound['old_credential_hash'])):
                raise IpChangeError('SprintBOX account ownership changed; prepare a new operation')
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
                    code, output = task.result()
                    if code:
                        prefix = "[autovpn] bootstrap APT failure: "
                        reason = next((line.removeprefix(prefix) for line in reversed(output.splitlines())
                                       if line.startswith(prefix)), "")
                        reason = re.sub(r"(https?://)[^\s/@]+@", r"\1[redacted]@", reason)
                        detail = f": {sanitize_error(reason, limit=800)}" if reason else ""
                        raise IpChangeError(f"SprintBOX VPN installation failed (exit {code}){detail}; old box preserved")
                    return
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _refresh_new_box_host_key(operation_id, config, bound, ssh, name, ip, error, timing):
    """Forget one stale pin only for this request's newly purchased, verified box."""
    from app.eu_install import forget_ssh_known_host
    if error.host != ip or error.port != 22:
        raise IpChangeError('SSH host key mismatch refers to a different endpoint')
    _check_binding(bound)
    _ensure_bound_revision_current(config.revision)
    credentials = sprintbox_config()
    client = SprintboxClient(credentials['login'], credentials['key'], timeout=timing.provider_timeout)
    box = await client.get_box(name)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        operation = db.execute('SELECT * FROM ip_change_operations WHERE id=?', (operation_id,)).fetchone()
        ctx = context(db, operation_id)
        lease = db.execute("SELECT 1 FROM operation_leases WHERE resource='vpn_vps' AND owner_type='IP_CHANGE' AND owner_id=?", (operation_id,)).fetchone()
        revision = db.execute('SELECT desired_revision FROM vpn_state WHERE singleton=1').fetchone()
        primary = db.execute("SELECT value FROM settings WHERE key='current_ip'").fetchone()
        anchor = db.execute('SELECT 1 FROM vpn_anchors WHERE ip=?', (ip,)).fetchone()
        if (not operation or not lease or operation['status'] != 'RUNNING'
                or operation['purchase_state'] != 'CONFIRMED' or operation['published_revision']
                or operation['make_main_state'] != 'NOT_STARTED' or operation['apply_state'] != 'RUNNING'
                or operation['new_ip_id'] != name or operation['new_ip'] != ip
                or ip == config.current_ip or (primary and ip == primary['value']) or anchor
                or revision['desired_revision'] != config.revision
                or not ctx.get('marker') or ctx.get('host_key_reset_attempted')
                or ctx.get('ssh_key_path') != ssh.credentials.key_path
                or box.get('name') != name or box.get('description') != ctx['marker'] or box_ipv4(box) != [ip]):
            raise IpChangeError('Cannot safely refresh the SSH host key for this box')
        ctx['host_key_reset_attempted'] = True
        ctx['host_key_reset_at'] = now_iso()
        db.execute('UPDATE settings SET value=? WHERE key=?', (json.dumps(ctx), f'ip_change.{operation_id}.sprintbox'))
    _check_binding(bound)
    await _step(operation_id, 'refresh_new_box_host_key')
    forget_ssh_known_host(ip)


async def _deploy_new_box(operation_id, config, bound, ssh, name, ip, timing, sleeper):
    connect_retried = False
    while True:
        try:
            await _step(operation_id, 'install_new_box', action_state='APPLYING', apply_state='RUNNING')
            await _deploy(operation_id, ip, replace(config, current_ip=ip))
            return True
        except SshHostKeyMismatch as exc:
            await _refresh_new_box_host_key(operation_id, config, bound, ssh, name, ip, exc, timing)
        except SshAuthenticationError as exc:
            if exc.host != ip or exc.port != 22:
                raise
            update_operation(operation_id, status='DONE', action_state='SSH_UNREACHABLE',
                             apply_state='NOT_STARTED', current_step='ssh_decision',
                             error_message='Новый сервер ещё не принял SSH-ключ. Можно подождать или выбрать другой сервер.')
            return False
        except SshConnectError as exc:
            if exc.host != ip or exc.port != 22:
                raise
            if connect_retried:
                update_operation(operation_id, status='DONE', action_state='SSH_UNREACHABLE',
                                 apply_state='NOT_STARTED', current_step='ssh_decision',
                                 error_message='SSH-соединение с новым сервером не установлено. Текущее подключение сохранено.')
                return False
            connect_retried = True
            _check_binding(bound)
            _ensure_bound_revision_current(config.revision)
            await _step(operation_id, 'retry_new_box_ssh', apply_state='NOT_STARTED')
            try:
                await _wait_for_ssh(ip, replace(timing, ssh_timeout=min(timing.ssh_timeout, 45)), sleeper)
            except IpChangeError:
                update_operation(operation_id, status='DONE', action_state='SSH_UNREACHABLE',
                                 current_step='ssh_decision', error_message='')
                return False


async def run_sprintbox_rotation(operation_id: int, *, timing: IpRotationTiming | None = None,
                                 sleeper: Sleeper = _sleep) -> None:
    timing = timing or IpRotationTiming.configured()
    operation, config = load_ip_change_snapshot(operation_id)
    if operation["status"] == "PENDING" and operation["action_state"] == "SSH_WAIT_QUEUED":
        await _resume_ssh_wait(operation_id, config, timing, sleeper)
        return
    if operation["status"] in TERMINAL_STATUSES:
        return
    if any(operation[field] != "NOT_STARTED" for field in ("purchase_state", "make_main_state", "apply_state")):
        update_operation(operation_id, status="AMBIGUOUS", action_state="AMBIGUOUS",
                         current_step="manual_verification_required", error_message="Interrupted SprintBOX operation; external actions were not resent")
        return
    bound = bound_provider(operation_id)
    credentials = sprintbox_config()
    client = SprintboxClient(credentials["login"], credentials["key"], timeout=timing.provider_timeout)
    creation_started = False
    new_name = ""
    try:
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        if not transactional_vpn_apply_enabled():
            raise IpChangeError("SprintBOX rotation requires transactional VPN apply")
        if not any((config.vless.protocol.enabled, config.hysteria.protocol.enabled, config.amnezia.protocol.enabled)):
            raise IpChangeError("Enable at least one VPN protocol before rotating SprintBOX")
        if not config.enabled_clients:
            raise IpChangeError("Create at least one enabled VPN client before creating a SprintBOX")
        if not all(bound.get(key) for key in ("login", "service", "distributive")) or not credentials["key"]:
            raise IpChangeError("Configure SprintBOX credentials, service and distributive in Setup")
        first_box = not bound["box_name"]
        if not first_box and (not config.current_ip or eu_ssh_host() not in ("", config.current_ip)):
            raise IpChangeError("SprintBOX rotation requires SSH to use the current VPN IP")
        ssh = prepare_sprintbox_ssh()
        pubkey = ssh.public_key
        with get_db() as db:
            initial_context = context(db, operation_id)
            retained = dict(initial_context.get("retained_boxes", {}))
        if not first_box:
            await _step(operation_id, "inspect_old_box")
            with get_db() as db:
                old_credentials = bound_credentials(db, bound, old=True)
            old_client = SprintboxClient(old_credentials['login'], old_credentials['key'], timeout=timing.provider_timeout)
            old = await old_client.get_box(bound["box_name"])
            if config.current_ip not in box_ipv4(old):
                raise IpChangeError("Configured SprintBOX box does not own current_ip; refusing rotation")
            retained.update(retained_boxes(bound["box_name"], config.current_ip, bound))
        before = await client.list_boxes()
        update_operation(operation_id, old_ip=config.current_ip, old_ip_id=bound["box_name"])
        marker = _new_box_label(before)
        operation_context = {"marker": marker, "request_id": uuid.uuid4().hex,
                             "first_box": first_box, "retained_boxes": retained,
                             "retained_snapshots": initial_context.get("retained_snapshots", {}),
                             "ssh_key_path": ssh.credentials.key_path}
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
        except SprintboxTaskIdMissing:
            # An accepted create can already have produced a box without a task.
            # Only inspect this request's unique label; never resend box/add.
            job = None
        except SprintboxApiError:
            creation_started = False  # explicit API rejection; no automatic retry
            update_operation(operation_id, purchase_state="REJECTED")
            raise
        operation_context["create_task"] = job
        set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        update_operation(operation_id, purchase_state="SENT", action_state="PURCHASE_SENT")
        if job is not None:
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
            if len(candidates) == 1:
                new_name, box = candidates[0]
                # Retain its identity even if it is unpaid/not yet assigned an IP.
                update_operation(operation_id, new_ip_id=new_name)
                if len(box_ipv4(box)) == 1:
                    new_ip = box_ipv4(box)[0]
                    if new_ip != config.current_ip and all(new_ip not in box_ipv4(item) for item in before.values()):
                        break
            if attempt + 1 < attempts:
                await sleeper(timing.ip_appear_interval)
        else:
            if new_name:
                raise IpChangeError(
                    f"Бокс {new_name} создан, но новый IPv4 не готов. Проверьте статус бокса и баланс "
                    "в кабинете SprintBOX; при статусе «не оплачен» пополните баланс. "
                    "Повторный запрос создания не отправлялся"
                )
            raise IpChangeError("Cannot uniquely identify new SprintBOX IPv4; inspect the panel")
        update_operation(operation_id, new_ip_id=new_name, new_ip=new_ip,
                         purchase_state="CONFIRMED", new_ip_created_at=now_iso())
        with get_db() as db:
            record_box(db, bound.get('account_id', ''), new_name, new_ip)
        await _provision_box(operation_id, config, bound, ssh, new_name, new_ip, timing, sleeper)
    except Exception as exc:
        message = sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password())
        if creation_started:
            message += f"; inspect SprintBOX before retrying (new box: {new_name or 'not identified'}). Old box was not deleted."
        update_operation(operation_id, status="AMBIGUOUS" if creation_started else "FAILED",
                         action_state="AMBIGUOUS" if creation_started else "FAILED",
                         current_step="manual_verification_required" if creation_started else "failed",
                         error_message=message)


async def _provision_box(operation_id: int, config: CapturedVpnConfig, bound: dict[str, str],
                         ssh: SprintboxSsh, name: str, ip: str, timing: IpRotationTiming,
                         sleeper: Sleeper) -> None:
    _check_binding(bound)
    _ensure_bound_revision_current(config.revision)
    with use_ssh_credentials(ssh.credentials):
        await _step(operation_id, "wait_ssh", action_state="WAITING_SSH")
        try:
            # Offer a decision after roughly a minute, rather than holding a
            # purchase workflow for the full VPN health-check timeout.
            await _wait_for_ssh(ip, replace(timing, ssh_timeout=min(timing.ssh_timeout, 45)), sleeper)
        except IpChangeError:
            update_operation(operation_id, status="DONE", action_state="SSH_UNREACHABLE",
                             current_step="ssh_decision", error_message="")
            return
        update_operation(operation_id, server_reachable_at=now_iso())
        await _step(operation_id, "install_new_box", action_state="APPLYING", apply_state="RUNNING")
        new_config = replace(config, current_ip=ip)
        if not await _deploy_new_box(operation_id, config, bound, ssh, name, ip, timing, sleeper):
            return
        update_operation(operation_id, apply_state="APPLIED", apply_completed_at=now_iso())
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        await _step(operation_id, "protocol_health")
        health = await _verify_protocol_health(ip, new_config, timing)
        await _step(operation_id, "port_forwarding")
        forwarding = await reconcile_port_forwarding(ip, new_config, strict=True, new_box=True)
    _check_binding(bound)
    revision = publish_ip_change(operation_id, ip, health, sprintbox_box_name=name,
                                 sprintbox_ssh_key_path=ssh.credentials.key_path,
                                 port_forwarding_result=forwarding)
    update_operation(operation_id, status="DONE", current_step="awaiting_user_review",
                     action_state="AWAITING_USER_REVIEW", published_revision=revision, error_message="")


async def _resume_ssh_wait(operation_id: int, config: CapturedVpnConfig,
                           timing: IpRotationTiming, sleeper: Sleeper) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,action_state FROM ip_change_operations WHERE id = ?", (operation_id,)).fetchone()
        if not row or (row["status"], row["action_state"]) != ("PENDING", "SSH_WAIT_QUEUED"):
            return
        try:
            operation = validate_ssh_decision(db, operation_id, queued=True)
        except ValueError:
            db.execute("UPDATE ip_change_operations SET status = 'DONE', action_state = 'SSH_UNREACHABLE', "
                       "current_step = 'ssh_decision', updated_at = ? WHERE id = ?", (now_iso(), operation_id))
            release_vps_lease(db, "IP_CHANGE", operation_id)
            return
        operation_context = context(db, operation_id)
        db.execute("UPDATE ip_change_operations SET status = 'RUNNING', action_state = 'WAITING_SSH', "
                   "updated_at = ? WHERE id = ?", (now_iso(), operation_id))
    credentials = sprintbox_config()
    try:
        bound = bound_provider(operation_id)
        _check_binding(bound)
        _ensure_bound_revision_current(config.revision)
        client = SprintboxClient(credentials["login"], credentials["key"], timeout=timing.provider_timeout)
        name, ip = operation["new_ip_id"], operation["new_ip"]
        box = await client.get_box(name)
        if box.get("description") != operation_context.get("marker") or box_ipv4(box) != [ip]:
            raise IpChangeError("Created box identity changed; refusing to resume")
        if not Path(operation_context.get("ssh_key_path", "")).is_file():
            raise IpChangeError("Original SprintBOX SSH key is missing")
        ssh = prepare_sprintbox_ssh()
        if ssh.credentials.key_path != operation_context["ssh_key_path"]:
            raise IpChangeError("SprintBOX SSH key path changed; refusing to resume")
        await _provision_box(operation_id, config, bound, ssh, name, ip, timing, sleeper)
    except Exception as exc:
        update_operation(operation_id, status="AMBIGUOUS", action_state="AMBIGUOUS",
                         current_step="manual_verification_required",
                         error_message=sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password()))


def _cleanup_target(db, operation_id: int) -> dict:
    if not context(db, operation_id).get('cleanup_at'):
        return validate_target(db, operation_id)
    # Scheduled cleanups may outlive further rotations. Protect today's primary,
    # and delete only the exact retained boxes approved in this operation.
    bound = bound_provider(operation_id)
    credentials = bound_credentials(db, bound)
    if (provider_name() != 'sprintbox' or credentials['login'] != bound.get('login')
            or hashlib.sha256(credentials['key'].encode()).hexdigest() != bound.get('credential_hash')):
        raise IpChangeError('SprintBOX cleanup account changed; inspect manually')
    def value(key):
        row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        return row['value'] if row else ''
    return {'new_ip': value('current_ip'),
            'new_ip_id': value('config.sprintbox_box_name'),
            'account_id': current_id(db)}


async def run_sprintbox_cleanup(operation_id: int, *, timing: IpRotationTiming | None = None,
                               sleeper: Sleeper = _sleep) -> None:
    timing = timing or IpRotationTiming.configured()
    bound = bound_provider(operation_id)
    credentials = {"key": "", "login": ""}
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
            credentials = bound_credentials(db, bound)
            active = _cleanup_target(db, operation_id)
        retained = operation_context.get("retained_boxes", {})
        results = operation_context.setdefault("cleanup_results", {})
        for ref, expected_ip in dict(retained).items():
            sending = False
            rejected = False
            owner_credentials = credentials
            name = ref
            try:
                with get_db() as db:
                    active = _cleanup_target(db, operation_id)
                    owner_credentials, name = box_owner(db, ref, credentials)
                    from app.anchors import protected_box
                    if protected_box(db, owner_credentials["login"], name, expected_ip):
                        results[ref] = {"state": "ANCHOR"}
                        continue
                if (expected_ip == active['new_ip'] or (name == active['new_ip_id'] and
                        owner_credentials.get('account_id', '') == active.get('account_id', bound.get('account_id', '')))):
                    raise IpChangeError("Refusing to delete the active VPN box")
                client = SprintboxClient(owner_credentials['login'], owner_credentials['key'], timeout=timing.provider_timeout)
                await _step(operation_id, "delete_old_box")
                if name not in await client.list_boxes():
                    results[ref] = {"state": "ABSENT"}
                    continue
                box = await client.get_box(name)
                if expected_ip not in box_ipv4(box):
                    raise IpChangeError("Box IP changed; inspect it manually")
                if ref in results and results[ref].get('state') != 'REJECTED':
                    raise IpChangeError("Deletion was already attempted; inspect it manually")
                # Overlapping nightly queues can contain the same retained box.
                # Never repeat an uncertain deletion sent by another operation.
                with get_db() as db:
                    for prior in db.execute("SELECT key,value FROM settings WHERE key LIKE 'ip_change.%.sprintbox'").fetchall():
                        if prior['key'] == f'ip_change.{operation_id}.sprintbox':
                            continue
                        prior_ctx = json.loads(prior['value'])
                        prior_state = prior_ctx.get('cleanup_results', {}).get(ref, {}).get('state')
                        if (prior_ctx.get('retained_boxes', {}).get(ref) == expected_ip
                                and prior_state in {'SENDING', 'SENT', 'AMBIGUOUS'}):
                            raise IpChangeError('Another deletion attempt is uncertain; inspect manually')
                results[ref] = {"state": "SENDING"}
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
                sending = True
                try:
                    job = await client.delete_box(name)
                except SprintboxApiError:
                    # An explicit rejection of this request is safe to retry in
                    # a later window. A lost response or failed task is not.
                    rejected = True
                    raise
                results[ref] = {"state": "SENT", "task": job}
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
                await _wait_task(client, job, operation_id, "wait_old_box_deletion", timing, sleeper)
                if name in await client.list_boxes():
                    raise IpChangeError("Delete task finished but box still exists")
                results[ref]["state"] = "DELETED"
            except Exception as exc:
                uncertain = sending and not isinstance(exc, SprintboxApiError)
                ambiguous = ambiguous or uncertain
                message = sanitize_error(exc, owner_credentials["key"], owner_credentials["login"], eu_ssh_password())
                results[ref] = {**results.get(ref, {}),
                                "state": "REJECTED" if rejected else "AMBIGUOUS" if uncertain else "MANUAL", "error": message}
                warnings.append(f"{name}: {message}")
            finally:
                if results.get(ref, {}).get("state") in {"DELETED", "ABSENT", "ANCHOR"}:
                    retained.pop(ref, None)
                set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        retry = bool(operation_context.get('cleanup_at') and retained and not ambiguous
                     and all(results.get(ref, {}).get('state') == 'REJECTED' for ref in retained))
        if retry:
            from app.scheduled_cleanup import next_cleanup
            attempt_at = operation_context.get('cleanup_attempt_at') or now_iso()
            operation_context['cleanup_at'] = next_cleanup(datetime.fromisoformat(attempt_at))
            set_setting(f"ip_change.{operation_id}.sprintbox", json.dumps(operation_context))
        warning = (("SprintBOX отклонил удаление; повторим в следующее окно: " if retry else
                    "Удалите или проверьте старые боксы вручную в кабинете SprintBOX: ")
                   + "; ".join(warnings)) if warnings else ""
        update_operation(operation_id, status="DONE", current_step="cleanup_scheduled" if retry else "done", error_message="",
                         action_state="CLEANUP_SCHEDULED" if retry else "CLEANUP_AMBIGUOUS" if ambiguous else "CLEANUP_REJECTED" if warnings else "DONE",
                         cleanup_warning=warning)
    except Exception as exc:
        message = sanitize_error(exc, credentials["key"], credentials["login"], eu_ssh_password())
        update_operation(operation_id, status="DONE", current_step="manual_verification_required", action_state="CLEANUP_AMBIGUOUS",
                         cleanup_warning=f"Проверьте старые боксы вручную в кабинете SprintBOX: {message}")
