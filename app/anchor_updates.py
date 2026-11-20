"""Durable edits of one anchor; primary VPN snapshots are never modified."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import unicodedata
from dataclasses import replace

from app.db import get_db, now_iso
from app.anchors import _grants, device_edit_plan
from app.amnezia import generate_private_key, generate_public_key, generate_preshared_key
from app.amnezia_devices import peer_script
from app.operation_coordinator import acquire_vps_lease, release_vps_lease
from app.vpn_config import capture_vpn_config, captured_vpn_config_from_json, canonical_vpn_config_json, AmneziaDevice
from app.sprintbox_accounts import account
from app.sprintbox import SprintboxClient, box_ipv4
from app.sprintbox_ssh import managed_key_path
from app.ssh_credentials import SshCredentials, use_ssh_credentials
from app.eu_install import build_eu_config_apply_script, _run_deploy_with_heartbeat


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _target(db, anchor_id):
    row = db.execute('SELECT * FROM vpn_anchors WHERE anchor_id=?', (anchor_id,)).fetchone()
    if not row or not row['payload_json']:
        raise ValueError('У якоря нет сохранённой конфигурации')
    config = captured_vpn_config_from_json(row['payload_json'])
    primary = db.execute("SELECT value FROM settings WHERE key='current_ip'").fetchone()
    if config.current_ip != row['ip'] or (primary and primary['value'] == row['ip']):
        raise ValueError('Этот сервер больше не является отдельным якорем')
    owner_id = row['account_id']
    if not owner_id:
        matches = db.execute('SELECT id FROM sprintbox_accounts WHERE login=?', (row['account_login'],)).fetchall()
        if len(matches) != 1:
            raise ValueError('Для изменения якоря нужен его аккаунт SprintBOX')
        owner_id = matches[0]['id']
    owner = account(db, owner_id)
    if owner['login'] != row['account_login']:
        raise ValueError('Аккаунт якоря изменился')
    return row, config, owner


def _address(config):
    network = ipaddress.ip_network(config.amnezia.network_prefix + '.0/24')
    used = {client.amnezia_ipv4 for client in config.clients}
    used.update(device.ipv4 for device in config.amnezia_devices)
    used.update(peer.ipv4 for peer in config.router_amnezia_peers)
    for address in list(network.hosts())[1:]:
        if str(address) not in used:
            return str(address)
    raise ValueError('Нет свободных адресов на якоре')


def _binding(db, operation_id):
    row = db.execute('SELECT value FROM settings WHERE key=?', (f'install.{operation_id}.anchor_edit',)).fetchone()
    return json.loads(row['value']) if row else None


def edit_state(anchor_id):
    with get_db() as db:
        row = db.execute('SELECT maintenance_operation_id FROM vpn_anchors WHERE anchor_id=?', (anchor_id,)).fetchone()
        if not row or not row['maintenance_operation_id']:
            return None
        plan = _binding(db, row['maintenance_operation_id'])
        credential_client_id = None
        if not plan:
            rotation = db.execute('SELECT value FROM settings WHERE key=?', (f"install.{row['maintenance_operation_id']}.credential_rotation",)).fetchone()
            rotation = json.loads(rotation['value']) if rotation else None
            if rotation and any(a['anchor_id'] == anchor_id for a in rotation['anchors']):
                credential_client_id = rotation['client_id']
        if (not plan or plan['anchor_id'] != anchor_id) and credential_client_id is None:
            return None
        operation = db.execute('SELECT id,status,error_message,updated_at FROM vpn_install_operations WHERE id=?', (row['maintenance_operation_id'],)).fetchone()
        return {**operation, "credential_client_id": credential_client_id} if operation else None


def _queue(db, row, owner, planned, *, grant_ids=(), peer=None, delete=False):
    now = now_iso()
    operation_id = int(db.execute("INSERT INTO vpn_install_operations(status,target_host,current_step,created_at,updated_at) VALUES ('PENDING',?,'anchor_edit',?,?)", (row['ip'], now, now)).lastrowid)
    acquire_vps_lease(db, 'INSTALL', operation_id)
    plan = {'anchor_id': row['anchor_id'], 'base_hash': digest(row['payload_json']),
            'ip': row['ip'], 'box': row['box_name'], 'account_id': owner['id'],
            'credential_hash': digest(owner['key']), 'login': owner['login'],
            'payload': canonical_vpn_config_json(planned), 'grant_ids': list(grant_ids),
            'peer': peer, 'delete': delete}
    db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (f'install.{operation_id}.anchor_edit', json.dumps(plan)))
    db.execute('UPDATE vpn_anchors SET maintenance_operation_id=? WHERE anchor_id=?', (operation_id, row['anchor_id']))
    return operation_id


def prepare_users(anchor_id, client_ids):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row, config, owner = _target(db, anchor_id)
        if row['maintenance_operation_id']:
            raise ValueError('Дождитесь изменения якоря или повторите неудавшуюся операцию')
        selected = set(client_ids)
        if not selected:
            raise ValueError('Выберите пользователей')
        current = capture_vpn_config(db)
        people = {client.id: client for client in current.enabled_clients}
        if not selected <= people.keys():
            raise ValueError('Пользователь отключён или удалён')
        installed = {client.id: client for client in config.clients}
        added = False
        for client_id in sorted(selected):
            person = people[client_id]
            if client_id in installed:
                if installed[client_id].token != person.token:
                    raise ValueError('Идентичность пользователя изменилась')
                continue
            added = True
            private = generate_private_key()
            person = replace(person, amnezia_ipv4=_address(config), amnezia_private_key=private,
                             amnezia_public_key=generate_public_key(private), amnezia_preshared_key=generate_preshared_key())
            config = replace(config, clients=config.clients + (person,))
        if not added:
            grants = _grants(db, anchor_id) | selected
            db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f'anchor.{anchor_id}.clients', json.dumps(sorted(grants))))
            return None
        return _queue(db, row, owner, config, grant_ids=sorted(selected))


def prepare_anchor_device(anchor_id, token, *, name=None, delete_id=None, admin=False):
    if name is not None:
        name = name.strip()
        if not 1 <= len(name) <= 40 or name.casefold() == 'главный' or any(unicodedata.category(c).startswith('C') for c in name):
            raise ValueError('Укажите название от 1 до 40 символов, кроме «Главный»')
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row, config, owner = _target(db, anchor_id)
        live = db.execute('SELECT id FROM clients WHERE token=? AND enabled=1 AND deleted_at IS NULL', (token,)).fetchone()
        person = config.client_by_token(token)
        if not live or not person or not person.enabled or person.id != live['id'] or (not admin and person.id not in _grants(db, anchor_id)):
            raise ValueError('Нет доступа к этому якорю')
        if row['maintenance_operation_id'] or not config.amnezia.protocol.enabled:
            raise ValueError('Изменение устройств сейчас недоступно')
        devices = config.amnezia_devices
        own = [device for device in devices if device.client_id == person.id]
        if delete_id is not None:
            device = next((device for device in own if device.id == delete_id), None)
            if not device:
                raise ValueError('Устройство не найдено')
            planned = replace(config, amnezia_devices=tuple(device for device in devices if device.id != delete_id))
        else:
            existing = next((device for device in own if device.name.casefold() == name.casefold()), None)
            if existing:
                return existing.id, None
            if len(own) >= 16:
                raise ValueError('Достигнут лимит устройств')
            private = generate_private_key()
            device = AmneziaDevice(-(secrets.randbits(53) + 1), person.id, name, private, generate_public_key(private), generate_preshared_key(), _address(config))
            planned = replace(config, amnezia_devices=devices + (device,))
        peer = {'public_key': device.public_key, 'preshared_key': device.preshared_key, 'ipv4': device.ipv4}
        return device.id, _queue(db, row, owner, planned, peer=peer, delete=delete_id is not None)


def _authorize_device_retry(db, row, config, plan, token, admin):
    if token is None:
        return
    live = db.execute('SELECT id FROM clients WHERE token=? AND enabled=1 AND deleted_at IS NULL', (token,)).fetchone()
    person = config.client_by_token(token)
    if not live or not person or not person.enabled or live['id'] != person.id or (not admin and person.id not in _grants(db, row['anchor_id'])):
        raise ValueError('Нет доступа к этому якорю')
    if not device_edit_plan(db, row) or plan.get('grant_ids'):
        raise ValueError('Это изменение может повторить только администратор')
    planned = captured_vpn_config_from_json(plan['payload'])
    devices = config.amnezia_devices + planned.amnezia_devices
    if not any(d.client_id == person.id and d.public_key == plan['peer']['public_key'] for d in devices):
        raise ValueError('Это изменение принадлежит другому пользователю')


def can_retry_device(anchor_id, token, *, admin=False):
    try:
        with get_db() as db:
            row, config, _ = _target(db, anchor_id)
            plan = device_edit_plan(db, row)
            if not plan:
                return False
            _authorize_device_retry(db, row, config, plan, token, admin)
            return True
    except ValueError:
        return False


def retry_edit(anchor_id, *, token=None, admin=False):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row, config, owner = _target(db, anchor_id)
        plan = _binding(db, row['maintenance_operation_id']) if row['maintenance_operation_id'] else None
        old = db.execute('SELECT status FROM vpn_install_operations WHERE id=?', (row['maintenance_operation_id'],)).fetchone()
        if not plan or plan['anchor_id'] != anchor_id or not old or old['status'] not in {'FAILED', 'AMBIGUOUS', 'TIMED_OUT'}:
            raise ValueError('Нет изменения якоря для повторения')
        _authorize_device_retry(db, row, config, plan, token, admin)
        _validate(db, row['maintenance_operation_id'], plan, allow_rotated_key=True)
        return _queue(db, row, owner, captured_vpn_config_from_json(plan['payload']), grant_ids=plan['grant_ids'], peer=plan['peer'], delete=plan['delete'])



def restore_edit(anchor_id):
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row, confirmed, owner = _target(db, anchor_id)
        plan = _binding(db, row['maintenance_operation_id']) if row['maintenance_operation_id'] else None
        old = db.execute('SELECT status FROM vpn_install_operations WHERE id=?', (row['maintenance_operation_id'],)).fetchone()
        if not plan or plan['anchor_id'] != anchor_id or not old or old['status'] not in {'FAILED', 'AMBIGUOUS', 'TIMED_OUT'}:
            raise ValueError('Нет неудавшегося изменения для восстановления')
        if digest(row['payload_json']) != plan['base_hash']:
            raise ValueError('Подтверждённая конфигурация якоря изменилась')
        return _queue(db, row, owner, confirmed)


def _validate(db, operation_id, plan, *, allow_rotated_key=False):
    row, _, owner = _target(db, plan['anchor_id'])
    if (row['maintenance_operation_id'] != operation_id or row['ip'] != plan['ip'] or row['box_name'] != plan['box']
            or digest(row['payload_json']) != plan['base_hash'] or owner['id'] != plan['account_id']
            or owner['login'] != plan['login'] or (not allow_rotated_key and digest(owner['key']) != plan['credential_hash'])):
        raise ValueError('Якорь или его аккаунт изменился; нужна проверка администратора')
    planned = captured_vpn_config_from_json(plan['payload'])
    for client_id in plan['grant_ids']:
        person = next(client for client in planned.clients if client.id == client_id)
        live = db.execute('SELECT token,enabled,deleted_at FROM clients WHERE id=?', (client_id,)).fetchone()
        if not live or not live['enabled'] or live['deleted_at'] or live['token'] != person.token:
            raise ValueError('Пользователь изменился или отключён')
    return owner


async def run_edit(operation_id):
    with get_db() as db:
        if not db.execute("UPDATE vpn_install_operations SET status='RUNNING',updated_at=? WHERE id=? AND status='PENDING'", (now_iso(), operation_id)).rowcount:
            return
    failure = 'Не удалось проверить сервер якоря у провайдера.'
    try:
        with get_db() as db:
            plan = _binding(db, operation_id)
            owner = _validate(db, operation_id, plan)
        box = await SprintboxClient(owner['login'], owner['key']).get_box(plan['box'])
        failure = 'IP якоря у провайдера изменился. Нужна проверка администратора.'
        if box_ipv4(box) != [plan['ip']]:
            raise ValueError('IP якоря у провайдера изменился')
        key = managed_key_path()
        failure = 'Нет исходного SSH-ключа SprintBOX. Нужна помощь администратора.'
        if not key.is_file():
            raise ValueError('Нет исходного SSH-ключа SprintBOX')
        config = captured_vpn_config_from_json(plan['payload'])
        script = peer_script(plan['peer'], plan['delete'], config.amnezia.server_public_key) if plan['peer'] else build_eu_config_apply_script(config)
        script = 'set -euo pipefail\nexec 8>/run/autovpn-anchor-edit.lock\nflock -x -w 20 8\n' + script
        with get_db() as db:
            _validate(db, operation_id, plan)
        failure = 'Не удалось применить изменение по SSH (порт 22): сервер недоступен, доступ отклонён или выполнение не подтверждено. Проверьте SSH-доступ и повторите попытку.'
        with use_ssh_credentials(SshCredentials('root', 22, str(key))):
            code, _ = await _run_deploy_with_heartbeat(operation_id, plan['ip'], script, timeout=45 if plan['peer'] else 300, heartbeat_interval=15)
        if code:
            raise RuntimeError('Anchor apply failed')
        failure = 'Сервер ответил, но сохранение изменения не подтверждено. Повторите применение.'
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            _validate(db, operation_id, plan)
            db.execute('UPDATE vpn_anchors SET payload_json=?,maintenance_operation_id=NULL WHERE anchor_id=?', (plan['payload'], plan['anchor_id']))
            grants = _grants(db, plan['anchor_id']) | set(plan['grant_ids'])
            db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (f"anchor.{plan['anchor_id']}.clients", json.dumps(sorted(grants))))
            db.execute("UPDATE vpn_install_operations SET status='DONE',current_step='done',updated_at=?,error_message=NULL WHERE id=?", (now_iso(), operation_id))
            release_vps_lease(db, 'INSTALL', operation_id)
    except Exception:
        with get_db() as db:
            db.execute("UPDATE vpn_install_operations SET status='AMBIGUOUS',current_step='anchor_edit_failed',updated_at=?,error_message=? WHERE id=?", (now_iso(), failure, operation_id))
            release_vps_lease(db, 'INSTALL', operation_id)
