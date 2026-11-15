import pytest
import yaml
from fastapi.testclient import TestClient
from urllib.parse import urlsplit
from app import main
from app.db import init_db,set_setting,create_client,get_db,get_vpn_state,set_client_enabled
from app.anchors import save_anchor,set_anchor_access
from app.vpn_state import prepare_install_operation,complete_install_operation

@pytest.fixture
def anchor_users():
    init_db()
    set_setting('config.admin_password','test-password')
    set_setting('current_ip','203.0.113.10')
    first=create_client('Alice');second=create_client('Bob')
    op=prepare_install_operation('203.0.113.10')
    complete_install_operation(op.operation_id,'Applied')
    set_setting('current_ip','203.0.113.20')
    with get_db() as db: anchor=save_anchor(db,'private-login','old-box','203.0.113.10',op.revision)
    return anchor,first,second

def test_grant_revoke_all_exports_and_frozen_clash(anchor_users):
    anchor,user,other=anchor_users
    http=TestClient(main.app,base_url='http://panel.local')
    base=f'/anchor/{anchor}'
    kinds=('client','sub','sing-box','clash','clash-provider','amnezia','amnezia-key','subscription.qr','amnezia.qr')
    before=get_vpn_state()
    assert http.post(f'/admin/anchors/{anchor}/access',data={'client_ids':user['id']}).status_code==401
    assert http.post(f'/admin/anchors/{anchor}/access',data={'client_ids':user['id']},auth=('admin','test-password'),headers={'Origin':'https://evil.example'}).status_code==403
    granted=http.post(f'/admin/anchors/{anchor}/access',data={'client_ids':user['id']},auth=('admin','test-password'),follow_redirects=False)
    assert granted.status_code==303
    assert f'{base}/client/' in http.get(f"/client/{user['token']}").text
    assert base not in http.get(f"/client/{other['token']}").text
    for kind in kinds:
        response=http.get(f"{base}/{kind}/{user['token']}")
        assert response.status_code==200
        assert response.headers['cache-control']=='private, no-store'
        assert http.get(f"{base}/{kind}/{other['token']}").status_code==401
    profile=yaml.safe_load(http.get(f"{base}/clash/{user['token']}").text)
    provider=next(iter(profile['proxy-providers'].values()))
    assert urlsplit(provider['url']).path==f"{base}/clash-provider/{user['token']}"
    nodes=yaml.safe_load(http.get(urlsplit(provider['url']).path).text)['proxies']
    assert nodes and all(node['server']=='203.0.113.10' for node in nodes)
    assert '203.0.113.20' not in http.get(f"{base}/amnezia/{user['token']}").text
    assert other['amnezia_private_key'] not in http.get(f"{base}/client/{user['token']}").text
    assert http.post(f'/admin/anchors/{anchor}/access',auth=('admin','test-password'),follow_redirects=False).status_code==303
    assert base not in http.get(f"/client/{user['token']}").text
    for kind in kinds: assert http.get(f"{base}/{kind}/{user['token']}").status_code==401
    assert http.get(f"{base}/client/{user['token']}",auth=('admin','test-password')).status_code==200
    assert get_vpn_state()==before

def test_cannot_grant_missing_or_disabled_clients_or_serve_maintenance(anchor_users):
    anchor,user,_=anchor_users
    later=create_client('New client')
    with pytest.raises(ValueError):set_anchor_access(anchor,[later['id']])
    set_anchor_access(anchor,[user['id']])
    set_client_enabled(user['id'],False)
    with pytest.raises(ValueError):set_anchor_access(anchor,[user['id']])
    http=TestClient(main.app)
    assert http.get(f"/anchor/{anchor}/amnezia/{user['token']}").status_code!=200
    set_client_enabled(user['id'],True)
    with get_db() as db:db.execute('UPDATE vpn_anchors SET maintenance_operation_id=99 WHERE anchor_id=?',(anchor,))
    assert http.get(f"/anchor/{anchor}/sub/{user['token']}").status_code==404
    assert anchor not in http.get(f"/client/{user['token']}").text
    set_anchor_access(anchor, [])
