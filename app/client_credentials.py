"""Revocable export links and durable credential replacement across VPN servers."""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import replace

from app.db import get_db, now_iso, _set_setting, _mark_vpn_config_updated
from app.amnezia import generate_private_key, generate_public_key, generate_preshared_key
from app.vpn_config import capture_vpn_config, captured_vpn_config_from_json, canonical_vpn_config_json
from app.vpn_state import (_snapshot_payload, _ensure_snapshot, load_install_snapshot,
                           complete_install_operation, mark_install_operation_ambiguous)
from app.operation_coordinator import acquire_vps_lease
from app import anchor_updates
from app.eu_install import build_eu_config_apply_script, _run_deploy_with_heartbeat, resolve_eu_host
from app.sprintbox import SprintboxClient, box_ipv4
from app.sprintbox_ssh import managed_key_path
from app.ssh_credentials import SshCredentials, use_ssh_credentials


def access_token(client_id, *, db=None):
    if db is None:
        with get_db() as connection:
            return access_token(client_id, db=connection)
    row = db.execute('SELECT c.token,s.value FROM clients c LEFT JOIN settings s ON s.key=? WHERE c.id=? AND c.deleted_at IS NULL',
                     (f'client.{client_id}.access_token', client_id)).fetchone()
    return (row['value'] if row['value'] is not None else row['token']) if row else ''


def page_token_for_access(token):
    if not token:
        return None
    with get_db() as db:
        row = db.execute("SELECT c.token FROM clients c LEFT JOIN settings s ON s.key='client.' || c.id || '.access_token' "
                         'WHERE COALESCE(s.value,c.token)=? AND c.enabled=1 AND c.deleted_at IS NULL', (token,)).fetchone()
    return row['token'] if row else None


def _plan(db, client_id):
    pointer = db.execute('SELECT value FROM settings WHERE key=?', (f'client.{client_id}.credential_rotation',)).fetchone()
    row = db.execute('SELECT value FROM settings WHERE key=?', (f'install.{pointer["value"]}.credential_rotation',)).fetchone() if pointer else None
    return json.loads(row['value']) if row else None


def _save(db, plan):
    _set_setting(db, f'install.{plan["operation_id"]}.credential_rotation', json.dumps(plan))
    _set_setting(db, f'client.{plan["client_id"]}.credential_rotation', str(plan['operation_id']))


def state(client_id):
    with get_db() as db:
        plan = _plan(db, client_id)
        if not plan:
            return None
        operation = db.execute('SELECT status,error_message FROM vpn_install_operations WHERE id=?', (plan['operation_id'],)).fetchone()
        operation = operation or {'status': 'DONE' if plan.get('completed') else 'AMBIGUOUS', 'error_message': 'Журнал операции удалён. Нужна проверка администратора.'}
        return {**operation, 'operation_id': plan['operation_id'], 'revoked': plan['revoked'],
                'targets': [{'name': 'Основной VPN', 'done': plan['primary_done']}] +
                           [{'name': f"Якорь {a['ip']}", 'done': a['done']} for a in plan['anchors']]}


def exports_available(client_id):
    current = state(client_id)
    return not current or current['status'] == 'DONE'


def export_client(config, token):
    page = page_token_for_access(token)
    client = config.client_by_token(page) if page else None
    if not client or not client.enabled or not exports_available(client.id):
        return None
    return replace(client, token=token)


def _keys():
    private = generate_private_key()
    return private, generate_public_key(private), generate_preshared_key()


def _rotate_config(config, client_id, page_token):
    private, public, psk = _keys()
    clients = tuple(replace(c, token=page_token, vless_uuid=str(uuid.uuid4()),
                            hysteria_password=secrets.token_urlsafe(24), amnezia_private_key=private,
                            amnezia_public_key=public, amnezia_preshared_key=psk) if c.id == client_id else c
                    for c in config.clients)
    devices = []
    for device in config.amnezia_devices:
        if device.client_id == client_id:
            private, public, psk = _keys()
            device = replace(device, private_key=private, public_key=public, preshared_key=psk)
        devices.append(device)
    return replace(config, clients=clients, amnezia_devices=tuple(devices),
                   hysteria=replace(config.hysteria, auth_type='userpass'))


def _queue_primary(db):
    config, payload, digest = _snapshot_payload(db)
    if not config.current_ip or not resolve_eu_host():
        raise ValueError('Основной сервер не настроен')
    now = now_iso()
    _ensure_snapshot(db, config, payload, digest, now, reprepare_failed=True)
    operation = int(db.execute("INSERT INTO vpn_install_operations(status,target_host,current_step,revision,created_at,updated_at) VALUES ('PENDING',?,'credential_rotation',?,?,?)",
                               (resolve_eu_host(), config.revision, now, now)).lastrowid)
    acquire_vps_lease(db, 'INSTALL', operation)
    return operation


