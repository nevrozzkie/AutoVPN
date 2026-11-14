from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from test_sprintbox import scenario, OLD, NEW, TIMING, run
from test_sprintbox_accounts import accounts
from app import main, sprintbox_controls as controls, sprintbox_rotation as rotation
from app.anchors import list_anchors, save_anchor, anchor_client
from app.db import get_db, get_operation, get_setting, get_vpn_state, update_operation, set_setting
from app.operation_coordinator import recover_incomplete_vps_operations
from app.sprintbox_accounts import account, select_account
from app.vps_provider import sprintbox_config
from app.vpn_state import prepare_ip_change_operation
from app.vpn_config import capture_vpn_config, canonical_vpn_config_json

TARGET = '203.0.113.80'


@pytest.fixture
def box_controls(accounts, monkeypatch):
    a,b,first,second=accounts
    set_setting('config.enable_transactional_vpn_apply','1')
    monkeypatch.setattr(controls,'SprintboxClient',rotation.SprintboxClient)
    monkeypatch.setattr(controls,'_verify_protocol_health',rotation._verify_protocol_health)
    config=replace(capture_vpn_config(),current_ip=TARGET,revision=200)
    payload=canonical_vpn_config_json(config)
    with get_db() as db:
        db.execute("INSERT INTO vpn_snapshots(revision,payload_json,payload_sha256,lifecycle,prepared_at,applied_at) "
                   "VALUES (200,?,?,'APPLIED','test','test')",(payload,hashlib.sha256(payload.encode()).hexdigest()))
        anchor=save_anchor(db,'second-login','box-anchor',TARGET,200,b)
    second.boxes['box-anchor']={'name':'box-anchor','ipv4':[TARGET],'description':'Existing anchor'}
    return a,b,first,second,anchor,config


def promote(anchor):
    op=prepare_ip_change_operation(anchor_id=anchor).operation_id
    run(op)
    return op


def delete_plan(b,name='box-anchor'):
    return asyncio.run(controls.prepare_deletion(b,name))


def delete_run(op):
    asyncio.run(controls.run_deletion(op,timing=TIMING))


def test_promote_anchor_uses_owner_without_purchase_and_freezes_previous_main(box_controls,monkeypatch):
    a,b,first,second,anchor,config=box_controls
    original=rotation._deploy
    async def deploy(*args):
        assert get_setting('current_ip')==OLD
        assert not list_anchors()[0]['ready']
        assert anchor_client(anchor,config.clients[0].token) is None
        await original(*args)
    monkeypatch.setattr(rotation,'_deploy',deploy)
    op=promote(anchor)
    assert get_operation(op)['status']=='DONE'
    assert get_setting('current_ip')==TARGET
    assert sprintbox_config(current=True)['account_id']==str(b)
    assert sprintbox_config()['account_id']==str(a)  # Purchase selection is independent.
    assert first.deleted_names==[] and second.deleted_names==[]
    assert 'create' not in first.events+second.events
    saved=list_anchors()
    assert len(saved)==1 and saved[0]['ip']==OLD and saved[0]['ready'] and saved[0]['account_id']==a
    assert get_vpn_state()['applied_revision']==get_vpn_state()['desired_revision']
    count=first.events.count('deploy')
    run(op)
    assert first.events.count('deploy')==count


@pytest.mark.parametrize('failure',['deploy','health','identity'])
def test_failed_promotion_keeps_primary_and_quarantines_modified_anchor(box_controls,failure):
    a,b,first,second,anchor,config=box_controls
    if failure=='identity':
        second.boxes['box-anchor']['ipv4']=['203.0.113.81']
    else:
        first.fail=failure
    before=get_vpn_state()
    op=promote(anchor)
    assert get_operation(op)['status']=='FAILED'
    assert get_setting('current_ip')==OLD
    assert sprintbox_config(current=True)['account_id']==str(a)
    assert get_vpn_state()['applied_revision']==before['applied_revision']
    assert list_anchors()[0]['ready']==(failure=='identity')
    assert 'create' not in first.events+second.events and first.deleted_names+second.deleted_names==[]


