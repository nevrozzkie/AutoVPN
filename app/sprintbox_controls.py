"""Explicit anchor promotion and durable, user-confirmed box deletion."""
from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import datetime, UTC, timedelta

from app.anchors import applied_endpoint, save_anchor
from app.db import get_db, now_iso, update_operation
from app.ip_change import IpRotationTiming, _ensure_bound_revision_current, _step, _verify_protocol_health
from app.operation_coordinator import acquire_vps_lease, heartbeat_vps_lease, release_vps_lease
from app.runtime_config import transactional_vpn_apply_enabled
from app.secret_sanitization import sanitize_error
from app.sprintbox import SprintboxApiError, SprintboxClient, box_ipv4
from app.sprintbox_accounts import account, current_id, legacy_value, put
from app.sprintbox_ssh import prepare_sprintbox_ssh
from app.ssh_credentials import use_ssh_credentials
from app.vpn_config import captured_vpn_config_from_json


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def anchor_target(db, anchor_id: str) -> dict:
    row = db.execute('SELECT * FROM vpn_anchors WHERE anchor_id=?', (anchor_id,)).fetchone()
    if not row or not row['account_id'] or not row['payload_json']:
        raise ValueError('Для переключения нужен якорь с сохранённой конфигурацией и аккаунтом')
    if captured_vpn_config_from_json(row['payload_json']).current_ip != row['ip']:
        raise ValueError('Конфигурация якоря не соответствует его IP')
    if _is_current(db, row['account_id'], row['box_name'], [row['ip']]):
        raise ValueError('Этот бокс уже является основным VPN')
    return row


def bind_anchor_promotion(db, operation_id: int, anchor_id: str, bound: dict, config) -> dict:
    if bound.get('provider') != 'sprintbox' or not transactional_vpn_apply_enabled():
        raise ValueError('Включите SprintBOX и транзакционное применение VPN')
    if not config.enabled_clients or not any(p.enabled for p in (config.vless.protocol, config.hysteria.protocol, config.amnezia.protocol)):
        raise ValueError('Включите хотя бы один протокол и одного клиента VPN')
    row = anchor_target(db, anchor_id)
    cfg = account(db, row['account_id'])
    bound.update(kind='ANCHOR_PROMOTE', anchor_id=anchor_id, anchor_hash=_digest(row['payload_json']),
                 account_id=str(cfg['id']), login=cfg['login'], credential_hash=_digest(cfg['key']),
                 service=cfg['service'], distributive=cfg['distributive'],
                 target_box=row['box_name'], target_ip=row['ip'])
    if not bound.get('old_account_id'):
        raise ValueError('Не определён владелец текущего VPN')
    put(db, f'ip_change.{operation_id}.provider', json.dumps(bound))
    db.execute('UPDATE ip_change_operations SET old_ip=?,old_ip_id=?,new_ip=?,new_ip_id=?,current_step=? WHERE id=?',
               (config.current_ip, bound['box_name'], row['ip'], row['box_name'], 'anchor_queued', operation_id))
    return bound


def _validate_promotion(db, bound: dict, *, publishing: bool = False) -> tuple[dict, dict]:
    row = db.execute('SELECT * FROM vpn_anchors WHERE anchor_id=?', (bound['anchor_id'],)).fetchone()
    cfg = account(db, bound['account_id'])
    expected_ip = bound['target_ip'] if publishing else None
    current_ip = db.execute("SELECT value FROM settings WHERE key='current_ip'").fetchone()
    if (not row or str(row['account_id']) != bound['account_id'] or row['box_name'] != bound['target_box']
            or row['ip'] != bound['target_ip'] or _digest(row['payload_json'] or '') != bound['anchor_hash']
            or current_id(db) != bound['old_account_id'] or legacy_value(db, 'box_name') != bound['box_name']
            or cfg['login'] != bound['login'] or _digest(cfg['key']) != bound['credential_hash']
            or (publishing and (not current_ip or current_ip['value'] != expected_ip))):
        raise ValueError('Якорь, основной сервер или аккаунт изменился; подготовьте переключение заново')
    return row, cfg


