from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import main, web_push
from app.db import init_db, create_client, get_db, get_operation, set_client_enabled, set_setting
from app.vpn_state import prepare_ip_change_operation
from test_sprintbox import scenario, run
from test_user_ip_change import user_flow

SEND_WEB_PUSH = web_push.send_web_push


def b64(value):
    return base64.urlsafe_b64encode(value).decode().rstrip('=')


def browser(endpoint='https://fcm.googleapis.com/fcm/send/example'):
    private = ec.generate_private_key(ec.SECP256R1())
    return {'endpoint': endpoint, 'keys': {
        'p256dh': b64(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)),
        'auth': b64(b'0123456789abcdef'),
    }}


@pytest.fixture
def pushes(monkeypatch):
    init_db()
    set_setting('config.admin_password', 'test-password')
    client = create_client('Получатель')
    sent = []
    monkeypatch.setattr(web_push, 'send_web_push', lambda sub, payload: sent.append((sub, payload)))
    async def idle():
        await asyncio.Event().wait()
    monkeypatch.setattr(web_push, 'push_loop', idle)
    return client, sent


def enqueue(body='Тест', **kwargs):
    with get_db() as db:
        return web_push.enqueue(db, body, **kwargs)


def deliveries():
    with get_db() as db:
        return db.execute('SELECT * FROM push_deliveries ORDER BY rowid').fetchall()


def test_permission_is_opt_in_and_api_is_private_owned_and_csrf_protected(pushes):
    client, sent = pushes
    sub = browser()
    other = create_client('Другой')
    base = f"/client/{client['token']}/push"
    with TestClient(main.app, base_url='http://panel.local') as http:
        response = http.get(base)
        assert response.status_code == 200 and len(response.json()['publicKey']) == 87
        assert response.headers['cache-control'] == 'private, no-store'
        assert http.post('/client/invalid/push', json=sub).status_code == 404
        assert http.post(base, json=sub, headers={'Origin': 'https://evil.local'}).status_code == 403
        assert http.post(base, json=sub).status_code == 200
        assert http.post(base, json=sub).status_code == 200
        assert http.post(base+'/status', json={'endpoint':sub['endpoint']}).json() == {'enabled': True}
        assert http.post(f"/client/{other['token']}/push", json=sub).status_code == 400
        http.post(f"/client/{other['token']}/push/unsubscribe", json={'endpoint':sub['endpoint']})
        assert http.post(base+'/status', json={'endpoint':sub['endpoint']}).json()['enabled']
        assert http.post(base+'/unsubscribe', json={'endpoint':sub['endpoint']}).status_code == 200
        assert not http.post(base+'/status', json={'endpoint':sub['endpoint']}).json()['enabled']
        assert http.get('/admin/notifications').status_code == 401
    assert sent == []


@pytest.mark.parametrize('endpoint', [
    'http://fcm.googleapis.com/send/a', 'https://127.0.0.1/test',
    'https://fcm.googleapis.com.attacker.test/test', 'https://attacker.test/test',
    'https://fcm.googleapis.com:8443/a', 'https://user:pass@fcm.googleapis.com/a',
    'https://fcm.googleapis.com/a#fragment',
])
def test_subscription_cannot_turn_sender_into_ssrf_proxy(pushes, endpoint):
    client, _ = pushes
    with pytest.raises(ValueError):
        web_push.subscribe(client['id'], browser(endpoint))
    assert not deliveries()


def test_invalid_encryption_key_is_rejected(pushes):
    client, _ = pushes
    sub = browser()
    sub['keys']['p256dh'] = b64(b'x'*65)
    with pytest.raises(ValueError):
        web_push.subscribe(client['id'], sub)


def test_stable_vapid_key_survives_reinitialization(pushes):
    first = web_push.vapid_keys()
    init_db()
    assert web_push.vapid_keys() == first
    assert first['private'] != first['public']


def test_custom_notification_recipients_dedup_and_unsubscribe(pushes):
    client, sent = pushes
    other = create_client('Другой')
    web_push.subscribe(client['id'], browser())
    web_push.subscribe(other['id'], browser('https://web.push.apple.com/example'))
    enqueue(event_key='custom:once', client_ids=[client['id']])
    enqueue(event_key='custom:once', client_ids=[client['id']])
    assert len(deliveries()) == 1
    assert web_push.deliver_one()
    assert len(sent) == 1 and sent[0][1]['body'] == 'Тест'
    assert sent[0][1]['url'] == '/client/' + client['token']
    assert not web_push.deliver_one()
    enqueue('После отключения', client_ids=[client['id']])
    web_push.unsubscribe(client['id'], sent[0][0]['endpoint'])
    assert not web_push.deliver_one()
    assert len(sent) == 1


def test_transient_error_retries_after_restart_with_same_notification_tag(pushes, monkeypatch):
    client, sent = pushes
    web_push.subscribe(client['id'], browser())
    enqueue()
    calls = []
    def unstable(sub, payload):
        calls.append(payload)
        if len(calls) == 1:
            raise TimeoutError('endpoint-and-secret-must-not-be-logged')
    monkeypatch.setattr(web_push, 'send_web_push', unstable)
    now = int(time.time())
    assert web_push.deliver_one(now)
    assert deliveries()[0]['state'] == 'PENDING'
    assert not web_push.deliver_one(now+1)
    init_db()
    assert web_push.deliver_one(now+61)
    assert deliveries()[0]['state'] == 'SENT'
    assert calls[0]['tag'] == calls[1]['tag']


