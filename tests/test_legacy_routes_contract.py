from __future__ import annotations

import base64
import json
import os
import sqlite3
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import settings
from app.db import get_client_by_id, get_db, init_db, set_setting


TOKEN = "legacy-token-keep-me"
CURRENT_IP = "203.0.113.10"
CLIENT_UUID = "00000000-1111-2222-3333-444444444444"


@pytest.fixture
def legacy_client() -> TestClient:
    init_db()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO clients(
                id, name, token, enabled, vless_uuid, hysteria_password,
                amnezia_private_key, amnezia_public_key,
                amnezia_preshared_key, amnezia_ipv4, created_at
            ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                7,
                "Alice",
                TOKEN,
                CLIENT_UUID,
                "unused-per-client-hysteria-password",
                "client-private-key",
                "client-public-key",
                "client-preshared-key",
                "10.66.66.8",
                "2026-01-02T03:04:05+00:00",
            ),
        )
    deterministic_settings = {
        "current_ip": CURRENT_IP,
        "vless.reality_public_key": "reality-public-key",
        "vless.reality_short_id": "0123456789abcdef",
        "hysteria.password": "shared-hysteria-password",
        "hysteria.obfs_password": "obfs-password",
        "amnezia.server_public_key": "server-public-key",
        "amnezia.jc": "4",
        "amnezia.jmin": "30",
        "amnezia.jmax": "900",
        "amnezia.s1": "64",
        "amnezia.s2": "128",
        "amnezia.h1": "1",
        "amnezia.h2": "2",
        "amnezia.h3": "3",
        "amnezia.h4": "4",
        "config.vless_enabled": "1",
        "config.vless_port": "443",
        "config.hysteria_enabled": "1",
        "config.hysteria_port": "8443",
        "config.amnezia_enabled": "1",
        "config.amnezia_port": "51820",
    }
    for key, value in deterministic_settings.items():
        set_setting(key, value)
    return TestClient(main.app, base_url="http://panel.local")


def test_suite_database_is_an_external_temporary_file() -> None:
    database_path = Path(settings.database_path).resolve()

    assert Path(os.environ["DATABASE_PATH"]).resolve() == database_path
    assert database_path.name == "autovpn.sqlite3"
    assert database_path.parent.name.startswith("autovpn-tests-")
    assert database_path != (Path.cwd() / "data" / "autovpn.sqlite3").resolve()


def test_route_inventory_is_backward_compatible() -> None:
    inventory = {
        (
            tuple(sorted(getattr(route, "methods", None) or ())),
            route.path,
            route.name,
        )
        for route in main.app.routes
        if hasattr(route, "path")
    }

    legacy_routes = {
        (("GET", "HEAD"), "/openapi.json", "openapi"),
        (("GET", "HEAD"), "/docs", "swagger_ui_html"),
        (("GET", "HEAD"), "/docs/oauth2-redirect", "swagger_ui_redirect"),
        (("GET", "HEAD"), "/redoc", "redoc_html"),
        ((), "/static", "static"),
        (("GET",), "/", "root"),
        (("GET",), "/setup", "setup_page_redirect"),
        (("GET",), "/admin/setup", "admin_setup_page"),
        (("POST",), "/admin/setup", "setup_submit"),
        (("POST",), "/setup", "setup_submit"),
        (("GET",), "/healthz", "healthz"),
        (("GET",), "/robots.txt", "robots_txt"),
        (("GET",), "/admin", "admin_dashboard"),
        (("GET",), "/admin/ip/confirm", "confirm_ip_refresh"),
        (("POST",), "/admin/ip/refresh", "refresh_ip"),
        (("GET",), "/admin/ip", "admin_ip_manager"),
        (("GET",), "/admin/ip/buy/confirm", "admin_ip_buy_confirm"),
        (("POST",), "/admin/ip/buy", "admin_ip_buy"),
        (("POST",), "/admin/ip/{ipv4_id}/make-main", "admin_ip_make_main"),
        (("POST",), "/admin/ip/{ipv4_id}/delete", "admin_ip_delete"),
        (("GET",), "/admin/clients", "admin_clients"),
        (("POST",), "/admin/clients", "admin_create_client"),
        (("POST",), "/admin/clients/{client_id}/rename", "admin_rename_client"),
        (("POST",), "/admin/clients/{client_id}/enable", "admin_enable_client"),
        (("POST",), "/admin/clients/{client_id}/disable", "admin_disable_client"),
        (("POST",), "/admin/clients/{client_id}/delete", "admin_delete_client"),
        (("GET",), "/admin/operations", "admin_operations"),
        (("GET",), "/admin/install", "admin_install_redirect"),
        (("GET",), "/admin/install/script", "admin_install_script"),
        (("POST",), "/admin/ssh/known-host/forget", "admin_forget_ssh_known_host"),
        (("POST",), "/admin/install/run", "admin_run_install"),
        (("POST",), "/admin/protocols/refresh", "admin_refresh_protocols"),
        (("POST",), "/admin/stats/refresh", "admin_refresh_stats"),
        (("POST",), "/admin/reset-server", "admin_reset_server"),
        (("GET",), "/sub/{token}", "subscription"),
        (("GET",), "/sing-box/{token}", "sing_box_subscription"),
        (("GET",), "/client/{token}", "client_page"),
        (("POST",), "/client/{token}/protocols/refresh", "client_refresh_protocols"),
        (("GET",), "/client/{token}/subscription.qr", "client_subscription_qr"),
        (("GET",), "/client/{token}/amnezia.qr", "client_amnezia_qr"),
        (("GET",), "/ip/{token}", "current_eu_ip"),
        (("GET",), "/amnezia/{token}", "amnezia_config"),
        (("GET",), "/amnezia-key/{token}", "amnezia_vpn_key"),
    }
    assert legacy_routes <= inventory