def finish_anchor_promotion(db, operation_id: int, bound: dict, old_ip: str, new_ip: str) -> None:
    row, cfg = _validate_promotion(db, bound, publishing=True)
    if row['maintenance_operation_id'] != operation_id or new_ip != bound['target_ip']:
        raise ValueError('Настройка якоря не соответствует этой операции')
    revision = applied_endpoint(db, old_ip)
    old_owner = account(db, bound['old_account_id'])
    if old_ip and bound['box_name']:
        save_anchor(db, old_owner['login'], bound['box_name'], old_ip, revision, old_owner['id'])
    db.execute('DELETE FROM vpn_anchors WHERE anchor_id=?', (bound['anchor_id'],))
    # Older review actions must not reappear after manually changing the primary server.
    db.execute("UPDATE ip_change_operations SET action_state='SUPERSEDED' "
               "WHERE id!=? AND status='DONE' AND action_state='AWAITING_USER_REVIEW'", (operation_id,))


async def run_anchor_promotion(operation_id: int, *, timing: IpRotationTiming | None = None) -> None:
    from app.vps_provider import bound_provider
    from app.vpn_state import load_ip_change_snapshot, publish_ip_change
    from app.sprintbox_rotation import _deploy
    from app.port_forwarding import reconcile as reconcile_port_forwarding
    bound = bound_provider(operation_id)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("UPDATE ip_change_operations SET status='RUNNING',current_step='inspect_anchor' "
                          "WHERE id=? AND status='PENDING'", (operation_id,)).rowcount:
            return
    timing = timing or IpRotationTiming.configured()
    cfg = None
    try:
        operation, config = load_ip_change_snapshot(operation_id)
        with get_db() as db:
            row, cfg = _validate_promotion(db, bound)
        if operation['old_ip'] != config.current_ip:
            raise ValueError('Основной IP изменился')
        client = SprintboxClient(cfg['login'], cfg['key'], timeout=timing.provider_timeout)
        box = await client.get_box(bound['target_box'])
        if box_ipv4(box) != [bound['target_ip']]:
            raise ValueError('IP якорного бокса у провайдера изменился')
        _ensure_bound_revision_current(config.revision)
        ssh = prepare_sprintbox_ssh()
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            _validate_promotion(db, bound)
            current_ip = db.execute("SELECT value FROM settings WHERE key='current_ip'").fetchone()['value']
            if current_ip != operation['old_ip']:
                raise ValueError('Основной IP изменился')
            db.execute('UPDATE vpn_anchors SET maintenance_operation_id=? WHERE anchor_id=?', (operation_id, bound['anchor_id']))
        # Hide frozen profiles before touching the anchor; failed/interrupted installs remain quarantined.
        new_config = replace(config, current_ip=bound['target_ip'])
        with use_ssh_credentials(ssh.credentials):
            await _step(operation_id, 'configure_anchor', apply_state='RUNNING')
            await _deploy(operation_id, bound['target_ip'], new_config)
            await _step(operation_id, 'protocol_health', apply_state='APPLIED')
            health = await _verify_protocol_health(bound['target_ip'], new_config, timing)
            await _step(operation_id, 'port_forwarding')
            forwarding = await reconcile_port_forwarding(bound['target_ip'], new_config, strict=True)
        with get_db() as db:
            _validate_promotion(db, bound)
        publish_ip_change(operation_id, bound['target_ip'], health, sprintbox_box_name=bound['target_box'],
                          sprintbox_ssh_key_path=ssh.credentials.key_path, port_forwarding_result=forwarding)
        update_operation(operation_id, status='DONE', action_state='DONE', current_step='done', error_message='')
    except Exception as exc:
        message = sanitize_error(exc, *(cfg[k] for k in ('login', 'key')) if cfg else ())
        update_operation(operation_id, status='FAILED', action_state='FAILED', current_step='failed', error_message=message)


def _is_current(db, owner_id: int | str, name: str, ips: list[str]) -> bool:
    ip = db.execute("SELECT value FROM settings WHERE key='current_ip'").fetchone()
    return ((str(owner_id) == current_id(db) and name == legacy_value(db, 'box_name'))
            or bool(ip and ip['value'] and ip['value'] in ips))


def _anchor_ids(db, cfg: dict, name: str, ips: list[str]) -> list[str]:
    # The box name stays protected even when its IP has changed.
    return sorted(row['anchor_id'] for row in db.execute(
        'SELECT anchor_id FROM vpn_anchors WHERE account_login=? AND box_name=?', (cfg['login'], name)).fetchall())


