from html.parser import HTMLParser
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import main
from app.anchors import set_anchor_access
from app.db import init_db, create_client, set_setting, set_client_enabled
from app.hysteria_auth import hysteria_auth
from app.subscriptions import render_connection_keys, render_subscription
from app.vpn_config import capture_vpn_config
from test_anchor_access import anchor_users


class Fields(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.fields = {}
        self.active = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'textarea' and attrs.get('id') in {'vless-key', 'hysteria-key'}:
            self.active = attrs['id']
            self.fields[self.active] = ''
            assert 'readonly' in attrs and 'hidden' not in attrs

    def handle_endtag(self, tag):
        if tag == 'textarea': self.active = None

    def handle_data(self, value):
        if self.active: self.fields[self.active] += value


@pytest.fixture
def user():
    init_db()
    set_setting('current_ip', '203.0.113.10')
    set_setting('config.vless_enabled', '1')
    set_setting('config.hysteria_enabled', '1')
    set_setting('config.hysteria_obfs_password', 'secret & with spaces')
    return create_client('Артём')


def test_direct_keys_are_individual_imports_with_current_identity_and_transport(user):
    config = capture_vpn_config()
    keys = render_connection_keys(config, user)
    assert list(keys) == ['vless', 'hysteria']
    assert list(keys.values()) == render_subscription(config, user).splitlines()
    vless = urlsplit(keys['vless'])
    hysteria = urlsplit(keys['hysteria'])
    assert vless.scheme == 'vless' and vless.hostname == config.current_ip
    assert vless.username == user['vless_uuid'] and vless.port == config.vless.protocol.port
    assert parse_qs(vless.query)['pbk'] == [config.vless.public_key]
    assert hysteria.scheme == 'hy2' and hysteria.hostname == config.current_ip
    assert unquote(hysteria.netloc.rsplit('@',1)[0]) == hysteria_auth(user, config)
    assert parse_qs(hysteria.query)['obfs-password'] == [config.hysteria.obfs_password]


def test_user_config_tab_contains_two_visible_fields_and_only_own_keys(user):
    other = create_client('Другой')
    with TestClient(main.app) as http:
        page = http.get(f"/client/{user['token']}")
        assert page.status_code == 200
        fields = Fields(page.text).fields
        assert fields == {name+'-key':key for name,key in render_connection_keys(capture_vpn_config(), user).items()}
        assert 'data-connection-tab="keys"' in page.text
        assert 'aria-label="Копировать VLESS"' in page.text
        assert other['vless_uuid'] not in page.text and other['hysteria_password'] not in page.text
        assert page.headers['cache-control'] == 'private, no-store'
        set_client_enabled(user['id'], False)
        assert http.get(f"/client/{user['token']}").status_code == 404


def test_disabled_protocol_and_missing_ip_do_not_expose_unusable_keys(user):
    set_setting('config.hysteria_enabled', '0')
    with TestClient(main.app) as http:
        assert list(Fields(http.get(f"/client/{user['token']}").text).fields) == ['vless-key']
    set_setting('current_ip', '')
    assert render_connection_keys(capture_vpn_config(), user) == {}


def test_anchor_keys_use_frozen_ip_and_respect_grant_revocation(anchor_users):
    anchor, user, other = anchor_users
    set_anchor_access(anchor, [user['id']])
    path = f"/anchor/{anchor}/client/{user['token']}"
    with TestClient(main.app) as http:
        page = http.get(path)
        fields = Fields(page.text).fields
        assert page.status_code == 200 and len(fields) == 2
        assert all(urlsplit(key).hostname == '203.0.113.10' for key in fields.values())
        assert urlsplit(fields['vless-key']).username == user['vless_uuid']
        assert other['hysteria_password'] not in page.text
        assert http.get(f"/anchor/{anchor}/client/{other['token']}").status_code == 401
        set_anchor_access(anchor, [])
        assert http.get(path).status_code == 401
