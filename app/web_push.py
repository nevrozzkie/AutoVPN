"""Optional Web Push: durable bounded retries, independent of VPN operations."""
from __future__ import annotations

import asyncio
import base64
from contextlib import suppress
import json
import logging
import os
import time
import uuid
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.db import get_db, now_iso

logger = logging.getLogger(__name__)
IP_CHANGED = 'Айпи был сменён. Не забудьте обновить конфиги'


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def vapid_keys() -> dict:
    # Generate once, atomically: a new key would invalidate existing subscriptions.
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute("SELECT value FROM settings WHERE key='push.vapid'").fetchone()
        if row:
            return json.loads(row['value'])
        key = ec.generate_private_key(ec.SECP256R1())
        pair = {'private': _b64(key.private_bytes(serialization.Encoding.DER,
                            serialization.PrivateFormat.PKCS8, serialization.NoEncryption())),
                'public': _b64(key.public_key().public_bytes(serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint))}
        db.execute("INSERT INTO settings(key,value) VALUES ('push.vapid',?)", (json.dumps(pair),))
        return pair


def validate_subscription(value: dict) -> tuple[str, str]:
    try:
        endpoint = value['endpoint']
        if not isinstance(endpoint, str) or len(endpoint) > 4096:
            raise ValueError()
        url = urlsplit(endpoint)
        host = url.hostname or ''
        allowed = (host == 'fcm.googleapis.com' or host == 'updates.push.services.mozilla.com'
                   or host.endswith('.push.services.mozilla.com') or host == 'web.push.apple.com'
                   or host.endswith('.push.apple.com') or host.endswith('.notify.windows.com'))
        if (not allowed or url.scheme != 'https' or url.port not in (None, 443)
                or url.username or url.password or url.fragment or not url.path
                or any(ord(c) < 33 for c in endpoint)):
            raise ValueError()
        keys = {name: value['keys'][name] for name in ('p256dh', 'auth')}
        decoded = {}
        for name, encoded in keys.items():
            if not isinstance(encoded, str) or len(encoded) > 128:
                raise ValueError()
            decoded[name] = base64.b64decode(encoded + '=' * (-len(encoded) % 4), altchars=b'-_', validate=True)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded['p256dh'])
        if len(decoded['auth']) != 16:
            raise ValueError()
        return endpoint, json.dumps(keys)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ValueError('Не удалось сохранить подписку этого браузера') from None


def subscribe(client_id: int, value: dict) -> None:
    endpoint, keys = validate_subscription(value)
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT client_id FROM push_subscriptions WHERE endpoint=?', (endpoint,)).fetchone()
        if prior and prior['client_id'] != client_id:
            raise ValueError('Этот браузер подписан на другой личный кабинет. Сначала отключите уведомления там.')
        if not prior and db.execute('SELECT count(*) AS n FROM push_subscriptions WHERE client_id=?', (client_id,)).fetchone()['n'] >= 20:
            raise ValueError('Достигнут лимит устройств с уведомлениями')
        db.execute('INSERT INTO push_subscriptions(client_id,endpoint,keys_json,created_at,updated_at) '
                   'VALUES (?,?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET keys_json=excluded.keys_json,updated_at=excluded.updated_at',
                   (client_id, endpoint, keys, now_iso(), now_iso()))


def unsubscribe(client_id: int, endpoint: str) -> None:
    with get_db() as db:
        row = db.execute('SELECT id FROM push_subscriptions WHERE client_id=? AND endpoint=?', (client_id, endpoint)).fetchone()
        if row:
            db.execute("UPDATE push_deliveries SET state='SKIPPED' WHERE subscription_id=? AND state IN ('PENDING','INFLIGHT')", (row['id'],))
            db.execute('DELETE FROM push_subscriptions WHERE id=?', (row['id'],))


def enqueue(db, body: str, *, title: str = 'Уведомление', event_key: str | None = None,
            client_ids: list[int] | None = None) -> int:
    body, title = body.strip(), title.strip()
    if not body or len(body) > 500 or not title or len(title) > 80:
        raise ValueError('Укажите заголовок до 80 символов и текст до 500 символов')
    event_key = event_key or 'custom:' + uuid.uuid4().hex
    db.execute('INSERT OR IGNORE INTO push_messages(event_key,title,body,created_at) VALUES (?,?,?,?)',
               (event_key, title, body, int(time.time())))
    message = db.execute('SELECT id FROM push_messages WHERE event_key=?', (event_key,)).fetchone()['id']
    query = ('SELECT s.id FROM push_subscriptions s JOIN clients c ON c.id=s.client_id '
             'WHERE c.enabled=1 AND c.deleted_at IS NULL')
    params: list = []
    if client_ids is not None:
        if not client_ids:
            return message
        query += ' AND c.id IN (' + ','.join('?' for _ in client_ids) + ')'
        params.extend(client_ids)
    for row in db.execute(query, params).fetchall():
        db.execute('INSERT OR IGNORE INTO push_deliveries(message_id,subscription_id,next_attempt) VALUES (?,?,?)',
                   (message, row['id'], int(time.time())))
    return message