async def prepare_deletion(account_id: int, name: str) -> int:
    with get_db() as db:
        cfg = account(db, account_id)
    client = SprintboxClient(cfg['login'], cfg['key'])
    box = await client.get_box(name)
    ips = box_ipv4(box)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if _is_current(db, account_id, name, ips):
            raise ValueError('Нельзя удалить основной VPN. Сначала сделайте другой сервер основным')
        anchors = _anchor_ids(db, cfg, name, ips)
        if db.execute("SELECT 1 FROM sprintbox_deletions WHERE account_id=? AND box_name=? "
                      "AND status IN ('PENDING','RUNNING')", (account_id, name)).fetchone():
            raise ValueError('Удаление этого бокса уже выполняется')
        cursor = db.execute('INSERT INTO sprintbox_deletions(account_id,box_name,ips_json,credential_hash,anchor_ids_json,status,created_at,updated_at) '
                            "VALUES (?,?,?,?,?,'CONFIRM',?,?)", (account_id, name, json.dumps(ips), _digest(cfg['key']), json.dumps(anchors), now_iso(), now_iso()))
        return int(cursor.lastrowid)


def deletion(operation_id: int) -> dict:
    with get_db() as db:
        row = db.execute('SELECT d.*,a.label AS account_label FROM sprintbox_deletions d '
                         'JOIN sprintbox_accounts a ON a.id=d.account_id WHERE d.id=?', (operation_id,)).fetchone()
    if not row:
        raise ValueError('Операция удаления не найдена')
    row['ips'] = json.loads(row['ips_json'])
    row['is_anchor'] = bool(json.loads(row['anchor_ids_json']))
    return row


def _validate_delete(db, row: dict) -> dict:
    cfg = account(db, row['account_id'])
    ips = json.loads(row['ips_json'])
    if (_digest(cfg['key']) != row['credential_hash'] or _is_current(db, row['account_id'], row['box_name'], ips)
            or _anchor_ids(db, cfg, row['box_name'], ips) != json.loads(row['anchor_ids_json'])):
        raise ValueError('Аккаунт, основной сервер или якорь изменился; подготовьте удаление заново')
    if json.loads(row['anchor_ids_json']) and not row['allow_anchor']:
        raise ValueError('Нужно отдельно подтвердить удаление якоря и его профилей')
    return cfg


def confirm_deletion(operation_id: int, name: str, allow_anchor: bool) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM sprintbox_deletions WHERE id=?', (operation_id,)).fetchone()
        if not row or row['status'] != 'CONFIRM' or row['box_name'] != name.strip():
            raise ValueError('Введите точное имя бокса; повторная отправка подтверждения запрещена')
        if datetime.fromisoformat(row['created_at']) < datetime.now(UTC) - timedelta(minutes=15):
            raise ValueError('Подтверждение устарело. Откройте удаление бокса заново')
        row['allow_anchor'] = int(allow_anchor)
        _validate_delete(db, row)
        acquire_vps_lease(db, 'BOX_DELETE', operation_id)
        db.execute("UPDATE sprintbox_deletions SET status='PENDING',allow_anchor=?,updated_at=? WHERE id=?", (int(allow_anchor), now_iso(), operation_id))


def _update_delete(operation_id: int, **fields) -> None:
    fields['updated_at'] = now_iso()
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('UPDATE sprintbox_deletions SET '+','.join(f'{key}=?' for key in fields)+' WHERE id=?', (*fields.values(), operation_id))
        if fields.get('status') in {'DONE', 'FAILED', 'AMBIGUOUS'}:
            release_vps_lease(db, 'BOX_DELETE', operation_id)
        else:
            heartbeat_vps_lease(db, 'BOX_DELETE', operation_id)


