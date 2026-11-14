"""Account credentials and immutable ownership of SprintBOX resources."""
from __future__ import annotations

import hashlib
import json
import sqlite3

from app.config import settings
from app.db import get_db, now_iso
from app.operation_coordinator import OperationBusyError, LeaseOwner


SELECTED = 'sprintbox.selected_account_id'
CURRENT = 'sprintbox.current_account_id'


def setting(db, key: str, default: str = '') -> str:
    row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return str(row['value']) if row else default


def put(db, key: str, value: str) -> None:
    db.execute('INSERT INTO settings(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, value))


def legacy_value(db, key: str) -> str:
    row = db.execute('SELECT value FROM settings WHERE key=?', ('config.sprintbox_' + key,)).fetchone()
    if row and (row['value'] or key == 'box_name'):
        return str(row['value'])
    return str(getattr(settings, 'sprintbox_' + key, ''))


def account(db, account_id: str | int) -> dict:
    row = db.execute('SELECT * FROM sprintbox_accounts WHERE id=?', (account_id,)).fetchone()
    if not row:
        raise ValueError('Аккаунт SprintBOX не найден')
    return {**row, 'account_id': str(row['id']), 'key': row['api_key']}


def selected_id(db) -> str:
    return setting(db, SELECTED)


def current_id(db) -> str:
    return setting(db, CURRENT) or selected_id(db)


def box_ref(account_id: str | int, name: str) -> str:
    return f'{account_id}:{name}' if account_id else name


def box_owner(db, ref: str, fallback: dict) -> tuple[dict, str]:
    if ':' in ref:
        owner_id, name = ref.split(':', 1)
        return account(db, owner_id), name
    if fallback.get('account_id'):
        return account(db, fallback['account_id']), ref
    return fallback, ref


def record_box(db, account_id: str | int, name: str, ip: str) -> None:
    if not account_id or not name or not ip:
        return
    db.execute('INSERT INTO sprintbox_boxes(account_id,box_name,ip) VALUES (?,?,?) '
               'ON CONFLICT(account_id,box_name) DO NOTHING', (account_id, name, ip))


def ensure_legacy_account(db) -> None:
    """Import once, including old ownership and context references, without touching VPN snapshots."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sprintbox_accounts'").fetchone():
        return
    if db.execute('SELECT 1 FROM sprintbox_accounts LIMIT 1').fetchone():
        return
    values = {key: legacy_value(db, key) for key in ('login', 'key', 'service', 'distributive', 'box_name')}
    if not values['login'] or not values['key']:
        return
    cursor = db.execute('INSERT INTO sprintbox_accounts(label,login,api_key,service,distributive,created_at) '
                        'VALUES (?,?,?,?,?,?)', ('Основной аккаунт', values['login'], values['key'],
                                                 values['service'], values['distributive'], now_iso()))
    owner = str(cursor.lastrowid)
    put(db, SELECTED, owner)
    put(db, CURRENT, owner)
    record_box(db, owner, values['box_name'], setting(db, 'current_ip'))
    db.execute('UPDATE vpn_anchors SET account_id=? WHERE account_login=?', (owner, values['login']))
    for row in db.execute('SELECT box_name,ip FROM vpn_anchors WHERE account_id=?', (owner,)).fetchall():
        record_box(db, owner, row['box_name'], row['ip'])
    for row in db.execute("SELECT key,value FROM settings WHERE key LIKE 'ip_change.%.provider'").fetchall():
        bound = json.loads(row['value'])
        if bound.get('provider') != 'sprintbox' or bound.get('login') != values['login']:
            continue
        bound.update(account_id=owner, old_account_id=owner)
        put(db, row['key'], json.dumps(bound))
        op_id = row['key'].split('.')[1]
        op = db.execute('SELECT old_ip_id,old_ip,new_ip_id,new_ip FROM ip_change_operations WHERE id=?', (op_id,)).fetchone()
        if op:
            record_box(db, owner, op['old_ip_id'], op['old_ip'])
            record_box(db, owner, op['new_ip_id'], op['new_ip'])
        key = f'ip_change.{op_id}.sprintbox'
        raw = setting(db, key)
        if raw:
            ctx = json.loads(raw)
            for field in ('retained_boxes', 'retained_snapshots', 'cleanup_results'):
                if field in ctx:
                    ctx[field] = {box_ref(owner, name): value for name, value in ctx[field].items()}
            put(db, key, json.dumps(ctx))


def assert_idle(db) -> None:
    row = db.execute("SELECT owner_type,owner_id FROM operation_leases WHERE resource='vpn_vps'").fetchone()
    if row:
        raise OperationBusyError(LeaseOwner(row['owner_type'], row['owner_id']))


def _text(value: str, title: str, *, required: bool = True, limit: int = 256) -> str:
    value = value.strip()
    if (required and not value) or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f'Некорректное поле: {title}')
    return value


def save_account(*, label: str, login: str, key: str, service: str, distributive: str,
                 account_id: int | None = None) -> int:
    label = _text(label, 'название', limit=100)
    login = _text(login, 'логин')
    key = _text(key, 'API-ключ', required=account_id is None, limit=4096)
    service = _text(service, 'тариф', required=False)
    distributive = _text(distributive, 'образ ОС', required=False)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        assert_idle(db)
        ensure_legacy_account(db)
        try:
            if account_id is not None:
                old = account(db, account_id)
                if old['login'] != login:
                    raise ValueError('Логин существующего аккаунта менять нельзя: добавьте новый аккаунт')
                db.execute('UPDATE sprintbox_accounts SET label=?,api_key=?,service=?,distributive=? WHERE id=?',
                           (label, key or old['key'], service, distributive, account_id))
                return account_id
            result = db.execute('INSERT INTO sprintbox_accounts(label,login,api_key,service,distributive,created_at) '
                                'VALUES (?,?,?,?,?,?)', (label, login, key, service, distributive, now_iso()))
        except sqlite3.IntegrityError:
            raise ValueError('Аккаунт с таким логином уже добавлен') from None
        owner = int(result.lastrowid)
        if not selected_id(db):
            put(db, SELECTED, str(owner))
        return owner


def select_account(account_id: int) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        assert_idle(db)
        cfg = account(db, account_id)
        if not cfg['service'] or not cfg['distributive']:
            raise ValueError('Сначала выберите и сохраните тариф и образ ОС этого аккаунта')
        if db.execute("SELECT 1 FROM ip_change_operations WHERE status='DONE' AND action_state='SSH_UNREACHABLE'").fetchone():
            raise ValueError('Сначала завершите или отмените ожидание SSH текущей операции')
        put(db, SELECTED, str(account_id))


def list_accounts() -> list[dict]:
    with get_db() as db:
        rows = db.execute('SELECT id,label,login,service,distributive FROM sprintbox_accounts ORDER BY id').fetchall()
        for row in rows:
            row['selected'] = str(row['id']) == selected_id(db)
            row['current'] = bool(legacy_value(db, 'box_name')) and str(row['id']) == current_id(db)
            row['boxes_count'] = db.execute('SELECT COUNT(*) AS n FROM sprintbox_boxes WHERE account_id=?', (row['id'],)).fetchone()['n']
        return rows


def config_for(db, owner_id: str) -> dict:
    if owner_id:
        data = account(db, owner_id)
        return {key: data[key] for key in ('account_id', 'label', 'login', 'key', 'service', 'distributive')}
    return {key: legacy_value(db, key) for key in ('login', 'key', 'service', 'distributive')}


def bound_credentials(db, bound: dict, *, old: bool = False) -> dict:
    owner = bound.get('old_account_id' if old else 'account_id', '')
    cfg = config_for(db, owner)
    if not old and (cfg['login'] != bound.get('login') or hashlib.sha256(cfg['key'].encode()).hexdigest() != bound.get('credential_hash')):
        raise ValueError('Учётные данные аккаунта SprintBOX изменились')
    return cfg