def test_root_setup_health_and_robots_wire_contract() -> None:
    init_db()
    client = TestClient(main.app, base_url="http://panel.local")

    root = client.get("/", follow_redirects=False)
    setup = client.get("/setup", follow_redirects=False)
    setup_page = client.get("/admin/setup")
    health = client.get("/healthz")
    robots = client.get("/robots.txt")

    assert (root.status_code, root.headers["location"], root.content) == (
        307,
        "/admin/setup",
        b"",
    )
    assert (setup.status_code, setup.headers["location"], setup.content) == (
        307,
        "/admin/setup",
        b"",
    )
    assert setup_page.status_code == 200
    assert setup_page.headers["content-type"].startswith("text/html;")
    assert health.content == b'{"status":"ok"}'
    assert robots.content == b"User-agent: *\nDisallow: /\n"


PUBLIC_TOKEN_PATHS = (
    "/sub/{token}",
    "/sing-box/{token}",
    "/client/{token}",
    "/client/{token}/subscription.qr",
    "/client/{token}/amnezia.qr",
    "/ip/{token}",
    "/amnezia/{token}",
    "/amnezia-key/{token}",
)


@pytest.mark.parametrize("path", PUBLIC_TOKEN_PATHS)
def test_public_routes_return_404_for_missing_or_disabled_client(
    legacy_client: TestClient,
    path: str,
) -> None:
    assert legacy_client.get(path.format(token="missing")).status_code == 404

    with get_db() as db:
        db.execute("UPDATE clients SET enabled = 0 WHERE token = ?", (TOKEN,))
    assert legacy_client.get(path.format(token=TOKEN)).status_code == 404


@pytest.mark.parametrize(
    ("path", "expected_status"),
    [
        ("/sub/{token}", 503),
        ("/sing-box/{token}", 503),
        ("/client/{token}", 503),
        ("/client/{token}/subscription.qr", 200),
        ("/client/{token}/amnezia.qr", 503),
        ("/ip/{token}", 503),
        ("/amnezia/{token}", 503),
        ("/amnezia-key/{token}", 503),
    ],
)
def test_public_route_status_without_current_ip(
    legacy_client: TestClient,
    path: str,
    expected_status: int,
) -> None:
    set_setting("current_ip", "")

    assert legacy_client.get(path.format(token=TOKEN)).status_code == expected_status


def test_subscription_wire_format_is_vless_then_hy2_with_trailing_newline(
    legacy_client: TestClient,
) -> None:
    response = legacy_client.get(f"/sub/{TOKEN}")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain;")
    assert response.content == (
        "vless://00000000-1111-2222-3333-444444444444@203.0.113.10:443"
        "?type=tcp&security=reality&pbk=reality-public-key&fp=firefox&sni=ok.ru"
        "&sid=0123456789abcdef&spx=%2F&flow=xtls-rprx-vision"
        "#%5BAutoVPN%5D%20Alice%20-%20vless\n"
        "hy2://shared-hysteria-password@203.0.113.10:8443/"
        "?insecure=1&sni=ok.ru&obfs=salamander&obfs-password=obfs-password"
        "#%5BAutoVPN%5D%20Alice%20-%20hysteria\n"
    ).encode()


