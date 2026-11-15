import asyncio
import json
import pytest
from fastapi.testclient import TestClient
from dataclasses import replace
from test_anchor_access import anchor_users
from app import main, anchor_updates as edits
from app.anchors import anchor_client,set_anchor_access,has_anchor_access
from app.db import get_db,create_client,get_vpn_state,public_vpn_version,set_client_enabled,get_latest_install_operation
from app.vpn_config import capture_vpn_config,canonical_vpn_config_json
from app.sprintbox_accounts import save_account
from app.operation_coordinator import recover_incomplete_vps_operations

@pytest.fixture
def editable(anchor_users,monkeypatch,tmp_path):
    anchor,user,other=anchor_users
    owner=save_account(label='Anchor owner',login='private-login',key='test-key',service='',distributive='')
    with get_db() as db:db.execute('UPDATE vpn_anchors SET account_id=? WHERE anchor_id=?',(owner,anchor))
    key=tmp_path/'key';key.write_text('Test key')
    monkeypatch.setattr(edits,'managed_key_path',lambda:key)
    calls=[]
    class Provider:
        def __init__(self,*args,**kwargs):pass
        async def get_box(self,name):return {'ipv4':['203.0.113.10']}
    async def apply(operation,host,script,**kwargs):
        calls.append((host,script));return 0,''
    monkeypatch.setattr(edits,'SprintboxClient',Provider)
    monkeypatch.setattr(edits,'_run_deploy_with_heartbeat',apply)
    set_anchor_access(anchor,[user['id']])
    return anchor,user,other,calls


def test_add_people_only_to_anchor_and_publish_access_after_ack(editable):
    anchor,user,_,calls=editable
    newcomer=create_client('New person')
    before=canonical_vpn_config_json(capture_vpn_config());state=get_vpn_state();version=public_vpn_version()
    frozen=anchor_client(anchor,user['token'])[1]
    primary_install=get_latest_install_operation(primary_only=True)
    operation=edits.prepare_users(anchor,[newcomer['id']])
    assert not has_anchor_access(anchor,newcomer['token'])
    http=TestClient(main.app)
    assert 'Применяем изменения' in http.get(f"/anchor/{anchor}/client/{user['token']}").text
    assert http.get(f"/anchor/{anchor}/sub/{user['token']}").status_code==409
    asyncio.run(edits.run_edit(operation))
    assert calls[0][0]=='203.0.113.10'
    applied=anchor_client(anchor,newcomer['token'])[1]
    assert applied.amnezia==frozen.amnezia and applied.vless==frozen.vless and applied.hysteria==frozen.hysteria
    assert applied.client_by_token(user['token'])==frozen.client_by_token(user['token'])
    assert has_anchor_access(anchor,newcomer['token'])
    assert http.get(f"/anchor/{anchor}/amnezia/{newcomer['token']}").status_code==200
    assert canonical_vpn_config_json(capture_vpn_config())==before
    assert get_vpn_state()==state and public_vpn_version()==version
    assert get_latest_install_operation(primary_only=True)==primary_install
    assert edits.prepare_users(anchor,[newcomer["id"]]) is None
    assert len(calls)==1


def test_anchor_device_owner_exports_and_revoke_without_main_changes(editable):
    anchor,user,other,calls=editable
    http=TestClient(main.app)
    before=canonical_vpn_config_json(capture_vpn_config());state=get_vpn_state()
    base=f"/anchor/{anchor}"
    original=http.get(f"{base}/amnezia/{user['token']}").text
    response=http.post(f"{base}/devices/{user['token']}",data={'name':'Tablet'},follow_redirects=False)
    assert response.status_code==303
    device=int(response.headers['location'].split('device=')[1].split('#')[0])
    second=http.get(f"{base}/amnezia/{user['token']}?device={device}")
    assert second.status_code==200 and second.text!=original
    assert user['amnezia_private_key'] not in second.text
    assert http.get(f"{base}/amnezia/{user['token']}").text==original
    assert 'systemctl' not in calls[-1][1] and 'awg set awg0 peer' in calls[-1][1]
    set_anchor_access(anchor,[user['id'],other['id']])
    assert http.get(f"{base}/amnezia/{other['token']}?device={device}").status_code==404
    assert http.post(f"{base}/devices/{other['token']}/{device}/delete").status_code==409
    assert http.post(f"{base}/devices/{user['token']}/{device}/delete",headers={'Origin':'https://evil.example'}).status_code==403
    assert http.post(f"{base}/devices/{user['token']}/{device}/delete",follow_redirects=False).status_code==303
    assert http.get(f"{base}/amnezia/{user['token']}?device={device}").status_code==404
    assert http.get(f"{base}/amnezia/{user['token']}").text==original
    assert canonical_vpn_config_json(capture_vpn_config())==before and get_vpn_state()==state