def test_anchor_retry_after_failure_and_restart_never_auto_repeats_install(box_controls):
    a,b,first,second,anchor,config=box_controls
    op=prepare_ip_change_operation(anchor_id=anchor).operation_id
    with get_db() as db:
        db.execute('UPDATE vpn_anchors SET maintenance_operation_id=? WHERE anchor_id=?',(op,anchor))
        db.execute("UPDATE ip_change_operations SET status='RUNNING',apply_state='RUNNING' WHERE id=?",(op,))
        recover_incomplete_vps_operations(db,'Restart during promotion')
    assert get_operation(op)['status']=='FAILED'
    assert not list_anchors()[0]['ready'] and list_anchors()[0]['can_promote']
    run(op)
    assert 'deploy' not in first.events
    next_op=promote(anchor)
    assert get_operation(next_op)['status']=='DONE'


def test_manual_delete_needs_anchor_confirmation_and_preserves_profiles_until_absent(box_controls):
    a,b,first,second,anchor,config=box_controls
    op=delete_plan(b)
    assert controls.deletion(op)['status']=='CONFIRM' and 'delete' not in second.events
    with pytest.raises(ValueError): controls.confirm_deletion(op,'box-anchor',False)
    assert list_anchors()[0]['ready']
    controls.confirm_deletion(op,'box-anchor',True)
    with pytest.raises(ValueError): controls.confirm_deletion(op,'box-anchor',True)
    delete_run(op)
    assert controls.deletion(op)['status']=='DONE'
    assert list_anchors()==[]
    assert second.deleted_names==['box-anchor'] and first.deleted_names==[]
    delete_run(op)
    assert second.deleted_names==['box-anchor']
    assert get_setting('current_ip')==OLD


def test_cannot_delete_primary_even_with_forged_form(box_controls):
    a,b,first,second,anchor,config=box_controls
    with pytest.raises(ValueError,match='основной VPN'):
        delete_plan(a,'box-old')
    op=delete_plan(b)
    set_setting('current_ip',TARGET)
    with pytest.raises(ValueError): controls.confirm_deletion(op,'box-anchor',True)
    assert second.deleted_names==[]


def test_changed_box_ip_after_confirmation_is_not_deleted(box_controls):
    a,b,first,second,anchor,config=box_controls
    op=delete_plan(b)
    controls.confirm_deletion(op,'box-anchor',True)
    second.boxes['box-anchor']['ipv4']=['203.0.113.81']
    delete_run(op)
    assert controls.deletion(op)['status']=='FAILED'
    assert list_anchors()[0]['ready'] and second.deleted_names==[]


@pytest.mark.parametrize('failure',['delete-timeout','delete-rejected'])
def test_delete_error_preserves_anchor_and_does_not_repeat_request(box_controls,failure):
    a,b,first,second,anchor,config=box_controls
    second.fail=failure
    op=delete_plan(b)
    controls.confirm_deletion(op,'box-anchor',True)
    delete_run(op)
    assert controls.deletion(op)['status']==('AMBIGUOUS' if failure=='delete-timeout' else 'FAILED')
    assert list_anchors()[0]['ready']
    delete_run(op)
    assert second.events.count('delete')==1
    if failure=='delete-timeout':
        retry=delete_plan(b)
        assert controls.deletion(retry)['status']=='CONFIRM'
        assert second.events.count('delete')==1
        second.boxes.pop('box-anchor')  # Provider eventually completed the original request.
        asyncio.run(controls.recheck_deletion(op))
        assert controls.deletion(op)['status']=='DONE' and list_anchors()==[]


def test_restart_marks_sent_deletion_uncertain_and_releases_lease(box_controls):
    a,b,first,second,anchor,config=box_controls
    op=delete_plan(b)
    controls.confirm_deletion(op,'box-anchor',True)
    controls._update_delete(op,status='RUNNING',action_state='SENDING')
    with get_db() as db:
        recover_incomplete_vps_operations(db,'Restart')
        assert db.execute('SELECT * FROM operation_leases').fetchall()==[]
    assert controls.deletion(op)['status']=='AMBIGUOUS'
    delete_run(op)
    assert 'delete' not in second.events


def test_delete_and_promote_are_mutually_exclusive(box_controls):
    a,b,first,second,anchor,config=box_controls
    op=delete_plan(b)
    controls.confirm_deletion(op,'box-anchor',True)
    with pytest.raises(main.OperationBusyError): prepare_ip_change_operation(anchor_id=anchor)
    with pytest.raises(main.OperationBusyError): select_account(b)
    assert 'delete' not in second.events


