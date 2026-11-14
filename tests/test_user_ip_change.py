from __future__ import annotations

import asyncio
from datetime import datetime, UTC

import pytest
from fastapi.testclient import TestClient

from test_sprintbox import scenario, run, OLD, NEW, TIMING
from app import main, sprintbox_rotation as rotation
from app.db import create_client, get_db, get_operation, get_setting, init_db, set_setting, set_client_enabled
from app.scheduled_cleanup import (dispatch_cleanups, next_cleanup, schedule_cleanup,
                                   source_label, cleanup_queue)
from app.vpn_config import capture_vpn_config
from app.vpn_state import prepare_ip_change_operation

BEFORE = datetime(2026, 11, 15, 20, 29, tzinfo=UTC)
DUE = datetime(2026, 11, 15, 20, 30, tzinfo=UTC)


@pytest.fixture
def user_flow(scenario, monkeypatch):
    set_setting('config.enable_safe_aeza_ip_rotation', '1')
    set_setting('config.admin_password', 'test-password')
    async def execute(operation_id):
        await rotation.run_sprintbox_rotation(operation_id, timing=TIMING)
    monkeypatch.setattr(main, '_run_operation_background', execute)
    async def deploy(operation_id, host, config):
        assert host == config.current_ip
        assert any(item.id == 1 for item in config.enabled_clients)
        scenario.events.append('deploy')
    monkeypatch.setattr(rotation, '_deploy', deploy)
    original = rotation.run_sprintbox_cleanup
    async def cleanup(operation_id):
        await original(operation_id, timing=TIMING)
    monkeypatch.setattr(rotation, 'run_sprintbox_cleanup', cleanup)
    monkeypatch.setattr(main, 'run_sprintbox_cleanup', cleanup)
    return scenario, capture_vpn_config().clients[0]


def test_user_pipeline_owner_retry_accept_and_source(user_flow):
    boxes, user = user_flow
    other = create_client('Другой пользователь')
    base = f'/client/{user.token}'
    with TestClient(main.app, base_url='http://panel.local') as client:
        assert 'Сменить IP' in client.get(base).text
        assert client.post('/client/invalid/ip-change').status_code == 404
        assert client.post(base+'/ip-change', headers={'Origin': 'https://evil.local'}).status_code == 403
        assert client.post(base+'/ip-change', follow_redirects=False).status_code == 303
        with get_db() as db:
            op = db.execute('SELECT max(id) AS id FROM ip_change_operations').fetchone()['id']
        assert get_operation(op)['action_state'] == 'AWAITING_USER_REVIEW', get_operation(op)['error_message']
        assert source_label(op) == f'Пользователь: {user.name} (ID {user.id})'
        assert client.post(f"/client/{other['token']}/ip-change/{op}/accept").status_code == 409
        assert client.post(f"/client/{other['token']}/ip-change").status_code == 409
        assert 'Да, всё ок' in client.get(base).text
        assert client.post(f'{base}/ip-change/{op}/retry', follow_redirects=False).status_code == 303
        with get_db() as db:
            second = db.execute('SELECT max(id) AS id FROM ip_change_operations').fetchone()['id']
        assert get_operation(op)['action_state'] == 'RETRY_REQUESTED'
        assert get_operation(second)['action_state'] == 'AWAITING_USER_REVIEW'
        assert source_label(second) == source_label(op)
        assert client.post(f'{base}/ip-change/{second}/accept', follow_redirects=False).status_code == 303
        assert boxes.deleted_names == []
        assert get_operation(second)['action_state'] == 'CLEANUP_SCHEDULED'
        assert client.post(f'{base}/ip-change/{second}/accept').status_code == 409
        assert '23:30' in client.get(base).text
        page = client.get('/admin', auth=('admin', 'test-password'))
        assert 'Удалить сейчас' in page.text and user.name in page.text
        history = client.get('/admin/operations', auth=('admin', 'test-password'))
        assert user.name in history.text
        assert f'{user.token}' not in page.text


def test_scheduled_cleanup_survives_restart_and_runs_only_at_night(user_flow):
    boxes, user = user_flow
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    schedule_cleanup(op, client_id=user.id, now=BEFORE)
    init_db()
    assert cleanup_queue()[0]['cleanup_at'] == DUE.isoformat(timespec='seconds')
    asyncio.run(dispatch_cleanups(BEFORE))
    asyncio.run(dispatch_cleanups(DUE.replace(hour=21)))
    assert boxes.deleted_names == []
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == ['box-old']
    assert get_setting('current_ip') == NEW
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == ['box-old']


def test_cleanup_now_admin_only_csrf_and_single_use(user_flow):
    boxes, user = user_flow
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    schedule_cleanup(op, now=BEFORE)
    auth = ('admin', 'test-password')
    url = f'/admin/sprintbox/{op}/cleanup-now'
    with TestClient(main.app, base_url='http://panel.local') as client:
        assert client.post(url).status_code == 401
        assert client.post(url, auth=auth, headers={'Origin': 'https://evil.local'}).status_code == 403
        assert boxes.deleted_names == []
        assert client.post(url, auth=auth, follow_redirects=False).status_code == 303
        assert boxes.deleted_names == ['box-old']
        assert client.post(url, auth=auth).status_code == 409