def test_crashed_worker_claim_is_recovered(pushes):
    client, sent = pushes
    web_push.subscribe(client['id'], browser())
    enqueue()
    with get_db() as db:
        db.execute("UPDATE push_deliveries SET state='INFLIGHT',claimed_at=?,attempts=1", (int(time.time())-121,))
    init_db()
    assert web_push.deliver_one()
    assert len(sent) == 1 and deliveries()[0]['attempts'] == 2


@pytest.mark.parametrize('code,state', [(410,'EXPIRED'), (404,'EXPIRED'), (403,'FAILED'), (429,'PENDING'), (503,'PENDING')])
def test_provider_errors_are_classified_and_expired_subscriptions_removed(pushes, monkeypatch, code, state):
    client, _ = pushes
    web_push.subscribe(client['id'], browser())
    enqueue()
    class Error(Exception):
        response = SimpleNamespace(status_code=code)
    def fail(*args): raise Error('sensitive provider body')
    monkeypatch.setattr(web_push, 'send_web_push', fail)
    web_push.deliver_one()
    assert deliveries()[0]['state'] == state
    with get_db() as db:
        assert bool(db.execute('SELECT 1 FROM push_subscriptions').fetchone()) == (code not in (404, 410))


def test_retry_budget_is_bounded_and_disabled_users_are_skipped(pushes, monkeypatch):
    client, sent = pushes
    web_push.subscribe(client['id'], browser())
    enqueue()
    def fail(*args): raise TimeoutError()
    monkeypatch.setattr(web_push, 'send_web_push', fail)
    now = int(time.time())
    for attempt in range(6):
        web_push.deliver_one(now + attempt*3600)
    assert deliveries()[0]['state'] == 'FAILED' and deliveries()[0]['attempts'] == 6
    enqueue('Не отправлять')
    set_client_enabled(client['id'], False)
    web_push.deliver_one()
    assert deliveries()[-1]['state'] == 'SKIPPED'
    assert sent == []


def test_admin_custom_send_selected_auth_csrf_and_post_redirect_get(pushes):
    client, sent = pushes
    web_push.subscribe(client['id'], browser())
    auth = ('admin', 'test-password')
    data = {'title':'Уведомление', 'body':'Всем привет', 'audience':'selected', 'recipients':str(client['id']), 'request_id':'a'*32}
    with TestClient(main.app, base_url='http://panel.local') as http:
        assert http.post('/admin/notifications', data=data).status_code == 401
        assert http.post('/admin/notifications', data=data, auth=auth, headers={'Origin':'https://evil.local'}).status_code == 403
        assert http.post('/admin/notifications', data=data, auth=auth, follow_redirects=False).status_code == 303
        assert http.post('/admin/notifications', data=data, auth=auth, follow_redirects=False).status_code == 303
        assert len(deliveries()) == 1
        page = http.get('/admin/notifications?sent=1', auth=auth)
        assert page.status_code == 200 and 'Всем привет' in page.text and client['name'] in page.text
    web_push.deliver_one()
    assert sent[0][1]['title'] == 'Уведомление' and sent[0][1]['body'] == 'Всем привет'


def test_ip_publication_queues_only_after_success_and_push_failure_does_not_break_ip(user_flow, monkeypatch):
    boxes, user = user_flow
    web_push.subscribe(user.id, browser())
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    assert deliveries() == []
    run(op)
    assert get_operation(op)['action_state'] == 'AWAITING_USER_REVIEW'
    assert len(deliveries()) == 1
    with get_db() as db:
        assert db.execute('SELECT body FROM push_messages').fetchone()['body'] == web_push.IP_CHANGED
    # A queue/storage failure must not prevent the next successful publication.
    original_enqueue = web_push.enqueue
    def fail(*args, **kwargs):
        original_enqueue(*args, **kwargs)
        raise RuntimeError('outbox unavailable')
    monkeypatch.setattr(web_push, 'enqueue', fail)
    second = prepare_ip_change_operation(user_client_id=user.id, review_operation_id=op).operation_id
    run(second)
    assert get_operation(second)['action_state'] == 'AWAITING_USER_REVIEW'
    assert len(deliveries()) == 1


def test_failed_rotation_does_not_notify(user_flow):
    boxes, user = user_flow
    web_push.subscribe(user.id, browser())
    boxes.fail = 'create-rejected'
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    assert deliveries() == []


def test_real_transport_encrypts_payload_and_signs_vapid_without_following_redirects(pushes, monkeypatch):
    import http_ece
    import requests
    private = ec.generate_private_key(ec.SECP256R1())
    auth = b'0123456789abcdef'
    sub = browser()
    sub['keys']['p256dh'] = b64(private.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    captured = []
    def request(session, method, url, **kwargs):
        captured.append(kwargs)
        assert url == sub['endpoint']
        assert kwargs['allow_redirects'] is False and session.trust_env is False
        return SimpleNamespace(status_code=201, text="", headers={})
    monkeypatch.setattr(requests.Session, 'request', request)
    # The fixture replaces the sender; exercise the actual production function.
    payload = {'title':'Уведомление','body':web_push.IP_CHANGED}
    SEND_WEB_PUSH(sub, payload)
    assert captured[0]['timeout'] == 8
    headers = {k.lower():v for k,v in captured[0]['headers'].items()}
    assert headers['authorization'].startswith('vapid ')
    assert headers['content-encoding'] == 'aes128gcm'
    plaintext = http_ece.decrypt(captured[0]['data'], private_key=private, auth_secret=auth)
    assert json.loads(plaintext) == payload
