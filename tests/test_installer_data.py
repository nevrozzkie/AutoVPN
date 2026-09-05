from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

from tools.prepare_data import prepare_data_storage


def _create_legacy_database(path: Path, token: str) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE clients (token TEXT NOT NULL)")
    connection.execute("INSERT INTO clients(token) VALUES (?)", (token,))
    connection.commit()
    return connection


def _tokens(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        return [row[0] for row in connection.execute("SELECT token FROM clients")]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_legacy_wal_database_is_backed_up_verified_and_migrated(tmp_path: Path) -> None:
    legacy = tmp_path / "opt" / "autovpn" / "data" / "autovpn.sqlite3"
    database = tmp_path / "var" / "lib" / "autovpn" / "autovpn.sqlite3"
    backup_dir = database.parent / "backups"
    live_connection = _create_legacy_database(legacy, "legacy-token")
    try:
        backup = prepare_data_storage(
            database,
            backup_dir,
            legacy_database=legacy,
            timestamp="20260904T120000Z",
        )
    finally:
        live_connection.close()

    assert backup == backup_dir / "autovpn-pre-upgrade-20260904T120000Z.sqlite3"
    assert _tokens(database) == ["legacy-token"]
    assert _tokens(backup) == ["legacy-token"]
    assert _tokens(legacy) == ["legacy-token"]
    assert _mode(database.parent) == 0o700
    assert _mode(backup_dir) == 0o700
    assert _mode(database) == 0o600
    assert _mode(backup) == 0o600
    assert _mode(legacy) == 0o600


def test_reinstall_backs_up_current_database_without_replacing_it_from_legacy(
    tmp_path: Path,
) -> None:
    database = tmp_path / "data" / "autovpn.sqlite3"
    legacy = tmp_path / "legacy" / "autovpn.sqlite3"
    current_connection = _create_legacy_database(database, "current-token")
    legacy_connection = _create_legacy_database(legacy, "stale-token")
    current_connection.close()
    legacy_connection.close()

    backup = prepare_data_storage(
        database,
        database.parent / "backups",
        legacy_database=legacy,
        timestamp="20260904T120001Z",
    )

    assert _tokens(database) == ["current-token"]
    assert _tokens(backup) == ["current-token"]
    assert _tokens(legacy) == ["stale-token"]


def test_failed_backup_does_not_destroy_existing_database(tmp_path: Path) -> None:
    database = tmp_path / "data" / "autovpn.sqlite3"
    database.parent.mkdir(parents=True)
    original = b"not a sqlite database"
    database.write_bytes(original)

    with pytest.raises(sqlite3.DatabaseError):
        prepare_data_storage(
            database,
            database.parent / "backups",
            timestamp="20260904T120002Z",
        )

    assert database.read_bytes() == original
    assert list((database.parent / "backups").iterdir()) == []


def test_server_installer_guards_data_and_preserves_environment() -> None:
    script = Path("install.sh").read_text()

    assert 'DATA_DIR="${DATA_DIR:-/var/lib/autovpn}"' in script
    assert 'LEGACY_DATABASE="$APP_DIR/data/autovpn.sqlite3"' in script
    assert 'DATABASE_PATH="$DATA_DIR/autovpn.sqlite3"' in script
    assert '--exclude "data"' in script
    assert '--exclude ".env"' in script
    assert 'rm -rf "$APP_DIR"' not in script
    assert 'install -d -m 0700 "$DATA_DIR" "$BACKUP_DIR"' in script
    assert 'chmod 0600 "$DATABASE_PATH"' in script
    assert "preserve_existing_env" in script

    main = script[script.index("main() {") :]
    assert main.index("prepare_source") < main.index("prepare_persistent_data")
    assert main.index("prepare_persistent_data") < main.index("cleanup_existing_install")
    assert main.index("cleanup_existing_install") < main.index("deploy_source")


def test_local_installer_excludes_legacy_data_from_in_place_upgrade() -> None:
    script = Path("install-local.sh").read_text()

    assert 'LEGACY_DATABASE="$APP_DIR/data/autovpn.sqlite3"' in script
    assert '--exclude "data"' in script
    assert '--exclude ".env"' in script
    assert 'rm -rf "$APP_DIR"' not in script
    assert 'install -d -m 0700 "$DATA_DIR" "$BACKUP_DIR"' in script
    assert 'chmod 0600 "$DATABASE_PATH"' in script
    assert "preserve_existing_env" in script
    assert os.fspath(Path("tools/prepare_data.py")) in script
