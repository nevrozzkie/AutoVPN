from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.db import init_db, create_client, set_setting, set_client_enabled


@pytest.fixture
def page_client():
    init_db()
    set_setting('current_ip', '203.0.113.10')
    user = create_client('PWA user')
    with TestClient(app) as client:
        yield client, user


def test_personal_manifest_install_identity_and_private_headers(page_client):
    client, user = page_client
    path = f"/client/{user['token']}"
    page = client.get(path)
    assert page.status_code == 200
    assert f'{path}/manifest.webmanifest' in page.text
    assert '/static/pwa/install.js' in page.text
    response = client.get(path+'/manifest.webmanifest')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/manifest+json'
    assert response.headers['cache-control'] == 'private, no-store'
    manifest = response.json()
    assert manifest['id'] == manifest['start_url'] == path
    assert manifest['scope'] == '/client/'
    assert manifest['display'] == 'standalone'
    assert manifest['name'] == 'AutoVPN'
    assert 'PWA user' not in response.text
    for icon in manifest['icons']:
        result = client.get(icon['src'])
        with Image.open(BytesIO(result.content)) as image:
            assert f'{image.width}x{image.height}' == icon['sizes']
    other = create_client('Other')
    assert client.get(f"/client/{other['token']}/manifest.webmanifest").json()['id'] != manifest['id']


def test_manifest_rejects_invalid_or_disabled_user(page_client):
    client, user = page_client
    assert client.get('/client/not-a-token/manifest.webmanifest').status_code == 404
    set_client_enabled(user['id'], False)
    response = client.get(f"/client/{user['token']}/manifest.webmanifest")
    assert response.status_code == 404
    assert response.headers['cache-control'] == 'private, no-store'


def test_worker_scope_excludes_admin_and_is_updatable(page_client):
    client, _ = page_client
    response = client.get('/client/service-worker.js')
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/javascript')
    assert response.headers['service-worker-allowed'] == '/client/'
    assert response.headers['cache-control'] == 'private, no-store'
    assert 'Content-Security-Policy' in response.headers
    assert 'caches.open' not in response.text
    assert client.get('/static/pwa/icon-180.png').status_code == 200


def test_android_download_is_real_apk_and_no_signing_material_is_public(page_client):
    client, user = page_client
    response = client.get('/static/downloads/AutoVPN-0.2.0.apk')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/vnd.android.package-archive'
    with ZipFile(BytesIO(response.content)) as apk:
        assert 'AndroidManifest.xml' in apk.namelist()
        assert 'classes.dex' in apk.namelist()
    assert '/static/downloads/AutoVPN-0.2.0.apk' in client.get(f"/client/{user['token']}").text
    assert client.get('/static/downloads/release.jks').status_code == 404


def test_pwa_import_links_preserve_subscription_url_and_amnezia_key(page_client):
    from html.parser import HTMLParser
    from urllib.parse import urlsplit, parse_qs
    from app.db import update_client_name

    class Links(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hrefs = []
            self.key = ''
            self.in_key = False
        def handle_starttag(self, tag, attrs):
            values = dict(attrs)
            if tag == 'a': self.hrefs.append(values.get('href', ''))
            if tag == 'textarea' and values.get('id') == 'amnezia-key': self.in_key = True
        def handle_endtag(self, tag):
            if tag == 'textarea': self.in_key = False
        def handle_data(self, data):
            if self.in_key: self.key += data

    client, user = page_client
    name = 'Телефон & name=чужое # "ключ"'
    update_client_name(user['id'], name)
    set_setting('config.vless_enabled', '1')
    set_setting('config.amnezia_enabled', '1')
    page = client.get(f"/client/{user['token']}")
    parsed = Links()
    parsed.feed(page.text)
    neko = next(url for url in parsed.hrefs if url.startswith('sn://subscription?'))
    assert parse_qs(urlsplit(neko).query) == {
        'url': [f"http://testserver/sub/{user['token']}"],
        'name': ['AutoVPN · '+name],
    }
    amnezia = next(url for url in parsed.hrefs if url.startswith('vpn://'))
    assert amnezia == parsed.key and len(amnezia) > 100
    assert 'Скачать APK · 0.2.0' in page.text


def test_disabled_protocols_do_not_offer_import_buttons(page_client):
    client, user = page_client
    for protocol in ('vless', 'hysteria', 'amnezia'):
        set_setting(f'config.{protocol}_enabled', '0')
    page = client.get(f"/client/{user['token']}")
    assert 'Открыть в NekoBox' not in page.text
    assert 'Открыть в AmneziaVPN' not in page.text
    assert 'Скачать APK · 0.2.0' in page.text