def test_sing_box_wire_shape_and_tags(legacy_client: TestClient) -> None:
    response = legacy_client.get(f"/sing-box/{TOKEN}")

    assert response.status_code == 200
    assert response.json() == {
        "log": {"level": "info"},
        "dns": {"servers": [{"tag": "cloudflare", "address": "1.1.1.1"}]},
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": ["172.19.0.1/30"],
                "auto_route": True,
                "strict_route": True,
                "sniff": True,
            }
        ],
        "outbounds": [
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": ["vless-reality", "hysteria2"],
                "default": "vless-reality",
            },
            {
                "type": "vless",
                "tag": "vless-reality",
                "server": CURRENT_IP,
                "server_port": 443,
                "uuid": CLIENT_UUID,
                "flow": "xtls-rprx-vision",
                "tls": {
                    "enabled": True,
                    "server_name": "ok.ru",
                    "utls": {"enabled": True, "fingerprint": "firefox"},
                    "reality": {
                        "enabled": True,
                        "public_key": "reality-public-key",
                        "short_id": "0123456789abcdef",
                    },
                },
            },
            {
                "type": "hysteria2",
                "tag": "hysteria2",
                "server": CURRENT_IP,
                "server_port": 8443,
                "password": "shared-hysteria-password",
                "tls": {"enabled": True, "server_name": "ok.ru", "insecure": True},
                "obfs": {"type": "salamander", "password": "obfs-password"},
            },
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"auto_detect_interface": True, "final": "proxy"},
    }


def test_ip_and_amnezia_ini_wire_contract(legacy_client: TestClient) -> None:
    ip_response = legacy_client.get(f"/ip/{TOKEN}")
    config_response = legacy_client.get(f"/amnezia/{TOKEN}")

    assert ip_response.content == b"203.0.113.10\n"
    assert config_response.headers["content-disposition"] == (
        "attachment; filename=amnezia-7.conf"
    )
    assert config_response.content == (
        "[Interface]\n"
        "PrivateKey = client-private-key\n"
        "Address = 10.66.66.8/32\n"
        "DNS = 1.1.1.1, 8.8.8.8\n"
        "Jc = 4\nJmin = 30\nJmax = 900\nS1 = 64\nS2 = 128\n"
        "H1 = 1\nH2 = 2\nH3 = 3\nH4 = 4\n\n"
        "[Peer]\n"
        "PublicKey = server-public-key\n"
        "PresharedKey = client-preshared-key\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "Endpoint = 203.0.113.10:51820\n"
        "PersistentKeepalive = 25\n"
    ).encode()


def test_amnezia_key_packing_and_newline_contract(legacy_client: TestClient) -> None:
    response = legacy_client.get(f"/amnezia-key/{TOKEN}")

    assert response.status_code == 200
    assert response.content.startswith(b"vpn://")
    assert response.content.endswith(b"\n")
    encoded = response.content.removeprefix(b"vpn://").removesuffix(b"\n")
    packed = base64.urlsafe_b64decode(encoded + b"=" * ((4 - len(encoded) % 4) % 4))
    raw = zlib.decompress(packed[4:])
    payload = json.loads(raw)

    assert int.from_bytes(packed[:4], "big") == len(raw)
    assert payload["defaultContainer"] == "amnezia-awg2"
    assert payload["description"] == "[AutoVPN] Alice - amnezia"
    assert payload["hostName"] == CURRENT_IP
    assert payload["containers"][0]["container"] == "amnezia-awg2"
    assert payload["containers"][0]["awg"]["protocol_version"] == "2"
    assert payload["containers"][0]["awg"]["client_priv_key"] == "client-private-key"
    assert payload["containers"][0]["awg"]["config"].endswith("\n")


def test_qr_routes_keep_png_media_type(legacy_client: TestClient) -> None:
    subscription_qr = legacy_client.get(f"/client/{TOKEN}/subscription.qr")
    amnezia_qr = legacy_client.get(f"/client/{TOKEN}/amnezia.qr")

    for response in (subscription_qr, amnezia_qr):
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.parametrize("path", PUBLIC_TOKEN_PATHS)
def test_public_token_routes_disable_shared_caching(
    legacy_client: TestClient,
    path: str,
) -> None:
    response = legacy_client.get(path.format(token=TOKEN))

    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_public_token_error_also_disables_shared_caching(legacy_client: TestClient) -> None:
    response = legacy_client.get("/sub/missing-secret")

    assert response.status_code == 404
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_init_db_migrates_legacy_client_without_changing_token() -> None:
    with sqlite3.connect(settings.database_path) as db:
        db.execute(
            """
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                vless_uuid TEXT NOT NULL,
                hysteria_password TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO clients(
                id, name, token, enabled, vless_uuid, hysteria_password, created_at
            ) VALUES (7, 'Legacy', ?, 1, ?, 'legacy-hysteria', '2025-01-01T00:00:00+00:00')
            """,
            (TOKEN, CLIENT_UUID),
        )

    init_db()

    migrated = get_client_by_id(7)
    assert migrated is not None
    assert migrated["token"] == TOKEN
    assert migrated["amnezia_private_key"]
    assert migrated["amnezia_public_key"]
    assert migrated["amnezia_preshared_key"]
    assert migrated["amnezia_ipv4"] == "10.66.66.8"