def enqueue_ip_changed(db, operation_id: int) -> None:
    # A push failure must never roll back a successfully applied IP change.
    try:
        db.execute('SAVEPOINT optional_push')
        enqueue(db, IP_CHANGED, event_key=f'ip-change:{operation_id}')
        db.execute('RELEASE optional_push')
    except Exception:
        with suppress(Exception):
            db.execute('ROLLBACK TO optional_push')
            db.execute('RELEASE optional_push')
        logger.warning('IP change notification could not be queued')


def send_web_push(subscription: dict, payload: dict) -> None:
    # Lazy import: even a missing optional transport cannot break the website.
    from pywebpush import webpush
    import requests
    class NoRedirectSession(requests.Session):
        def request(self, *args, **kwargs):
            kwargs['allow_redirects'] = False
            return super().request(*args, **kwargs)
    with NoRedirectSession() as session:
        session.trust_env = False
        webpush(subscription_info=subscription, data=json.dumps(payload, ensure_ascii=False),
                vapid_private_key=vapid_keys()['private'],
                vapid_claims={'sub': os.environ.get('PUSH_VAPID_SUBJECT', 'mailto:196472320+nevrozzkie@users.noreply.github.com')},
                timeout=8, ttl=3600, requests_session=session)


def deliver_one(now: int | None = None) -> bool:
    now = int(time.time()) if now is None else now
    with get_db() as db:
        db.execute('BEGIN IMMEDIATE')
        db.execute("UPDATE push_deliveries SET state='PENDING' WHERE state='INFLIGHT' AND claimed_at<?", (now - 120,))
        db.execute("UPDATE push_deliveries SET state='FAILED' WHERE state='PENDING' AND (attempts>=6 OR message_id IN "
                   '(SELECT id FROM push_messages WHERE created_at<?))', (now - 86400,))
        row = db.execute("SELECT d.rowid AS delivery_id,d.attempts,s.id AS sub_id,s.endpoint,s.keys_json,c.token,c.enabled,c.deleted_at,"
                         'm.id AS message_id,m.title,m.body FROM push_deliveries d '
                         'LEFT JOIN push_subscriptions s ON s.id=d.subscription_id '
                         'LEFT JOIN clients c ON c.id=s.client_id JOIN push_messages m ON m.id=d.message_id '
                         "WHERE d.state='PENDING' AND d.next_attempt<=? ORDER BY d.next_attempt,d.rowid LIMIT 1", (now,)).fetchone()
        if not row:
            db.execute('DELETE FROM push_messages WHERE created_at<?', (now - 7 * 86400,))
            return False
        if not row['enabled'] or row['deleted_at'] is not None or not row['endpoint']:
            db.execute("UPDATE push_deliveries SET state='SKIPPED' WHERE rowid=?", (row['delivery_id'],))
            return True
        db.execute("UPDATE push_deliveries SET state='INFLIGHT',attempts=attempts+1,claimed_at=? WHERE rowid=?", (now, row['delivery_id']))
    state = 'SENT'
    gone = False
    try:
        subscription = {'endpoint': row['endpoint'], 'keys': json.loads(row['keys_json'])}
        validate_subscription(subscription)
        send_web_push(subscription, {'title': row['title'], 'body': row['body'],
                                    'tag': f"notice-{row['message_id']}", 'url': '/client/' + row['token']})
    except Exception as exc:
        response = getattr(exc, 'response', None)
        code = getattr(response, 'status_code', None)
        gone = code in (404, 410)
        state = 'EXPIRED' if gone else 'FAILED' if row['attempts'] >= 5 or code in (400, 401, 403, 413) else 'PENDING'
        # Never log push endpoints, encryption keys, tokens or provider bodies.
        logger.info('Push delivery %s: %s', row['delivery_id'], state)
    with get_db() as db:
        db.execute('UPDATE push_deliveries SET state=?,next_attempt=? WHERE rowid=? AND state=\'INFLIGHT\'',
                   (state, now + min(3600, 60 * 2 ** row['attempts']), row['delivery_id']))
        if gone:
            db.execute('DELETE FROM push_subscriptions WHERE id=?', (row['sub_id'],))
    return True


async def push_loop() -> None:
    while True:
        try:
            for _ in range(20):
                if not await asyncio.to_thread(deliver_one):
                    break
        except Exception:
            logger.warning('Push worker unavailable; will retry')
        await asyncio.sleep(10)


def admin_status() -> dict:
    with get_db() as db:
        subscribers = db.execute('SELECT c.id,c.name,count(s.id) AS devices FROM clients c '
                                 'JOIN push_subscriptions s ON s.client_id=c.id '
                                 'WHERE c.enabled=1 AND c.deleted_at IS NULL GROUP BY c.id ORDER BY c.name').fetchall()
        messages = db.execute('SELECT m.id,m.title,m.body,m.created_at,d.state,count(d.rowid) AS count '
                              'FROM push_messages m LEFT JOIN push_deliveries d ON d.message_id=m.id '
                              'GROUP BY m.id,d.state ORDER BY m.id DESC LIMIT 100').fetchall()
    return {'subscribers': subscribers, 'messages': messages}