def test_destructive_ui_requires_auth_csrf_typed_identity_and_separate_anchor_consent(box_controls):
    a,b,first,second,anchor,config=box_controls
    set_setting('config.admin_password','test-password')
    auth=('admin','test-password')
    with TestClient(main.app,base_url='http://panel.local') as client:
        url=f'/admin/sprintbox/accounts/{b}/boxes/box-anchor/delete/confirm'
        assert client.get(url,follow_redirects=False).status_code==401
        confirm=client.get(url,auth=auth)
        assert confirm.status_code==200
        assert 'безвозвратно' in confirm.text and 'name="delete_anchor"' in confirm.text
        op=int(str(confirm.url).rsplit('/',1)[1])
        post=f'/admin/sprintbox/deletions/{op}/confirm'
        assert client.post(post,data={'confirm_name':'box-anchor','delete_anchor':'on'},follow_redirects=False).status_code==401
        assert client.post(post,auth=auth,data={'confirm_name':'box-anchor','delete_anchor':'on'},headers={'Origin':'https://other.example'}).status_code==403
        assert client.post(post,auth=auth,data={'confirm_name':'wrong'},headers={'Origin':'http://panel.local'}).status_code==409
        assert client.post(post,auth=auth,data={'confirm_name':'box-anchor'},headers={'Origin':'http://panel.local'}).status_code==409
        assert second.deleted_names==[]
        assert 'second-secret' not in confirm.text and config.vless.private_key not in confirm.text
        result=client.post(post,auth=auth,data={'confirm_name':'box-anchor','delete_anchor':'on'},
                           headers={'Origin':'http://panel.local'},follow_redirects=False)
        assert result.status_code==303 and controls.deletion(op)['status']=='DONE'
        assert second.deleted_names==['box-anchor'] and first.deleted_names==[]


def test_promotion_ui_no_secret_and_typed_ip(box_controls,monkeypatch):
    a,b,first,second,anchor,config=box_controls
    set_setting('config.admin_password','test-password')
    scheduled=[]
    async def record(op): scheduled.append(op)
    monkeypatch.setattr(main,'_run_operation_background',record)
    with TestClient(main.app,base_url='http://panel.local') as client:
        auth=('admin','test-password')
        page=client.get(f'/admin/anchors/{anchor}/promote/confirm',auth=auth)
        assert page.status_code==200 and TARGET in page.text and 'Прежний основной' in page.text
        assert 'second-secret' not in page.text and config.vless.private_key not in page.text
        post=f'/admin/anchors/{anchor}/promote'
        assert client.post(post,auth=auth,data={'confirm_ip':'wrong'},headers={'Origin':'http://panel.local'}).status_code==409
        r=client.post(post,auth=auth,data={'confirm_ip':TARGET},headers={'Origin':'http://panel.local'},follow_redirects=False)
        assert r.status_code==303 and len(scheduled)==1
        assert get_setting('current_ip')==OLD and 'create' not in second.events


def test_same_box_name_on_different_account_does_not_delete_primary(box_controls):
    a,b,first,second,anchor,config=box_controls
    second.boxes['box-old']={'name':'box-old','ipv4':[TARGET]}
    async def remove(name):
        assert name=='box-old'
        assert get_setting('current_ip')==OLD and name in first.boxes
        second.deleted_names.append(name)
        del second.boxes[name]
        return 'delete-task'
    second.delete_box=remove
    op=delete_plan(b,'box-old')
    controls.confirm_deletion(op,'box-old',False)
    delete_run(op)
    assert controls.deletion(op)['status']=='DONE'
    assert 'box-old' in first.boxes and first.deleted_names==[]


def test_changed_desired_revision_during_promotion_never_publishes_stale_config(box_controls,monkeypatch):
    a,b,first,second,anchor,config=box_controls
    from app.db import set_settings
    async def health(*args):
        set_settings({'config.vless_port':'8443'},mark_vpn_config_updated=True)
        return '{}'
    monkeypatch.setattr(controls,'_verify_protocol_health',health)
    op=promote(anchor)
    assert get_operation(op)['status']=='FAILED'
    assert get_setting('current_ip')==OLD
    assert not list_anchors()[0]['ready']