def test_prior_scheduled_cleanup_remains_safe_after_another_rotation(user_flow):
    boxes, user = user_flow
    first = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(first)
    schedule_cleanup(first, now=BEFORE)
    second = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(second)
    assert get_setting('current_ip') != NEW
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == ['box-old']
    assert 'box-new' in boxes.boxes and 'box-new2' in boxes.boxes
    assert get_operation(second)['action_state'] == 'AWAITING_USER_REVIEW'
    schedule_cleanup(second, now=BEFORE)
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == ['box-old', 'box-new']
    assert set(boxes.boxes) == {'box-new2'}


def test_disabled_user_cannot_start_or_confirm(user_flow):
    boxes, user = user_flow
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    set_client_enabled(user.id, False)
    with TestClient(main.app) as client:
        assert client.post(f'/client/{user.token}/ip-change').status_code == 404
        assert client.post(f'/client/{user.token}/ip-change/{op}/accept').status_code == 404
    assert boxes.deleted_names == []


def test_next_window_and_admin_source(user_flow):
    assert next_cleanup(BEFORE) == '2026-11-15T20:30:00+00:00'
    assert next_cleanup(DUE) == '2026-11-16T20:30:00+00:00'
    op = prepare_ip_change_operation().operation_id
    assert source_label(op) == 'Администратор'


def test_busy_cleanup_waits_for_next_window(user_flow):
    boxes, user = user_flow
    first = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(first)
    schedule_cleanup(first, now=BEFORE)
    second = prepare_ip_change_operation(user_client_id=user.id).operation_id
    asyncio.run(dispatch_cleanups(DUE))
    assert get_operation(first)['action_state'] == 'CLEANUP_SCHEDULED'
    assert boxes.deleted_names == []
    run(second)
    asyncio.run(dispatch_cleanups(DUE.replace(hour=21)))
    assert boxes.deleted_names == []
    asyncio.run(dispatch_cleanups(DUE.replace(day=16)))
    assert boxes.deleted_names == ['box-old']


def test_scheduled_cleanup_cannot_delete_server_that_became_primary(user_flow):
    boxes, user = user_flow
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    schedule_cleanup(op, now=BEFORE)
    set_setting('current_ip', OLD)
    set_setting('config.sprintbox_box_name', 'box-old')
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == []
    assert get_operation(op)['action_state'] == 'CLEANUP_REJECTED'


def test_scheduled_anchor_survives_cleanup_and_user_page_hides_it(user_flow):
    from test_sprintbox import _record_old_install
    from app.anchors import list_anchors
    boxes, user = user_flow
    _record_old_install()
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(op)
    schedule_cleanup(op, keep_boxes=['box-old'], now=BEFORE)
    asyncio.run(dispatch_cleanups(DUE))
    assert boxes.deleted_names == [] and list_anchors()
    with TestClient(main.app) as client:
        page = client.get(f'/client/{user.token}')
        assert 'Якорные VPN' not in page.text and '/anchor/' not in page.text
        anchor = list_anchors()[0]['anchor_id']
        assert client.get(f'/anchor/{anchor}/client/{user.token}').status_code == 401
        assert client.get(f'/anchor/{anchor}/client/{user.token}', auth=('admin', 'test-password')).status_code == 200


def test_overlapping_schedules_do_not_repeat_uncertain_deletion(user_flow):
    boxes, user = user_flow
    first = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(first)
    schedule_cleanup(first, now=BEFORE)
    second = prepare_ip_change_operation(user_client_id=user.id).operation_id
    run(second)
    schedule_cleanup(second, now=BEFORE)
    boxes.fail = 'delete-timeout'
    asyncio.run(dispatch_cleanups(DUE))
    # One attempt for box-old, then one for box-new; box-old is not resent.
    assert boxes.events.count('delete') == 2
    assert get_operation(first)['action_state'] == 'CLEANUP_AMBIGUOUS'
    assert get_operation(second)['action_state'] == 'CLEANUP_AMBIGUOUS'


def test_source_name_is_preserved_after_rename(user_flow):
    boxes, user = user_flow
    op = prepare_ip_change_operation(user_client_id=user.id).operation_id
    with get_db() as db:
        db.execute('UPDATE clients SET name=? WHERE id=?', ('Новое имя', user.id))
    assert user.name in source_label(op) and 'Новое имя' not in source_label(op)


def test_user_ssh_wait_resumes_without_buying_another_box(user_flow):
    boxes, user = user_flow
    boxes.fail = 'ssh'
    base = f'/client/{user.token}'
    with TestClient(main.app, base_url='http://panel.local') as client:
        assert client.post(base+'/ip-change', follow_redirects=False).status_code == 303
        with get_db() as db:
            op = db.execute('SELECT max(id) AS id FROM ip_change_operations').fetchone()['id']
        assert get_operation(op)['action_state'] == 'SSH_UNREACHABLE'
        assert 'Подождать ещё' in client.get(base).text
        boxes.fail = ''
        assert client.post(f'{base}/ip-change/{op}/ssh-wait', follow_redirects=False).status_code == 303
        assert boxes.events.count('create') == 1
        assert get_operation(op)['action_state'] == 'AWAITING_USER_REVIEW'


def test_stale_admin_review_does_not_block_future_user_changes(user_flow):
    boxes, user = user_flow
    first = prepare_ip_change_operation().operation_id
    run(first)
    second = prepare_ip_change_operation().operation_id
    run(second)
    schedule_cleanup(second, now=BEFORE)
    assert get_operation(first)['action_state'] == 'AWAITING_USER_REVIEW'
    third = prepare_ip_change_operation(user_client_id=user.id).operation_id
    assert third > second