def prepare(client_id, *, rotate_page=False, revoke=False):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        person = db.execute('SELECT * FROM clients WHERE id=? AND deleted_at IS NULL AND enabled=1', (client_id,)).fetchone()
        if not person:
            raise ValueError('Пользователь отключён или удалён')
        if db.execute('SELECT 1 FROM routers WHERE client_id=?', (client_id,)).fetchone():
            raise ValueError('Ключи роутера меняются в разделе роутеров')
        previous = _plan(db, client_id)
        if previous:
            old = db.execute('SELECT status FROM vpn_install_operations WHERE id=?', (previous['operation_id'],)).fetchone()
            if (old and old['status'] != 'DONE') or (not old and not previous.get('completed')):
                raise ValueError('Сначала завершите предыдущий отзыв доступа кнопкой повторного применения')
        current = capture_vpn_config(db)
        applied = db.execute('SELECT applied_revision FROM vpn_state WHERE singleton=1').fetchone()
        if not applied or applied['applied_revision'] != current.revision:
            raise ValueError('Сначала синхронизируйте текущие настройки основного VPN')
        snapshot = db.execute('SELECT payload_json FROM vpn_snapshots WHERE revision=?', (applied['applied_revision'],)).fetchone()
        if not snapshot or captured_vpn_config_from_json(snapshot['payload_json']).current_ip != current.current_ip:
            raise ValueError('Основной VPN ещё не подтверждён')
        page = secrets.token_urlsafe(32) if rotate_page else person['token']
        anchors = []
        for row in db.execute('SELECT * FROM vpn_anchors WHERE payload_json IS NOT NULL').fetchall():
            config = captured_vpn_config_from_json(row['payload_json'])
            if not any(c.id == client_id for c in config.clients):
                continue
            row, config, owner = anchor_updates._target(db, row['anchor_id'])
            if row['maintenance_operation_id']:
                raise ValueError('Дождитесь завершения изменения якоря')
            planned = _rotate_config(config, client_id, page)
            anchors.append({'anchor_id': row['anchor_id'], 'ip': row['ip'], 'box': row['box_name'],
                            'account_id': owner['id'], 'login': owner['login'],
                            'base_hash': anchor_updates.digest(row['payload_json']),
                            'payload': canonical_vpn_config_json(planned), 'done': False})
        planned = _rotate_config(current, client_id, page)
        updated = next(c for c in planned.clients if c.id == client_id)
        db.execute('UPDATE clients SET token=?,vless_uuid=?,hysteria_password=?,amnezia_private_key=?,amnezia_public_key=?,amnezia_preshared_key=? WHERE id=?',
                   (page, updated.vless_uuid, updated.hysteria_password, updated.amnezia_private_key, updated.amnezia_public_key, updated.amnezia_preshared_key, client_id))
        for d in planned.amnezia_devices:
            if d.client_id == client_id:
                db.execute('UPDATE amnezia_devices SET private_key=?,public_key=?,preshared_key=? WHERE id=?', (d.private_key, d.public_key, d.preshared_key, d.id))
        if rotate_page:
            db.execute("UPDATE push_deliveries SET state='SKIPPED' WHERE subscription_id IN (SELECT id FROM push_subscriptions WHERE client_id=?) AND state IN ('PENDING','INFLIGHT')", (client_id,))
            db.execute('DELETE FROM push_subscriptions WHERE client_id=?', (client_id,))
        _set_setting(db, f'client.{client_id}.access_token', '' if revoke else secrets.token_urlsafe(32))
        _mark_vpn_config_updated(db, public_change=True)
        operation = _queue_primary(db)
        plan = {'operation_id': operation, 'client_id': client_id, 'page_token': page,
                'revoked': revoke, 'primary_ip': current.current_ip, 'primary_done': False, 'anchors': anchors}
        for a in anchors:
            db.execute('UPDATE vpn_anchors SET maintenance_operation_id=? WHERE anchor_id=?', (operation, a['anchor_id']))
        _save(db, plan)
        return operation, page


