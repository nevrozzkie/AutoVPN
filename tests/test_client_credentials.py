import asyncio

import pytest
from fastapi.testclient import TestClient

from test_anchor_access import anchor_users
from test_anchor_updates import editable
from app import client_credentials as credentials, main, anchor_updates as edits
from app.db import get_db, get_client_by_id, get_vpn_state, set_client_enabled, _mark_vpn_config_updated
from app.vpn_config import capture_vpn_config, canonical_vpn_config_json
from app.vpn_state import prepare_install_operation, complete_install_operation
from app.operation_coordinator import recover_incomplete_vps_operations, OperationBusyError
from app.anchors import anchor_client


@pytest.fixture
def rotation(editable, monkeypatch):
    anchor, user, other, _ = editable
    # Existing device on both the primary and the anchor.
    _, operation = edits.prepare_anchor_device(anchor, user['token'], name='Anchor tablet')
    asyncio.run(edits.run_edit(operation))
    private, public, psk = credentials._keys()
    with get_db() as db:
        db.execute("INSERT INTO amnezia_devices(client_id,name,private_key,public_key,preshared_key,ipv4,created_at) VALUES (?,?,?,?,?,?,'2026-01-01')", (user['id'], 'Phone', private, public, psk, '10.66.66.9'))
        _mark_vpn_config_updated(db)
    install = prepare_install_operation('203.0.113.20')
    complete_install_operation(install.operation_id, 'Test initial primary')
    monkeypatch.setattr(credentials, 'managed_key_path', edits.managed_key_path)
    monkeypatch.setattr(credentials, 'SprintboxClient', edits.SprintboxClient)
    calls = []
    async def deploy(operation, host, script, **kwargs):
        calls.append((host, script))
        return 0, ''
    monkeypatch.setattr(credentials, '_run_deploy_with_heartbeat', deploy)
    return anchor, user, other, calls


