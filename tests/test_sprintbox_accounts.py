from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from test_sprintbox import scenario, FakeSprintbox, OLD, NEW, run, _record_old_install, TIMING
from app import main, sprintbox_rotation as rotation, server_operations
from app.anchors import list_anchors, save_anchor, protected_box
from app.db import create_operation, get_db, get_operation, get_setting, get_vpn_state, init_db, set_setting
from app.sprintbox_accounts import (
    account, box_ref, current_id, ensure_legacy_account, list_accounts, save_account, select_account,
)
from app.sprintbox_review import pending_review, prepare_cleanup, prepare_ssh_wait, context
from app.vps_provider import sprintbox_config, bound_provider
from app.vpn_state import prepare_ip_change_operation


@pytest.fixture
def accounts(scenario, monkeypatch):
    _record_old_install()
    init_db()  # Import the previously configured account and active box.
    a = list_accounts()[0]['id']
    b = save_account(label='Новый промо', login='second-login', key='second-secret',
                     service='second-promo', distributive='ubuntu2404')
    second = FakeSprintbox()
    second.boxes.clear()
    def client(login, key, **kwargs):
        if login == 'account-login':
            assert key == 'secret-key'
            return scenario
        assert login == 'second-login' and key == 'second-secret'
        return second
    monkeypatch.setattr(rotation, 'SprintboxClient', client)
    return a, b, scenario, second


def test_selection_does_not_change_current_server_revision_or_anchor(accounts):
    a, b, first, second = accounts
    before = get_vpn_state()
    # The existing frozen anchor can coexist with the current VPN.
    with get_db() as db:
        save_anchor(db, 'account-login', 'anchor-old', '191.44.42.137', None, a)
    select_account(b)
    assert sprintbox_config()['login'] == 'second-login'
    assert sprintbox_config(current=True)['login'] == 'account-login'
    assert get_setting('current_ip') == OLD
    assert get_setting('config.sprintbox_box_name') == 'box-old'
    assert get_vpn_state() == before
    assert first.events == [] and second.events == []
    assert list_anchors()[0]['ip'] == '191.44.42.137'


def test_cross_account_rotation_keeps_old_owner_until_publication_and_saves_anchor(accounts, monkeypatch):
    a, b, first, second = accounts
    select_account(b)
    original = rotation._deploy
    async def deploy(*args):
        assert sprintbox_config(current=True)['login'] == 'account-login'
        assert get_setting('current_ip') == OLD
        await original(*args)
    monkeypatch.setattr(rotation, '_deploy', deploy)
    operation = create_operation()
    run(operation)
    row = get_operation(operation)
    assert row['action_state'] == 'AWAITING_USER_REVIEW'
    assert 'get:box-old' in first.events and 'create' not in first.events
    assert 'create' in second.events and 'get:box-old' not in second.events
    assert sprintbox_config(current=True)['login'] == 'second-login'
    assert get_setting('current_ip') == NEW
    assert pending_review()['retained_boxes'] == {box_ref(a, 'box-old'): OLD}
    prepare_cleanup(operation, keep_boxes=[box_ref(a, 'box-old')])
    asyncio.run(rotation.run_sprintbox_cleanup(operation, timing=TIMING))
    anchor = list_anchors()[0]
    assert anchor['ready'] and anchor['ip'] == OLD and len(anchor['clients']) == 1
    assert anchor['account_id'] == a and anchor['account_login'] == 'account-login'
    assert first.deleted_names == [] and second.deleted_names == []
    with get_db() as db:
        assert protected_box(db, 'account-login', 'box-old', OLD)


def test_cleanup_and_reboot_use_box_owner_even_if_purchase_selection_changed(accounts, monkeypatch):
    a, b, first, second = accounts
    select_account(b)
    operation = create_operation()
    run(operation)
    select_account(a)  # Planning the next purchase must not redirect B's management or review.
    assert server_operations._provider_client(.1).login == 'second-login'
    assert pending_review()['id'] == operation
    prepare_cleanup(operation)
    asyncio.run(rotation.run_sprintbox_cleanup(operation, timing=TIMING))
    assert get_operation(operation)['action_state'] == 'DONE'
    assert first.deleted_names == ['box-old']
    assert second.deleted_names == [] and 'box-new' in second.boxes


def test_duplicate_box_names_in_different_accounts_are_distinct(accounts):
    a, b, first, second = accounts
    # A's old box has the same name as the box B is about to create.
    first.boxes['box-new'] = first.boxes.pop('box-old') | {'name': 'box-new'}
    set_setting('config.sprintbox_box_name', 'box-new')
    select_account(b)
    op = create_operation()
    run(op)
    assert get_operation(op)['action_state'] == 'AWAITING_USER_REVIEW'
    assert pending_review()['retained_boxes'] == {box_ref(a, 'box-new'): OLD}
    # Override the legacy fake's name-only assertion; verify identities explicitly.
    async def delete(name):
        assert name == 'box-new' and first.boxes[name]['ipv4'] == [OLD]
        first.deleted_names.append(name)
        del first.boxes[name]
        return 'delete-task'
    first.delete_box = delete
    prepare_cleanup(op)
    asyncio.run(rotation.run_sprintbox_cleanup(op, timing=TIMING))
    assert get_operation(op)['action_state'] == 'DONE'
    assert first.deleted_names == ['box-new'] and second.deleted_names == []
    assert second.boxes['box-new']['ipv4'] == [NEW]


