"""Persist accepted SprintBOX cleanups and dispatch only at 23:30 Moscow time."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.db import get_db, now_iso
from app.operation_coordinator import OperationBusyError, acquire_vps_lease
from app.sprintbox_review import context, validate_review

MSK = ZoneInfo('Europe/Moscow')
logger = logging.getLogger(__name__)


def next_cleanup(now: datetime | None = None) -> str:
    local = (now or datetime.now(UTC)).astimezone(MSK)
    due = local.replace(hour=23, minute=30, second=0, microsecond=0)
    if due <= local:
        due += timedelta(days=1)
    return due.astimezone(UTC).isoformat(timespec='seconds')


def schedule_cleanup(operation_id: int, *, client_id: int | None = None,
                     keep_boxes: list[str] | None = None, now: datetime | None = None) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        validate_review(db, operation_id)
        if client_id is not None:
            require_owner(db, operation_id, client_id)
        if keep_boxes:
            from app.anchors import retain_as_anchors
            retain_as_anchors(db, operation_id, keep_boxes)
        ctx = context(db, operation_id)
        ctx['cleanup_at'] = next_cleanup(now)
        db.execute('UPDATE settings SET value=? WHERE key=?',
                   (json.dumps(ctx), f'ip_change.{operation_id}.sprintbox'))
        db.execute("UPDATE ip_change_operations SET action_state='CLEANUP_SCHEDULED', "
                   "current_step='cleanup_scheduled',updated_at=? WHERE id=?", (now_iso(), operation_id))


def require_owner(db, operation_id: int, client_id: int) -> None:
    row = db.execute('SELECT value FROM settings WHERE key=?',
                     (f'ip_change.{operation_id}.client_id',)).fetchone()
    if not row or row['value'] != str(client_id):
        raise ValueError('Этой сменой IP управляет другой пользователь')


async def dispatch_cleanups(now: datetime | None = None) -> None:
    from app.sprintbox_rotation import run_sprintbox_cleanup
    current = now or datetime.now(UTC)
    local = current.astimezone(MSK)
    # A missed window (downtime or busy VPS) waits for the next night.
    if (local.hour, local.minute) != (23, 30):
        return
    with get_db() as db:
        rows = db.execute("SELECT id FROM ip_change_operations WHERE status='DONE' "
                          "AND action_state='CLEANUP_SCHEDULED' ORDER BY id").fetchall()
    for row in rows:
        with get_db() as db:
            db.execute('BEGIN IMMEDIATE')
            operation_id = row['id']
            operation = db.execute('SELECT action_state FROM ip_change_operations WHERE id=?', (operation_id,)).fetchone()
            due = context(db, operation_id).get('cleanup_at')
            if operation['action_state'] != 'CLEANUP_SCHEDULED' or not due or datetime.fromisoformat(due) > current:
                continue
            try:
                acquire_vps_lease(db, 'IP_CHANGE', operation_id)
            except OperationBusyError:
                return
            db.execute("UPDATE ip_change_operations SET status='PENDING',action_state='CLEANUP_QUEUED', "
                       "current_step='cleanup_queued',updated_at=? WHERE id=?", (now_iso(), operation_id))
        await run_sprintbox_cleanup(operation_id)


async def cleanup_loop() -> None:
    while True:
        try:
            await dispatch_cleanups()
        except Exception:
            logger.exception('Scheduled SprintBOX cleanup failed')
        await asyncio.sleep(15)


def source_label(operation_id: int) -> str:
    with get_db() as db:
        row = db.execute('SELECT value FROM settings WHERE key=?', (f'ip_change.{operation_id}.source',)).fetchone()
    if not row:
        return 'Источник не записан (старая операция)'
    source = json.loads(row['value'])
    if source['kind'] == 'user':
        return f"Пользователь: {source['name']} (ID {source['client_id']})"
    return 'Администратор'


def cleanup_queue() -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM ip_change_operations WHERE status='DONE' "
                          "AND action_state='CLEANUP_SCHEDULED' ORDER BY id").fetchall()
        return [dict(row, cleanup_at=context(db, row['id']).get('cleanup_at'),
                     retained_boxes=context(db, row['id']).get('retained_boxes', {})) for row in rows]


def queue_cleanup_now(operation_id: int) -> None:
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT status,action_state FROM ip_change_operations WHERE id=?', (operation_id,)).fetchone()
        if not row or row['status'] != 'DONE' or row['action_state'] != 'CLEANUP_SCHEDULED':
            raise ValueError('Удаление уже запущено или отсутствует в очереди')
        acquire_vps_lease(db, 'IP_CHANGE', operation_id)
        db.execute("UPDATE ip_change_operations SET status='PENDING',action_state='CLEANUP_QUEUED', "
                   "current_step='cleanup_queued',updated_at=? WHERE id=?", (now_iso(), operation_id))