def _finish_delete(operation_id: int) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM sprintbox_deletions WHERE id=?', (operation_id,)).fetchone()
        cfg = _validate_delete(db, row)
        db.execute('DELETE FROM vpn_anchors WHERE account_login=? AND box_name=?', (cfg['login'], row['box_name']))
        db.execute('DELETE FROM sprintbox_boxes WHERE account_id=? AND box_name=?', (row['account_id'], row['box_name']))
        ref = f"{row['account_id']}:{row['box_name']}"
        for ctx_row in db.execute("SELECT key,value FROM settings WHERE key LIKE 'ip_change.%.sprintbox'").fetchall():
            ctx = json.loads(ctx_row['value'])
            retained = ctx.get('retained_boxes', {})
            if ref in retained and (not json.loads(row['ips_json']) or retained[ref] in json.loads(row['ips_json'])):
                retained.pop(ref)
                put(db, ctx_row['key'], json.dumps(ctx))
        for pending in db.execute("SELECT id FROM ip_change_operations WHERE status='DONE' AND action_state='SSH_UNREACHABLE' "
                                  "AND new_ip_id=?", (row['box_name'],)).fetchall():
            binding = json.loads(db.execute('SELECT value FROM settings WHERE key=?',
                                           (f"ip_change.{pending['id']}.provider",)).fetchone()['value'])
            if binding.get('account_id') == str(row['account_id']):
                db.execute("UPDATE ip_change_operations SET action_state='CANCELLED',current_step='cancelled', "
                           "cleanup_warning='Бокс удалён пользователем',updated_at=? WHERE id=?", (now_iso(), pending['id']))
        db.execute("UPDATE sprintbox_deletions SET status='DONE',action_state='CONFIRMED',error_message='',updated_at=? WHERE id=?", (now_iso(), operation_id))
        release_vps_lease(db, 'BOX_DELETE', operation_id)


async def run_deletion(operation_id: int, *, timing: IpRotationTiming | None = None) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("UPDATE sprintbox_deletions SET status='RUNNING',updated_at=? WHERE id=? AND status='PENDING'", (now_iso(), operation_id)).rowcount:
            return
    row = deletion(operation_id)
    timing = timing or IpRotationTiming.configured()
    sent = False
    accepted = False
    cfg = None
    try:
        with get_db() as db:
            cfg = _validate_delete(db, row)
        client = SprintboxClient(cfg['login'], cfg['key'], timeout=timing.provider_timeout)
        if row['box_name'] not in await client.list_boxes():
            _finish_delete(operation_id)
            return
        box = await client.get_box(row['box_name'])
        if sorted(box_ipv4(box)) != sorted(row['ips']):
            raise ValueError('IP бокса изменился после подтверждения; удаление отменено')
        with get_db() as db:
            _validate_delete(db, row)
        _update_delete(operation_id, action_state='SENDING')
        sent = True
        job = await client.delete_box(row['box_name'])
        accepted = True
        _update_delete(operation_id, action_state='SENT', task_id=job)
        deadline = asyncio.get_running_loop().time() + timing.ip_appear_timeout
        while True:
            state = await client.task_status(job)
            _update_delete(operation_id)
            if state == 'finished':
                break
            if state == 'error':
                raise ValueError('SprintBOX сообщил об ошибке задания удаления; проверьте наличие бокса')
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError('SprintBOX не завершил удаление за отведённое время')
            await asyncio.sleep(max(.01, timing.ip_appear_interval))
        if row['box_name'] in await client.list_boxes():
            raise ValueError('Бокс всё ещё существует после задания удаления; проверьте результат')
        _finish_delete(operation_id)
    except Exception as exc:
        uncertain = sent and (accepted or not isinstance(exc, SprintboxApiError))
        message = sanitize_error(exc, *(cfg[k] for k in ('login', 'key')) if cfg else ())
        _update_delete(operation_id, status='AMBIGUOUS' if uncertain else 'FAILED', error_message=message)


async def recheck_deletion(operation_id: int) -> None:
    row = deletion(operation_id)
    if row['status'] != 'AMBIGUOUS':
        raise ValueError('Эта операция не требует проверки результата')
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        cfg = _validate_delete(db, row)
        acquire_vps_lease(db, 'BOX_DELETE', operation_id)
    try:
        client = SprintboxClient(cfg['login'], cfg['key'])
        boxes = await client.list_boxes()
        if row['box_name'] not in boxes:
            _finish_delete(operation_id)
        elif row['task_id'] and await client.task_status(row['task_id']) == 'error':
            _update_delete(operation_id, status='FAILED', error_message='Задание отклонено SprintBOX. Бокс не удалён; можно подготовить новую попытку')
        else:
            raise ValueError('Бокс ещё существует; результат предыдущего запроса пока не подтверждён')
    finally:
        with get_db() as db:
            release_vps_lease(db, 'BOX_DELETE', operation_id)