def test_unconfirmed_edit_stays_quarantined_and_requires_explicit_retry(editable,monkeypatch):
    anchor,user,_,calls=editable
    device,operation=edits.prepare_anchor_device(anchor,user['token'],name='Laptop')
    async def timeout(*args,**kwargs):raise TimeoutError('lost acknowledgement')
    original=edits._run_deploy_with_heartbeat
    monkeypatch.setattr(edits,'_run_deploy_with_heartbeat',timeout)
    asyncio.run(edits.run_edit(operation))
    assert anchor_client(anchor,user['token']) is None
    assert edits.edit_state(anchor)['status']=='AMBIGUOUS'
    with pytest.raises(ValueError):edits.prepare_anchor_device(anchor,user['token'],name='Another')
    asyncio.run(edits.run_edit(operation))
    assert not calls
    retry=edits.retry_edit(anchor)
    monkeypatch.setattr(edits,'_run_deploy_with_heartbeat',original)
    asyncio.run(edits.run_edit(retry))
    assert anchor_client(anchor,user['token'])[1].amnezia_devices[0].id==device
    assert len(calls)==1


def test_restart_and_wrong_target_never_publish_or_repeat(editable,monkeypatch):
    anchor,user,_,calls=editable
    _,operation=edits.prepare_anchor_device(anchor,user['token'],name='Laptop')
    with get_db() as db:recover_incomplete_vps_operations(db,'Restart')
    asyncio.run(edits.run_edit(operation));assert not calls
    retry=edits.retry_edit(anchor)
    class Wrong:
        def __init__(self,*args,**kwargs):pass
        async def get_box(self,name):return {'ipv4':['203.0.113.20']}
    monkeypatch.setattr(edits,'SprintboxClient',Wrong)
    asyncio.run(edits.run_edit(retry))
    assert not calls and anchor_client(anchor,user['token']) is None


def test_anchor_admin_add_users_authorization_and_user_disable(editable):
    anchor,user,_,calls=editable
    newcomer=create_client('New person')
    http=TestClient(main.app,base_url='http://panel.local')
    url=f'/admin/anchors/{anchor}/users'
    assert http.post(url,data={'client_ids':newcomer['id']}).status_code==401
    assert http.post(url,data={'client_ids':newcomer['id']},auth=('admin','test-password'),headers={'Origin':'https://evil.example'}).status_code==403
    operation=edits.prepare_users(anchor,[newcomer['id']])
    set_client_enabled(newcomer['id'],False)
    asyncio.run(edits.run_edit(operation))
    assert not calls and not has_anchor_access(anchor,newcomer['token'])
    restore=edits.restore_edit(anchor)
    asyncio.run(edits.run_edit(restore))
    assert anchor_client(anchor,user['token']) is not None
    assert not has_anchor_access(anchor,newcomer['token'])


def test_router_client_is_managed_from_routers_and_anchor_links_are_first(editable):
    from app.router_credentials import create_router
    from app.db import get_client_by_id
    anchor,user,other,_=editable
    router=create_router('Living room',other['id'])
    http=TestClient(main.app)
    auth=('admin','test-password')
    clients=http.get('/admin/clients',auth=auth).text
    assert user['token'] in clients and other['token'] not in clients
    routers=http.get('/admin/routers',auth=auth).text
    assert other['token'] in routers and 'Кабинет и конфиги роутера' in routers
    changed=http.post(f"/admin/routers/{router['router_id']}/client-state",auth=auth,follow_redirects=False)
    assert changed.status_code==303 and changed.headers['location']=='/admin/routers'
    assert not get_client_by_id(other['id'])['enabled']
    page=http.get(f"/client/{user['token']}").text
    assert page.index('Якорные VPN')<page.index('<section id="vpn-status">')
    assert 'Если не открывается, попробуйте добавить подключение самостоятельно' in page
    anchor_page=http.get(f"/anchor/{anchor}/client/{user['token']}").text
    assert 'Если не открывается, попробуйте добавить подключение самостоятельно' in anchor_page


def test_explicit_retry_can_bind_rotated_api_key_for_same_owner(editable,monkeypatch):
    anchor,user,_,_=editable
    _,operation=edits.prepare_anchor_device(anchor,user['token'],name='Laptop')
    with get_db() as db:recover_incomplete_vps_operations(db,'Restart')
    with get_db() as db:db.execute("UPDATE sprintbox_accounts SET api_key='rotated-key' WHERE login='private-login'")
    retry=edits.retry_edit(anchor)
    asyncio.run(edits.run_edit(retry))
    assert anchor_client(anchor,user['token']) is not None
