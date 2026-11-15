"""Frozen VPN endpoints. Saving an anchor never changes its server or primary VPN."""
from __future__ import annotations

import json
import secrets

from app.db import get_db, now_iso
from app.vpn_config import captured_vpn_config_from_json


def protected_box(db, login: str, name: str, ip: str) -> bool:
    # The name alone is protected too: an address change must require manual review.
    return db.execute(
        "SELECT 1 FROM vpn_anchors WHERE account_login = ? AND (box_name = ? OR (ip != '' AND ip = ?))",
        (login, name, ip),
    ).fetchone() is not None


def applied_endpoint(db, ip: str) -> int | None:
    row = db.execute("SELECT snapshot.revision, snapshot.payload_json FROM vpn_state AS state "
                     "JOIN vpn_snapshots AS snapshot ON snapshot.revision = state.applied_revision "
                     "WHERE state.singleton = 1 AND snapshot.lifecycle = 'APPLIED'").fetchone()
    if row and captured_vpn_config_from_json(row["payload_json"]).current_ip == ip:
        return int(row["revision"])
    return None


def save_anchor(db, login: str, name: str, ip: str, revision: int | None, account_id: str | int | None = None) -> str:
    current = db.execute("SELECT value FROM settings WHERE key = 'current_ip'").fetchone()
    if current and current["value"] == ip:
        raise ValueError("Сначала смените основной IP, затем сохраните прежний бокс как якорный")
    row = db.execute("SELECT payload_json FROM vpn_snapshots WHERE revision = ? AND lifecycle = 'APPLIED'",
                     (revision,)).fetchone() if revision is not None else None
    if revision is not None and (not row or captured_vpn_config_from_json(row["payload_json"]).current_ip != ip):
        raise ValueError("Для этого бокса нет подтверждённой применённой конфигурации")
    anchor_id = secrets.token_urlsafe(16)
    db.execute("INSERT INTO vpn_anchors(anchor_id,account_login,box_name,ip,payload_json,created_at,account_id) "
               "VALUES (?,?,?,?,?,?,?) ON CONFLICT(account_login,box_name,ip) DO NOTHING",
               (anchor_id, login, name, ip, row["payload_json"] if row else None, now_iso(), account_id))
    return db.execute("SELECT anchor_id FROM vpn_anchors WHERE account_login=? AND box_name=? AND ip=?",
                      (login, name, ip)).fetchone()["anchor_id"]


def list_anchors(token: str | None = None) -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT a.*, accounts.label AS account_label FROM vpn_anchors a "
                          "LEFT JOIN sprintbox_accounts accounts ON accounts.id=a.account_id "
                          "ORDER BY a.created_at DESC, a.anchor_id").fetchall()
        live_clients = {row["token"]: row["id"] for row in db.execute(
            "SELECT id,token FROM clients WHERE enabled=1 AND deleted_at IS NULL"
        ).fetchall()}
        result = []
        for row in rows:
            payload = row.pop("payload_json")
            config = captured_vpn_config_from_json(payload) if payload else None
            row["has_config"] = config is not None
            row["can_promote"] = config is not None and bool(row.get("account_id"))
            row["ready"] = config is not None and not row.get("maintenance_operation_id")
            clients = [client for client in config.enabled_clients
                       if live_clients.get(client.token) == client.id and (token is None or token == client.token)] if config and (token is None or row["ready"]) else []
            if token is not None and not clients:
                continue
            # No server keys or other clients' credentials in template context.
            grants = _grants(db, row['anchor_id'])
            if token is not None and not any(client.id in grants for client in clients):
                continue
            row["clients"] = [{"id": client.id, "name": client.name, "token": client.token,
                               "granted": client.id in grants} for client in clients]
            result.append(row)
        return result


def anchor_client(anchor_id: str, token: str):
    with get_db() as db:
        live = db.execute("SELECT id FROM clients WHERE token=? AND enabled=1 AND deleted_at IS NULL",
                          (token,)).fetchone()
        row = db.execute("SELECT * FROM vpn_anchors WHERE anchor_id=?", (anchor_id,)).fetchone()
    if not live or not row or not row["payload_json"] or row.get("maintenance_operation_id"):
        return None
    config = captured_vpn_config_from_json(row.pop("payload_json"))
    client = config.client_by_token(token)
    if not client or not client.enabled or client.id != live["id"]:
        return None
    return row, config, client


def retain_as_anchors(db, operation_id: int, selected: list[str]) -> None:
    from app.sprintbox_review import context, _bound

    ctx = context(db, operation_id)
    retained = ctx.get("retained_boxes", {})
    revisions = ctx.get("retained_snapshots", {})
    from app.sprintbox_accounts import box_owner, config_for
    fallback = config_for(db, _bound(db, operation_id).get("account_id", ""))
    for name in set(selected):
        if name not in retained:
            raise ValueError("Бокс не принадлежит этой операции")
        revision = revisions.get(name)
        owner, box_name = box_owner(db, name, fallback)
        save_anchor(db, owner["login"], box_name, retained[name], int(revision) if revision is not None else None,
                    owner.get("account_id"))
        retained.pop(name)
    db.execute("UPDATE settings SET value=? WHERE key=?",
               (json.dumps(ctx), f"ip_change.{operation_id}.sprintbox"))


def _grants(db, anchor_id):
    row = db.execute('SELECT value FROM settings WHERE key=?', (f'anchor.{anchor_id}.clients',)).fetchone()
    return set(json.loads(row['value'])) if row else set()


def has_anchor_access(anchor_id, token):
    with get_db() as db:
        client = db.execute('SELECT id FROM clients WHERE token=? AND enabled=1 AND deleted_at IS NULL', (token,)).fetchone()
        return bool(client and client['id'] in _grants(db, anchor_id))


def set_anchor_access(anchor_id, client_ids):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        anchor = db.execute('SELECT payload_json,maintenance_operation_id FROM vpn_anchors WHERE anchor_id=?', (anchor_id,)).fetchone()
        if not anchor or not anchor['payload_json']:
            raise ValueError('Якорь недоступен для выдачи доступа')
        config = captured_vpn_config_from_json(anchor['payload_json'])
        live = {row['id']: row['token'] for row in db.execute('SELECT id,token FROM clients WHERE enabled=1 AND deleted_at IS NULL')}
        eligible = {client.id for client in config.enabled_clients if live.get(client.id) == client.token}
        selected = set(client_ids)
        if not selected <= eligible:
            raise ValueError('Выбранный пользователь отсутствует в конфигурации якоря или отключён')
        db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (f'anchor.{anchor_id}.clients', json.dumps(sorted(selected))))
