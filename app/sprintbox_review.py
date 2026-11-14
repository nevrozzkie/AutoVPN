"""Durable user decision between trying another IP and deleting retained boxes."""
from __future__ import annotations

import hashlib
import json

from app.config import settings
from app.db import get_db, now_iso
from app.operation_coordinator import acquire_vps_lease
from app.sprintbox_accounts import (config_for, current_id, selected_id, box_ref, box_owner)


def _value(db, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (f"config.{key}",)).fetchone()
    if row and key == "sprintbox_box_name":
        return str(row["value"])
    return str(row["value"] if row and row["value"] else getattr(settings, key, ""))


def context(db, operation_id: int) -> dict:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (f"ip_change.{operation_id}.sprintbox",)).fetchone()
    return json.loads(row["value"]) if row else {}


def _bound(db, operation_id: int) -> dict:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (f"ip_change.{operation_id}.provider",)).fetchone()
    return json.loads(row["value"]) if row else {}


def validate_target(db, operation_id: int) -> dict:
    row = db.execute("SELECT * FROM ip_change_operations WHERE id = ?", (operation_id,)).fetchone()
    bound = _bound(db, operation_id)
    current = db.execute("SELECT value FROM settings WHERE key = 'current_ip'").fetchone()
    cfg = config_for(db, bound.get('account_id', ''))
    owner_matches = not bound.get('account_id') or current_id(db) == bound['account_id']
    if (not owner_matches or not row or bound.get("provider") != "sprintbox" or _value(db, "vps_provider") != "sprintbox"
            or not row["published_revision"] or not current or row["new_ip"] != current["value"]
            or row["new_ip_id"] != _value(db, "sprintbox_box_name")
            or bound.get("login") != cfg["login"]
            or bound.get("credential_hash") != hashlib.sha256(cfg["key"].encode()).hexdigest()):
        raise ValueError("This SprintBOX decision no longer matches the active VPN server/account")
    return row


def validate_review(db, operation_id: int) -> dict:
    row = validate_target(db, operation_id)
    if row["status"] != "DONE" or row["action_state"] != "AWAITING_USER_REVIEW":
        raise ValueError("This SprintBOX decision was already submitted")
    return row


def pending_review() -> dict | None:
    with get_db() as db:
        rows = db.execute("SELECT id FROM ip_change_operations WHERE status = 'DONE' AND action_state = 'AWAITING_USER_REVIEW' ORDER BY id DESC").fetchall()
        for row in rows:
            try:
                result = validate_review(db, row["id"])
                ctx = context(db, row["id"])
                from app.anchors import protected_box
                fallback = config_for(db, _bound(db, row['id']).get('account_id', ''))
                result['retained_boxes'] = {}
                result['retained_labels'] = {}
                for ref, ip in ctx.get('retained_boxes', {}).items():
                    owner, name = box_owner(db, ref, fallback)
                    if not protected_box(db, owner['login'], name, ip):
                        result['retained_boxes'][ref] = ip
                        result['retained_labels'][ref] = f"{owner.get('label', owner['login'])} · {name}"
                result["anchorable_boxes"] = ctx.get("retained_snapshots", {})
                return result
            except ValueError:
                continue
    return None


def retained_boxes(old_name: str, old_ip: str, bound: dict) -> dict[str, str]:
    """Only carry boxes from this AutoVPN chain, never all boxes on the account."""
    with get_db() as db:
        rows = db.execute(
            "SELECT id FROM ip_change_operations WHERE new_ip_id = ? AND new_ip = ? "
            "AND published_revision IS NOT NULL ORDER BY id DESC", (old_name, old_ip),
        ).fetchall()
        retained = {}
        for row in rows:
            previous = _bound(db, row["id"])
            matches = (previous.get('account_id') == bound.get('old_account_id')) if bound.get('account_id') else all(
                previous.get(key) == bound.get(key) for key in ('provider', 'login', 'credential_hash'))
            if matches:
                retained = dict(context(db, row["id"]).get("retained_boxes", {}))
                break
    retained[box_ref(bound.get("old_account_id", ""), old_name)] = old_ip
    return retained


def prepare_cleanup(operation_id: int, *, keep_boxes: list[str] | None = None) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        validate_review(db, operation_id)
        acquire_vps_lease(db, "IP_CHANGE", operation_id)
        if keep_boxes:
            from app.anchors import retain_as_anchors
            retain_as_anchors(db, operation_id, keep_boxes)
        db.execute(
            "UPDATE ip_change_operations SET status = 'PENDING', action_state = 'CLEANUP_QUEUED', "
            "current_step = 'cleanup_queued', updated_at = ? WHERE id = ?", (now_iso(), operation_id),
        )


