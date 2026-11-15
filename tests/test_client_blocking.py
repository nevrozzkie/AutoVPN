from fastapi.testclient import TestClient

from app import main
from app.anchors import set_anchor_access
from app.db import get_client_by_id, get_db, get_vpn_state, public_vpn_version
from app.eu_install import build_eu_config_apply_script
from app.vpn_config import capture_vpn_config
from test_anchor_access import anchor_users


def test_block_unblock_preserves_identity_and_closes_personal_and_anchor_exports(anchor_users):
    anchor, user, other = anchor_users
    set_anchor_access(anchor, [user['id']])
    before = get_client_by_id(user['id'])
    other_before = get_client_by_id(other['id'])
    version = public_vpn_version()
    auth = ('admin', 'test-password')
    path = f"/admin/clients/{user['id']}"
    with TestClient(main.app, base_url='http://panel.local') as http:
        assert http.post(path+'/block').status_code == 401
        assert http.post(path+'/block', auth=auth, headers={'Origin':'https://evil.local'}).status_code == 403
        assert get_client_by_id(user['id']) == before
        result = http.post(path+'/block', auth=auth, follow_redirects=False)
        assert result.status_code == 303
        blocked = get_client_by_id(user['id'])
        assert blocked == {**before, 'enabled':0}
        assert get_client_by_id(other['id']) == other_before
        assert public_vpn_version() == version
        assert get_vpn_state()['desired_revision'] != get_vpn_state()['applied_revision']
        for kind in ('client','sub','amnezia','clash','sing-box'):
            assert http.get(f"/{kind}/{user['token']}").status_code == 404
        assert http.get(f"/anchor/{anchor}/client/{user['token']}").status_code == 401
        page = http.get('/admin/clients?access=blocked', auth=auth).text
        assert 'Разблокировать' in page and 'заблокирован' in page and 'Синхронизировать VPN' in page
        config = capture_vpn_config()
        assert user['id'] not in {person.id for person in config.enabled_clients}
        script = build_eu_config_apply_script(config)
        assert user['vless_uuid'] not in script and user['hysteria_password'] not in script
        assert other['vless_uuid'] in script
        assert http.post(path+'/block', auth=auth, follow_redirects=False).status_code == 303
        assert http.post(path+'/unblock', auth=auth, follow_redirects=False).status_code == 303
        assert get_client_by_id(user['id']) == before
        assert http.get(f"/client/{user['token']}").status_code == 200
        assert http.get(f"/anchor/{anchor}/client/{user['token']}").status_code == 200
        assert user['vless_uuid'] in build_eu_config_apply_script(capture_vpn_config())
        assert http.post('/admin/clients/999999/block', auth=auth).status_code == 404
        assert http.post('/admin/clients/999999/unblock', auth=auth).status_code == 404
    with get_db() as db:
        assert db.execute('SELECT deleted_at FROM clients WHERE id=?',(user['id'],)).fetchone()['deleted_at'] is None