def retry(client_id):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        plan = _plan(db, client_id)
        if not plan:
            raise ValueError('Нет отзыва для повторения')
        old = db.execute('SELECT status FROM vpn_install_operations WHERE id=?', (plan['operation_id'],)).fetchone()
        if not old or old['status'] not in {'FAILED', 'AMBIGUOUS', 'TIMED_OUT'}:
            raise ValueError('Операция ещё выполняется или уже завершена')
        person = db.execute('SELECT token FROM clients WHERE id=? AND enabled=1 AND deleted_at IS NULL', (client_id,)).fetchone()
        if not person or person['token'] != plan['page_token']:
            raise ValueError('Пользователь изменился или отключён')
        if capture_vpn_config(db).current_ip != plan['primary_ip']:
            raise ValueError('Основной IP изменился. Нужна проверка администратора')
        for a in plan['anchors']:
            if not a['done']:
                _validate_anchor(db, plan, a)
        operation = _queue_primary(db)
        plan['operation_id'] = operation
        plan['primary_done'] = False
        for a in plan['anchors']:
            if not a['done']:
                db.execute('UPDATE vpn_anchors SET maintenance_operation_id=? WHERE anchor_id=?', (operation, a['anchor_id']))
        _save(db, plan)
        return operation


def _assert_current(db, plan):
    current = _plan(db, plan['client_id'])
    operation = db.execute('SELECT status FROM vpn_install_operations WHERE id=?', (plan['operation_id'],)).fetchone()
    lease = db.execute("SELECT 1 FROM operation_leases WHERE resource='vpn_vps' AND owner_type='INSTALL' AND owner_id=?", (plan['operation_id'],)).fetchone()
    if not current or current['operation_id'] != plan['operation_id'] or not operation or operation['status'] != 'RUNNING' or not lease:
        raise ValueError('Операция прервана или заменена')


def _validate_anchor(db, plan, anchor):
    row, _, owner = anchor_updates._target(db, anchor['anchor_id'])
    if (row['maintenance_operation_id'] != plan['operation_id'] or row['ip'] != anchor['ip']
            or row['box_name'] != anchor['box'] or owner['id'] != anchor['account_id']
            or owner['login'] != anchor['login'] or anchor_updates.digest(row['payload_json']) != anchor['base_hash']):
        raise ValueError('Якорь изменился. Нужна проверка администратора')
    return owner


async def run(operation_id):
    with get_db() as db:
        if not db.execute("UPDATE vpn_install_operations SET status='RUNNING',updated_at=? WHERE id=? AND status='PENDING'", (now_iso(), operation_id)).rowcount:
            return
        row = db.execute('SELECT value FROM settings WHERE key=?', (f'install.{operation_id}.credential_rotation',)).fetchone()
        plan = json.loads(row['value'])
    failure = 'Отзыв на основном VPN не подтверждён. Проверьте SSH-доступ и повторите применение.'
    try:
        operation, config = load_install_snapshot(operation_id)
        with get_db() as db:
            _assert_current(db, plan)
        code, _ = await _run_deploy_with_heartbeat(operation_id, operation['target_host'], build_eu_config_apply_script(config), timeout=300, heartbeat_interval=15)
        if code:
            raise RuntimeError('Primary credential replacement failed')
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            _assert_current(db, plan)
            plan['primary_done'] = True
            _save(db, plan)
        for a in plan['anchors']:
            if a['done']:
                continue
            failure = f"Отзыв на якоре {a['ip']} не подтверждён. Проверьте SSH-доступ и повторите применение."
            with get_db() as db:
                _assert_current(db, plan)
                owner = _validate_anchor(db, plan, a)
            box = await SprintboxClient(owner['login'], owner['key']).get_box(a['box'])
            if box_ipv4(box) != [a['ip']]:
                raise ValueError('Anchor IP changed')
            key = managed_key_path()
            if not key.is_file():
                raise ValueError('SSH key missing')
            with use_ssh_credentials(SshCredentials('root', 22, str(key))):
                code, _ = await _run_deploy_with_heartbeat(operation_id, a['ip'], 'set -euo pipefail\nexec 8>/run/autovpn-anchor-edit.lock\nflock -x -w 20 8\n' + build_eu_config_apply_script(captured_vpn_config_from_json(a['payload'])), timeout=300, heartbeat_interval=15)
            if code:
                raise RuntimeError('Anchor credential replacement failed')
            with get_db() as db:
                db.execute('BEGIN IMMEDIATE')
                _assert_current(db, plan)
                _validate_anchor(db, plan, a)
                db.execute('UPDATE vpn_anchors SET payload_json=?,maintenance_operation_id=NULL WHERE anchor_id=?', (a['payload'], a['anchor_id']))
                a['done'] = True
                _save(db, plan)
        with get_db() as db:
            _assert_current(db, plan)
        complete_install_operation(operation_id, 'Новые ключи применены на основном VPN и всех сохранённых якорях пользователя. Старые ключи отозваны.')
        with get_db() as db:
            plan['completed'] = True
            _save(db, plan)
    except Exception:
        mark_install_operation_ambiguous(operation_id, failure)
