from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import app.migrations as migrations
from app.config import settings
from app.db import (
    DATABASE_BUSY_TIMEOUT_MS,
    create_client,
    get_client_by_token,
    get_db,
    init_db,
)


@pytest.fixture
def database_path(tmp_path: Path):  # type: ignore[no-untyped-def]
    original = settings.database_path
    path = tmp_path / "private" / "autovpn.sqlite3"
    object.__setattr__(settings, "database_path", str(path))
    try:
        yield path
    finally:
        object.__setattr__(settings, "database_path", original)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _create_current_legacy_database(path: Path) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    material = {
        "token": "legacy-token._~+/=keep-byte-for-byte",
        "vless_uuid": "11111111-2222-3333-4444-555555555555",
        "hysteria_password": "legacy-client-hysteria-secret",
        "amnezia_private_key": "legacy-awg-private=",
        "amnezia_public_key": "legacy-awg-public=",
        "amnezia_preshared_key": "legacy-awg-psk=",
        "hysteria_server_password": "legacy-server-hysteria-secret",
        "amnezia_server_private": "legacy-server-awg-private=",
    }
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                vless_uuid TEXT NOT NULL,
                hysteria_password TEXT NOT NULL,
                created_at TEXT NOT NULL,
                amnezia_private_key TEXT,
                amnezia_public_key TEXT,
                amnezia_preshared_key TEXT,
                amnezia_ipv4 TEXT
            );
            CREATE TABLE ip_change_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                old_ip TEXT,
                new_ip TEXT,
                old_ip_id TEXT,
                new_ip_id TEXT,
                current_step TEXT NOT NULL,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE client_stats (
                client_id INTEGER PRIMARY KEY,
                vless_uplink INTEGER NOT NULL DEFAULT 0,
                vless_downlink INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT,
                updated_at TEXT NOT NULL,
                raw TEXT,
                FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
            );
            """
        )
        connection.executemany(
            "INSERT INTO settings(key, value) VALUES (?, ?)",
            (
                ("hysteria.password", material["hysteria_server_password"]),
                ("hysteria.obfs_password", "legacy-obfs-secret"),
                ("amnezia.server_private_key", material["amnezia_server_private"]),
                ("amnezia.server_public_key", "legacy-server-awg-public="),
                ("vless.reality_private_key", "legacy-reality-private"),
                ("vless.reality_public_key", "legacy-reality-public"),
                ("vless.reality_short_id", "0011223344556677"),
                ("amnezia.jc", "4"),
                ("amnezia.jmin", "30"),
                ("amnezia.jmax", "900"),
                ("amnezia.s1", "64"),
                ("amnezia.s2", "128"),
                ("amnezia.h1", "1"),
                ("amnezia.h2", "2"),
                ("amnezia.h3", "3"),
                ("amnezia.h4", "4"),
            ),
        )
        connection.execute(
            """
            INSERT INTO clients(
                id, name, token, enabled, vless_uuid, hysteria_password, created_at,
                amnezia_private_key, amnezia_public_key,
                amnezia_preshared_key, amnezia_ipv4
            ) VALUES (9, 'Legacy', ?, 1, ?, ?, '2025-01-01T00:00:00+00:00', ?, ?, ?, '10.66.66.10')
            """,
            (
                material["token"],
                material["vless_uuid"],
                material["hysteria_password"],
                material["amnezia_private_key"],
                material["amnezia_public_key"],
                material["amnezia_preshared_key"],
            ),
        )
    return material


def test_fresh_database_has_current_schema_without_empty_backup(database_path: Path) -> None:
    init_db()

    with get_db() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        migration_rows = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations"
        ).fetchall()
        assert connection.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()["timeout"] == DATABASE_BUSY_TIMEOUT_MS
        assert connection.execute("PRAGMA journal_mode").fetchone()["journal_mode"] == "wal"

    assert tables == {
        "settings",
        "clients",
        "ip_change_operations",
        "vpn_install_operations",
        "client_stats",
        "schema_migrations",
        "vpn_state",
        "vpn_snapshots",
        "operation_leases",
        "server_operations",
        "router_credentials",
        "router_apply_results",
        "routers",
        "router_amnezia_peers",
    }
    assert [row["version"] for row in migration_rows] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
    assert [row["name"] for row in migration_rows] == [
        "legacy_schema",
        "desired_applied_snapshots",
        "shared_vps_operation_coordinator",
        "safe_aeza_ip_rotation",
        "router_credentials",
        "router_apply_results",
        "routers",
        "hysteria_per_client_auth",
        "router_amnezia_peers",
    ]
    assert [row["checksum"] for row in migration_rows] == [
        migration.checksum for migration in migrations.MIGRATIONS
    ]
    assert not (database_path.parent / "backups").exists()
    assert _mode(database_path.parent) == 0o700
    assert _mode(database_path) == 0o600


def test_router_migration_backfills_one_stable_router_per_legacy_credential(
    database_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    current_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations[:6])
    init_db()
    client = create_client("Legacy router client")
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO router_credentials(
                credential_id, client_id, label, secret_digest, scopes, enabled,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                "legacy-credential-a",
                client["id"],
                "Old primary",
                "a" * 64,
                "snapshot:read",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO router_credentials(
                credential_id, client_id, label, secret_digest, scopes, enabled,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                "legacy-credential-b",
                client["id"],
                "Old spare",
                "b" * 64,
                "apply:write",
                "2026-01-03T00:00:00+00:00",
                "2026-01-04T00:00:00+00:00",
            ),
        )
    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations)

    init_db()

    with get_db() as connection:
        credentials = connection.execute(
            """
            SELECT credential_id, router_id, client_id FROM router_credentials
            ORDER BY credential_id
            """
        ).fetchall()
        routers = connection.execute(
            """
            SELECT router_id, client_id, label, created_at, updated_at
            FROM routers ORDER BY label
            """
        ).fetchall()
    assert len(routers) == 2
    assert all(row["router_id"] for row in credentials)
    assert len({row["router_id"] for row in credentials}) == 2
    assert {row["client_id"] for row in routers} == {client["id"]}
    assert [row["label"] for row in routers] == ["Old primary", "Old spare"]
    assert [row["created_at"] for row in routers] == [
        "2026-01-01T00:00:00+00:00",
        "2026-01-03T00:00:00+00:00",
    ]


def test_legacy_upgrade_rotates_only_hysteria_auth_and_creates_verified_backup(
    database_path: Path,
) -> None:
    material = _create_current_legacy_database(database_path)

    init_db()

    client = get_client_by_token(material["token"])
    assert client is not None
    for key in (
        "token",
        "vless_uuid",
        "amnezia_private_key",
        "amnezia_public_key",
        "amnezia_preshared_key",
    ):
        assert client[key].encode() == material[key].encode()
    assert len(client["hysteria_password"]) >= 32
    assert client["hysteria_password"] not in (
        material["hysteria_password"], material["hysteria_server_password"],
    )

    with get_db() as connection:
        assert connection.execute(
            "SELECT value FROM settings WHERE key = 'hysteria.password'"
        ).fetchone()["value"].encode() == material["hysteria_server_password"].encode()
        assert connection.execute(
            "SELECT value FROM settings WHERE key = 'amnezia.server_private_key'"
        ).fetchone()["value"].encode() == material["amnezia_server_private"].encode()
        migrated_client = connection.execute(
            "SELECT deleted_at, deleted_revision FROM clients WHERE id = 9"
        ).fetchone()
        assert migrated_client == {"deleted_at": None, "deleted_revision": None}
        assert connection.execute(
            "SELECT desired_revision, applied_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone() == {"desired_revision": 1, "applied_revision": None}
        install_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(vpn_install_operations)")
        }
        assert "revision" in install_columns
        assert any(
            row["table"] == "vpn_snapshots" and row["from"] == "revision"
            for row in connection.execute("PRAGMA foreign_key_list(vpn_install_operations)")
        )
        ip_operation_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(ip_change_operations)")
        }
        assert {
            "revision",
            "published_revision",
            "action_state",
            "purchase_state",
            "make_main_state",
            "apply_state",
            "cleanup_warning",
            "rollback_outcome",
            "reconciled_at",
            "reconciliation_note",
        } <= ip_operation_columns
        assert any(
            row["table"] == "vpn_snapshots" and row["from"] == "revision"
            for row in connection.execute("PRAGMA foreign_key_list(ip_change_operations)")
        )

    backups = list((database_path.parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT token FROM clients WHERE id = 9").fetchone() == (
            material["token"],
        )
        assert backup.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone() is None
    assert _mode(backups[0]) == 0o600


def test_migration_is_applied_and_backed_up_exactly_once(database_path: Path) -> None:
    material = _create_current_legacy_database(database_path)
    init_db()
    client_before = get_client_by_token(material["token"])
    with get_db() as connection:
        applied = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()

    init_db()

    with get_db() as connection:
        rows = connection.execute(
            "SELECT version, applied_at FROM schema_migrations"
        ).fetchall()
    assert rows == applied
    assert get_client_by_token(material["token"]) == client_before
    assert len(list((database_path.parent / "backups").glob("*.sqlite3"))) == 1


def test_hysteria_upgrade_keeps_applied_snapshot_and_rotates_all_clients_once(
    database_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db import get_vpn_state, set_setting
    from app.vpn_config import (
        canonical_vpn_config_json, capture_vpn_config, captured_vpn_config_from_json,
    )

    current_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations[:7])
    init_db()
    clients = [create_client(name) for name in ("Active", "Disabled", "Deleted")]
    # Legacy initialization could expose the first client's secret to everyone.
    set_setting("hysteria.password", clients[0]["hysteria_password"])
    captured = capture_vpn_config()
    legacy_payload = json.loads(canonical_vpn_config_json(captured))
    legacy_payload["hysteria"].pop("auth_type")
    legacy_json = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
    legacy_hash = hashlib.sha256(legacy_json.encode()).hexdigest()
    with get_db() as db:
        db.execute("UPDATE clients SET enabled = 0 WHERE id = ?", (clients[1]["id"],))
        db.execute(
            "UPDATE clients SET deleted_at = '2026-01-01' WHERE id = ?",
            (clients[2]["id"],),
        )
        db.execute(
            """
            INSERT INTO vpn_snapshots(
                revision, payload_json, payload_sha256, lifecycle, prepared_at, applied_at
            ) VALUES (?, ?, ?, 'APPLIED', '2026-01-01', '2026-01-01')
            """,
            (captured.revision, legacy_json, legacy_hash),
        )
        db.execute(
            "UPDATE vpn_state SET applied_revision = ? WHERE singleton = 1",
            (captured.revision,),
        )
        before = db.execute("SELECT * FROM clients ORDER BY id").fetchall()

    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations)
    init_db()
    with get_db() as db:
        after = db.execute("SELECT * FROM clients ORDER BY id").fetchall()
        stored = db.execute("SELECT * FROM vpn_snapshots").fetchone()
    assert stored["payload_json"] == legacy_json
    assert stored["payload_sha256"] == legacy_hash
    assert stored["lifecycle"] == "APPLIED"
    assert get_vpn_state()["applied_revision"] == captured.revision
    assert get_vpn_state()["desired_revision"] == captured.revision + 1
    assert captured_vpn_config_from_json(stored["payload_json"]).hysteria.auth_type == "password"
    assert capture_vpn_config().hysteria.auth_type == "userpass"
    assert len({row["hysteria_password"] for row in after}) == len(clients)
    old_secrets = {row["hysteria_password"] for row in before}
    for old, new in zip(before, after):
        assert len(new["hysteria_password"]) >= 32
        assert new["hysteria_password"] not in old_secrets
        assert {k: v for k, v in new.items() if k != "hysteria_password"} == {
            k: v for k, v in old.items() if k != "hysteria_password"
        }

    init_db()
    with get_db() as db:
        assert db.execute("SELECT * FROM clients ORDER BY id").fetchall() == after
    assert get_vpn_state()["desired_revision"] == captured.revision + 1


def test_failed_migration_rolls_back_schema_and_version_marker(
    database_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()

    def fail_after_ddl(connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE must_be_rolled_back (value TEXT)")
        raise RuntimeError("simulated interrupted migration")

    failed_version = max(item.version for item in migrations.MIGRATIONS) + 1
    failed = migrations.Migration(
        version=failed_version,
        name="failure_probe",
        signature="create table then fail",
        apply=fail_after_ddl,
    )
    monkeypatch.setattr(migrations, "MIGRATIONS", (*migrations.MIGRATIONS, failed))

    with pytest.raises(RuntimeError, match="simulated interrupted migration"):
        init_db()

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT 1 FROM schema_migrations WHERE version = ?", (failed_version,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'must_be_rolled_back'"
        ).fetchone() is None


def test_memory_database_does_not_attempt_chmod(monkeypatch: pytest.MonkeyPatch) -> None:
    original = settings.database_path
    object.__setattr__(settings, "database_path", ":memory:")
    monkeypatch.setattr(os, "chmod", lambda *_: pytest.fail("chmod called for :memory:"))
    try:
        with get_db() as connection:
            assert connection.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
            assert connection.execute("PRAGMA busy_timeout").fetchone()["timeout"] == (
                DATABASE_BUSY_TIMEOUT_MS
            )
    finally:
        object.__setattr__(settings, "database_path", original)