def test_admin_reissues_page_links_and_every_key_across_servers(rotation):
    anchor, user, other, calls = rotation
    http = TestClient(main.app)
    primary_before = capture_vpn_config()
    anchor_before = anchor_client(anchor, user['token'])[1]
    with get_db() as db:
        db.execute("INSERT INTO push_subscriptions(client_id,endpoint,keys_json,created_at,updated_at) VALUES (?,'https://push.example/old','{}','now','now')", (user['id'],))
    url = f"/admin/clients/{user['id']}/credentials"
    assert http.post(url, data={'confirm': 'true'}).status_code == 401
    assert http.post(url, auth=('admin', 'test-password'), data={'confirm': 'true'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert http.post(url, auth=('admin', 'test-password')).status_code == 400
    response = http.post(url, auth=('admin', 'test-password'), data={'confirm': 'true', 'rotate_page': 'true'}, follow_redirects=False)
    assert response.status_code == 303
    person = get_client_by_id(user['id'])
    access = credentials.access_token(user['id'])
    assert person['token'] != user['token'] and access not in {person['token'], user['token']}
    assert credentials.state(user['id'])['status'] == 'DONE'
    with get_db() as db:
        assert not db.execute('SELECT 1 FROM push_subscriptions WHERE client_id=?', (user['id'],)).fetchone()
    assert [host for host, _ in calls] == ['203.0.113.20', '203.0.113.10']
    for kind in ('sub', 'sing-box', 'clash', 'clash-provider', 'amnezia', 'amnezia-key', 'ip'):
        assert http.get(f"/{kind}/{user['token']}").status_code == 404
        assert http.get(f'/{kind}/{access}').status_code == 200
        assert http.get(f"/{kind}/{person['token']}").status_code == 404
    assert http.get(f"/client/{user['token']}").status_code == 404
    assert http.get(f'/client/{access}').status_code == 404
    page = http.get(f"/client/{person['token']}")
    assert page.status_code == 200 and f'/sub/{access}' in page.text
    assert f'/client/{access}/subscription.qr' in page.text
    assert http.get(f'/client/{access}/subscription.qr').status_code == 200
    assert http.get(f"/client/{user['token']}/amnezia.qr").status_code == 404
    assert http.get(f'/client/{access}/amnezia.qr').status_code == 200
    assert f'/clash-provider/{access}' in http.get(f'/clash/{access}').text
    assert http.get(f"/anchor/{anchor}/sub/{user['token']}").status_code != 200
    assert http.get(f'/anchor/{anchor}/sub/{access}').status_code == 200
    anchor_page = http.get(f"/anchor/{anchor}/client/{person['token']}")
    assert anchor_page.status_code == 200 and f'/amnezia/{access}' in anchor_page.text
    current = capture_vpn_config()
    frozen = anchor_client(anchor, person['token'])[1]
    for old, new in ((primary_before, current), (anchor_before, frozen)):
        old_person = next(c for c in old.clients if c.id == user['id'])
        new_person = next(c for c in new.clients if c.id == user['id'])
        for key in ('vless_uuid', 'hysteria_password', 'amnezia_private_key', 'amnezia_public_key', 'amnezia_preshared_key'):
            assert getattr(old_person, key) != getattr(new_person, key)
        assert old_person.amnezia_ipv4 == new_person.amnezia_ipv4
        assert old.amnezia_devices[0].public_key != new.amnezia_devices[0].public_key
        assert next(c for c in old.clients if c.id == other['id']) == next(c for c in new.clients if c.id == other['id'])
    assert get_vpn_state()['desired_revision'] == get_vpn_state()['applied_revision']


def test_user_preserves_page_and_can_revoke_then_issue_new(rotation):
    anchor, user, _, _ = rotation
    http = TestClient(main.app)
    base = f"/client/{user['token']}"
    assert http.post(base + '/credentials', data={'confirm': 'true', 'rotate_page': 'true'}).status_code == 400
    assert http.post(base + '/credentials', data={'confirm': 'true'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert http.post(base + '/credentials', data={'confirm': 'true'}, follow_redirects=False).status_code == 303
    first_access = credentials.access_token(user['id'])
    assert get_client_by_id(user['id'])['token'] == user['token']
    assert http.get(base).status_code == 200
    assert http.get(f'/sub/{first_access}').status_code == 200
    assert http.post(base + '/credentials', data={'confirm': 'true', 'action': 'revoke'}, follow_redirects=False).status_code == 303
    assert credentials.access_token(user['id']) == ''
    assert http.get(f'/sub/{first_access}').status_code == 404
    assert 'Ключ AmneziaVPN' not in http.get(base).text
    assert 'Отозвать старые и выдать новые' in http.get(base).text
    anchor_page = http.get(f"/anchor/{anchor}/client/{user['token']}")
    assert 'Ключ AmneziaVPN' not in anchor_page.text and 'VLESS UUID' not in anchor_page.text
    assert http.post(base + '/credentials', data={'confirm': 'true'}, follow_redirects=False).status_code == 303
    assert credentials.access_token(user['id']) not in {first_access, '', user['token']}
    assert http.get(f'/sub/{first_access}').status_code == 404


def test_failure_restart_and_retry_never_republish_old_keys(rotation, monkeypatch):
    anchor, user, _, calls = rotation
    original = credentials._run_deploy_with_heartbeat
    async def fail_anchor(operation, host, script, **kwargs):
        if host == '203.0.113.10':
            raise TimeoutError('private provider details')
        return await original(operation, host, script, **kwargs)
    monkeypatch.setattr(credentials, '_run_deploy_with_heartbeat', fail_anchor)
    operation, page_token = credentials.prepare(user['id'], rotate_page=True)
    access = credentials.access_token(user['id'])
    asyncio.run(credentials.run(operation))
    status = credentials.state(user['id'])
    assert status['status'] == 'AMBIGUOUS' and status['targets'][0]['done'] and not status['targets'][1]['done']
    http = TestClient(main.app)
    page = http.get(f'/client/{page_token}')
    assert 'Повторить применение отзыва' in page.text and 'private provider details' not in page.text
    assert 'amnezia-key' not in page.text
    from app.vpn_state import prepare_ip_change_operation, IpChangeSafetyHoldError
    with pytest.raises(IpChangeSafetyHoldError): prepare_ip_change_operation()
    from app.db import delete_client, reset_server_and_aeza_state
    with pytest.raises(ValueError): delete_client(user['id'])
    with pytest.raises(ValueError): reset_server_and_aeza_state()
    edit = edits.edit_state(anchor)
    assert edit['credential_client_id'] == user['id']
    assert http.get(f'/sub/{access}').status_code == 404
    frozen_keys = canonical_vpn_config_json(capture_vpn_config())
    with pytest.raises(ValueError): credentials.prepare(user['id'])
    retry = credentials.retry(user['id'])
    with pytest.raises(ValueError): credentials.retry(user['id'])
    with get_db() as db: recover_incomplete_vps_operations(db, 'Restart')
    asyncio.run(credentials.run(retry))
    assert credentials.state(user['id'])['status'] != 'DONE'
    retry = credentials.retry(user['id'])
    monkeypatch.setattr(credentials, '_run_deploy_with_heartbeat', original)
    asyncio.run(credentials.run(retry))
    assert credentials.state(user['id'])['status'] == 'DONE'
    assert credentials.access_token(user['id']) == access
    assert canonical_vpn_config_json(capture_vpn_config()) == frozen_keys
    assert http.get(f'/sub/{access}').status_code == 200


def test_busy_and_disabled_do_not_change_credentials(rotation):
    anchor, user, other, _ = rotation
    operation = prepare_install_operation('203.0.113.20')
    before = get_client_by_id(user['id'])
    with pytest.raises(OperationBusyError): credentials.prepare(user['id'], rotate_page=True)
    assert get_client_by_id(user['id']) == before
    assert credentials.access_token(user['id']) == user['token']
    complete_install_operation(operation.operation_id, 'Test apply')
    set_client_enabled(user['id'], False)
    assert TestClient(main.app).post(f"/client/{user['token']}/credentials", data={'confirm': 'true'}).status_code == 404
    with pytest.raises(ValueError): credentials.prepare(user['id'])


def test_anchor_changed_at_provider_is_never_applied(rotation, monkeypatch):
    anchor, user, _, calls = rotation
    class WrongProvider:
        def __init__(self, *args): pass
        async def get_box(self, name): return {'ipv4': ['203.0.113.99']}
    monkeypatch.setattr(credentials, 'SprintboxClient', WrongProvider)
    operation, _ = credentials.prepare(user['id'])
    asyncio.run(credentials.run(operation))
    assert [host for host, _ in calls] == ['203.0.113.20']
    assert credentials.state(user['id'])['status'] == 'AMBIGUOUS'
    assert not credentials.state(user['id'])['targets'][1]['done']
    assert not credentials.exports_available(user['id'])