@pytest.mark.parametrize('failure', ['create-rejected', 'deploy', 'health', 'ssh'])
def test_failed_cross_account_purchase_keeps_original_vpn(accounts, failure):
    a, b, first, second = accounts
    second.fail = failure
    # The deployment/health fakes from the base scenario use the first object's failure flag.
    first.fail = failure if failure in {'deploy', 'health', 'ssh'} else ''
    select_account(b)
    op = create_operation()
    run(op)
    assert get_setting('current_ip') == OLD
    assert sprintbox_config(current=True)['login'] == 'account-login'
    assert first.deleted_names == [] and second.deleted_names == []
    if failure == 'ssh':
        first.fail = ''
        prepare_ssh_wait(op)
        run(op)
        assert get_operation(op)['action_state'] == 'AWAITING_USER_REVIEW'
        assert second.events.count('create') == 1


def test_selection_and_credentials_cannot_change_during_operation(accounts):
    a, b, first, second = accounts
    op = create_operation()
    for change in (
        lambda: select_account(b),
        lambda: save_account(account_id=a, label='Rename', login='account-login', key='new-key', service='tariff', distributive='ubuntu2404'),
    ):
        with pytest.raises(main.OperationBusyError):
            change()
    assert bound_provider(op)['account_id'] == str(a)
    assert sprintbox_config()['key'] == 'secret-key'


def test_import_preserves_frozen_anchor_snapshot_and_repeated_startup_is_idempotent(scenario):
    revision = _record_old_install()
    op = create_operation()
    run(op)
    with get_db() as db:
        save_anchor(db, 'account-login', 'box-old', OLD, revision)
        before = db.execute('SELECT payload_json FROM vpn_anchors').fetchone()['payload_json']
    init_db()
    init_db()
    assert len(list_accounts()) == 1
    assert list_anchors()[0]['ready'] and list_anchors()[0]['account_id'] == list_accounts()[0]['id']
    with get_db() as db:
        assert db.execute('SELECT payload_json FROM vpn_anchors').fetchone()['payload_json'] == before
        ctx = context(db, op)
        assert ctx['retained_boxes'] == {'1:box-old': OLD}
        assert '1:1:box-old' not in ctx['retained_snapshots']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert bound_provider(op)['account_id'] == '1'


def test_accounts_forms_require_auth_and_csrf_and_never_render_api_keys(accounts, monkeypatch):
    a, b, first, second = accounts
    set_setting('config.admin_password', 'test-password')
    class AccountClient:
        def __init__(self, login, key):
            self.login = login
        async def list_boxes(self):
            return {'box': {'name': 'box'}}
        async def _request(self, path):
            if path == 'services/list':
                return {'second-promo': {'description': 'Promo', 'fee': 429}}
            return {'ubuntu2404': {'description': 'Ubuntu 24.04'}}
    monkeypatch.setattr(main, 'SprintboxClient', AccountClient)
    with TestClient(main.app, base_url='http://panel.local') as client:
        auth = ('admin', 'test-password')
        url = f'/admin/sprintbox/accounts/{b}/select'
        assert client.get('/admin/sprintbox/accounts').status_code == 401
        assert client.post(url, follow_redirects=False).status_code == 401
        assert client.post(url, auth=auth, headers={'Origin': 'https://other.example'}).status_code == 403
        assert client.post(url, auth=auth, headers={'Origin': 'http://panel.local'}, follow_redirects=False).status_code == 303
        page = client.get(f'/admin/sprintbox/accounts?check={b}', auth=auth)
        assert page.status_code == 200 and 'second-promo' in page.text and '429 ₽/мес.' in page.text
        assert 'Доступ подтверждён' in page.text
        assert 'second-secret' not in page.text and 'secret-key' not in page.text
        assert page.headers['cache-control'] == 'private, no-store'
        add = client.post('/admin/sprintbox/accounts', auth=auth, headers={'Origin': 'http://panel.local'},
                          data={'label': 'Third', 'login': 'third-login', 'api_key': 'third-secret'}, follow_redirects=False)
        assert add.status_code == 303
        assert len(list_accounts()) == 3 and sprintbox_config()['login'] == 'second-login'


def test_sprintbox_error_message_field_is_preserved_and_credentials_redacted(monkeypatch):
    import app.sprintbox as api
    original = httpx.AsyncClient
    def response(request):
        return httpx.Response(400, json={'error': {'code': 40101, 'message': 'Unknown service secret-key'}, 'data': None})
    monkeypatch.setattr(api.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(response), **kwargs))
    with pytest.raises(api.SprintboxApiError, match='Unknown service') as exc:
        asyncio.run(api.SprintboxClient('login', 'secret-key').create_box('retired-promo', 'ubuntu2204', 'public-key', 'box'))
    assert 'secret-key' not in str(exc.value)