def validate_ssh_decision(db, operation_id: int, *, queued: bool = False, host_key: bool = False) -> dict:
    row = db.execute("SELECT * FROM ip_change_operations WHERE id = ?", (operation_id,)).fetchone()
    expected = ("PENDING", "SSH_WAIT_QUEUED") if queued else ("DONE", "SSH_UNREACHABLE")
    if host_key:
        expected = ("AMBIGUOUS", "AMBIGUOUS")
    if not row or (row["status"], row["action_state"]) != expected:
        raise ValueError("This SSH decision was already submitted")
    if host_key and not (
        f"Host key verification failed for root@{row['new_ip']}:22: "
        in (row["error_message"] or "")
        and "the server presented a different SSH host key than the one previously pinned" in row["error_message"]
    ):
        raise ValueError("This operation has no recoverable SSH host key mismatch")
    bound = _bound(db, operation_id)
    current = db.execute("SELECT value FROM settings WHERE key = 'current_ip'").fetchone()
    revision = db.execute("SELECT desired_revision FROM vpn_state WHERE singleton = 1").fetchone()
    cfg = config_for(db, bound.get('account_id', ''))
    cfg['box_name'] = _value(db, 'sprintbox_box_name')
    cfg['login'] = cfg.get('login', '')
    owner_matches = not bound.get('account_id') or (
        selected_id(db) == bound['account_id'] and current_id(db) == bound['old_account_id'])
    if (not owner_matches or bound.get("provider") != "sprintbox" or _value(db, "vps_provider") != "sprintbox"
            or any(bound.get(key) != cfg[key]
                   for key in ("login", "box_name", "service", "distributive"))
            or bound.get("credential_hash") != hashlib.sha256(cfg["key"].encode()).hexdigest()
            or row["revision"] != revision["desired_revision"]
            or row["old_ip"] != (current["value"] if current else "")
            or row["published_revision"] or row["purchase_state"] != "CONFIRMED"
            or row["apply_state"] not in ({"NOT_STARTED", "RUNNING"} if host_key else {"NOT_STARTED"})
            or row["make_main_state"] != "NOT_STARTED"
            or not row["new_ip_id"] or not row["new_ip"]
            or row["new_ip_id"] == bound.get("box_name")):
        raise ValueError("VPN configuration/account changed; cancel this SSH decision first")
    return row


def pending_ssh_decision() -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT * FROM ip_change_operations WHERE status = 'DONE' "
                         "AND action_state = 'SSH_UNREACHABLE' ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            try:
                validate_ssh_decision(db, row["id"])
                row["stale"] = False
            except ValueError:
                row["stale"] = True
        return row


def prepare_ssh_wait(operation_id: int) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        validate_ssh_decision(db, operation_id)
        acquire_vps_lease(db, "IP_CHANGE", operation_id)
        db.execute("UPDATE ip_change_operations SET status = 'PENDING', action_state = 'SSH_WAIT_QUEUED', "
                   "current_step = 'wait_ssh', updated_at = ? WHERE id = ?", (now_iso(), operation_id))


def cancel_ssh_wait(operation_id: int) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM ip_change_operations WHERE id = ? AND status = 'DONE' "
                         "AND action_state = 'SSH_UNREACHABLE'", (operation_id,)).fetchone()
        if not row:
            raise ValueError("This SSH decision was already submitted")
        active = box_ref(current_id(db), _value(db, "sprintbox_box_name"))
        binding = _bound(db, operation_id)
        new_ref = box_ref(binding.get("account_id", ""), row["new_ip_id"])
        kept = set(context(db, operation_id).get("retained_boxes", {})) | {new_ref}
        names = ", ".join(sorted(name for name in kept if name and name != active))
        warning = f"Попытка отменена. Боксы сохранены; при необходимости удалите вручную в SprintBOX: {names}"
        db.execute("UPDATE ip_change_operations SET action_state = 'CANCELLED', current_step = 'cancelled', "
                   "error_message = '', cleanup_warning = ?, updated_at = ? WHERE id = ?",
                   (warning, now_iso(), operation_id))


def pending_host_key_retry() -> dict | None:
    with get_db() as db:
        row = db.execute("SELECT id FROM ip_change_operations ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            try:
                return validate_ssh_decision(db, row["id"], host_key=True)
            except ValueError:
                pass
    return None


def prepare_host_key_retry(operation_id: int) -> None:
    from app.eu_install import forget_ssh_known_host
    from app.ssh_credentials import SshCredentials, use_ssh_credentials
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = validate_ssh_decision(db, operation_id, host_key=True)
        latest = db.execute("SELECT id FROM ip_change_operations ORDER BY id DESC LIMIT 1").fetchone()
        if latest["id"] != operation_id:
            raise ValueError("A newer IP change operation exists")
        acquire_vps_lease(db, "IP_CHANGE", operation_id)
        # Host verification failed before any remote command could execute.
        # Keep strict verification: the next connection pins the replacement key.
        with use_ssh_credentials(SshCredentials("root", 22, "")):
            forget_ssh_known_host(row["new_ip"])
        db.execute("UPDATE ip_change_operations SET status = 'PENDING', action_state = 'SSH_WAIT_QUEUED', "
                   "apply_state = 'NOT_STARTED', current_step = 'wait_ssh', error_message = '', updated_at = ? "
                   "WHERE id = ?", (now_iso(), operation_id))
