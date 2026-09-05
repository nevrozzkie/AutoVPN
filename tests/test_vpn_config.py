from __future__ import annotations

import base64
import json
import sqlite3
import zlib
from contextlib import contextmanager

import pytest

import app.db as database
from app.amnezia import (
    render_amnezia_client_config,
    render_amnezia_server_config,
    render_amnezia_vpn_key,
)
from app.config import settings
from app.db import create_client, get_db, init_db, set_setting
from app.eu_install import build_eu_install_script
from app.subscriptions import render_sing_box_subscription, render_subscription
from app.vpn_config import capture_vpn_config


def test_capture_uses_one_sqlite_read_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    init_db()
    client = create_client("before")
    set_setting("current_ip", "192.0.2.10")

    real_get_db = database.get_db

    class UpdatingConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection
            self.updated = False

        def execute(self, sql: str, parameters: tuple[object, ...] = ()):
            cursor = self.connection.execute(sql, parameters)
            if sql == "SELECT key, value FROM settings" and not self.updated:
                self.updated = True
                with real_get_db() as writer:
                    writer.execute(
                        "UPDATE settings SET value = '192.0.2.20' WHERE key = 'current_ip'"
                    )
                    writer.execute(
                        "UPDATE clients SET name = 'after' WHERE id = ?", (client["id"],)
                    )
            return cursor

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

    @contextmanager
    def intercepting_get_db():
        with real_get_db() as connection:
            yield UpdatingConnection(connection)

    monkeypatch.setattr(database, "get_db", intercepting_get_db)

    captured = capture_vpn_config()

    assert captured.current_ip == "192.0.2.10"
    assert captured.clients[0].name == "before"


def test_renderers_do_not_read_database_after_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    create_client("Alice")
    set_setting("current_ip", "203.0.113.10")
    captured = capture_vpn_config()
    client = captured.enabled_clients[0]

    def unexpected_database_access(*_: object, **__: object) -> None:
        raise AssertionError("renderer accessed live database")

    monkeypatch.setattr(database, "get_db", unexpected_database_access)

    assert "vless://" in render_subscription(captured, client.as_dict())
    assert render_sing_box_subscription(captured, client.as_dict())["route"]["final"] == "proxy"
    assert "[Interface]" in render_amnezia_client_config(captured, client)
    assert render_amnezia_vpn_key(captured, client).startswith("vpn://")
    script = build_eu_install_script(captured)
    assert client.vless_uuid in script
    assert client.amnezia_public_key in script


def test_amnezia_client_and_server_use_same_custom_network_snapshot() -> None:
    init_db()
    client = create_client("Alice")
    with get_db() as db:
        db.execute(
            "UPDATE clients SET amnezia_ipv4 = '10.77.88.2' WHERE id = ?",
            (client["id"],),
        )
    set_setting("config.amnezia_network_prefix", "10.77.88")
    set_setting("current_ip", "203.0.113.10")

    captured = capture_vpn_config()
    captured_client = captured.enabled_clients[0]
    client_config = render_amnezia_client_config(captured, captured_client)
    server_config = render_amnezia_server_config(captured)
    vpn_key = render_amnezia_vpn_key(captured, captured_client)

    assert "Address = 10.77.88.2/32" in client_config
    assert "Address = 10.77.88.1/24" in server_config
    assert "AllowedIPs = 10.77.88.2/32" in server_config
    assert "-s 10.77.88.0/24" in server_config
    encoded = vpn_key.removeprefix("vpn://")
    packed = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4))
    payload = json.loads(zlib.decompress(packed[4:]))
    assert payload["containers"][0]["awg"]["subnet_address"] == "10.77.88.0"


def test_new_client_address_uses_free_host_even_when_id_exceeds_253() -> None:
    init_db()
    created_at = "2026-01-01T00:00:00+00:00"
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO clients(
                id, name, token, enabled, vless_uuid, hysteria_password,
                amnezia_private_key, amnezia_public_key,
                amnezia_preshared_key, amnezia_ipv4, created_at
            ) VALUES (?, ?, ?, 1, ?, 'hy', 'priv', 'pub', 'psk', ?, ?)
            """,
            [
                (index, f"client-{index}", f"token-{index}", f"uuid-{index}", f"10.66.66.{index + 1}", created_at)
                for index in range(1, 254)
            ],
        )

    with pytest.raises(RuntimeError, match="address pool 10.66.66.0/24 is exhausted"):
        create_client("no-room")

    with get_db() as db:
        db.execute("DELETE FROM clients WHERE id = 42")
    replacement = create_client("replacement")

    assert replacement["id"] > 253
    assert replacement["amnezia_ipv4"] == "10.66.66.43"


def test_capture_merges_environment_defaults_deterministically() -> None:
    init_db()
    captured = capture_vpn_config()

    assert captured.vless.target == settings.vless_reality_target
    assert captured.vless.server_names == tuple(settings.vless_reality_server_names)
    assert captured.amnezia.network_prefix == settings.amnezia_network_prefix
