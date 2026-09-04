from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote


MigrationAction = Callable[[sqlite3.Connection], None]


def _row_value(row: object, index: int, name: str) -> object:
    if isinstance(row, dict):
        return row[name]
    return row[index]  # type: ignore[index]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    signature: str
    apply: MigrationAction

    @property
    def checksum(self) -> str:
        value = f"{self.version}\0{self.name}\0{self.signature}".encode()
        return hashlib.sha256(value).hexdigest()


def _column_exists(connection: sqlite3.Connection, table: str, column: str) -> bool:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return any(_row_value(row, 1, "name") == column for row in rows)


def _add_column_if_missing(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    if not _column_exists(connection, table, column):
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _apply_legacy_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS clients (
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
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS ip_change_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            old_ip TEXT,
            new_ip TEXT,
            old_ip_id TEXT,
            new_ip_id TEXT,
            current_step TEXT NOT NULL,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            server_reachable_at TEXT,
            healthcheck_result TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS vpn_install_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL,
            target_host TEXT,
            current_step TEXT NOT NULL,
            output TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS client_stats (
            client_id INTEGER PRIMARY KEY,
            vless_uplink INTEGER NOT NULL DEFAULT 0,
            vless_downlink INTEGER NOT NULL DEFAULT 0,
            amnezia_rx INTEGER NOT NULL DEFAULT 0,
            amnezia_tx INTEGER NOT NULL DEFAULT 0,
            amnezia_latest_handshake INTEGER NOT NULL DEFAULT 0,
            last_seen_at TEXT,
            updated_at TEXT NOT NULL,
            raw TEXT,
            FOREIGN KEY(client_id) REFERENCES clients(id) ON DELETE CASCADE
        )
        """,
    )
    for statement in statements:
        connection.execute(statement)

    for column, declaration in {
        "amnezia_private_key": "TEXT",
        "amnezia_public_key": "TEXT",
        "amnezia_preshared_key": "TEXT",
        "amnezia_ipv4": "TEXT",
    }.items():
        _add_column_if_missing(connection, "clients", column, declaration)
    for column, declaration in {
        "amnezia_rx": "INTEGER NOT NULL DEFAULT 0",
        "amnezia_tx": "INTEGER NOT NULL DEFAULT 0",
        "amnezia_latest_handshake": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        _add_column_if_missing(connection, "client_stats", column, declaration)
    for column, declaration in {
        "server_reachable_at": "TEXT",
        "healthcheck_result": "TEXT",
    }.items():
        _add_column_if_missing(connection, "ip_change_operations", column, declaration)


MIGRATIONS = (
    Migration(
        version=1,
        name="legacy_schema",
        signature=(
            "create current legacy tables; add clients amnezia material; "
            "add client_stats amnezia counters; add ip operation health fields"
        ),
        apply=_apply_legacy_schema,
    ),
)


def _is_memory_database(database_path: str) -> bool:
    return database_path == ":memory:" or (
        database_path.startswith("file:") and "mode=memory" in database_path
    )


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def _verify_database(path: Path) -> None:
    with closing(_open_read_only(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise sqlite3.DatabaseError(f"SQLite integrity check failed for {path}: {result}")


def _backup_database(path: Path) -> Path:
    backup_dir = path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_dir, 0o700)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"autovpn-pre-migration-{timestamp}.sqlite3"
    temporary = backup.with_name(f".{backup.name}.tmp-{os.getpid()}")
    try:
        with closing(_open_read_only(path)) as source, closing(
            sqlite3.connect(temporary)
        ) as destination:
            source.backup(destination)
        _verify_database(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, backup)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return backup


def _has_user_schema(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    with closing(_open_read_only(path)) as connection:
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
    return row is not None


def _read_applied(connection: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    table_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if not table_exists:
        return {}
    return {
        int(_row_value(row, 0, "version")): (
            str(_row_value(row, 1, "name")),
            str(_row_value(row, 2, "checksum")),
        )
        for row in connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
    }


def _validate_applied(applied: dict[int, tuple[str, str]]) -> None:
    known = {migration.version: migration for migration in MIGRATIONS}
    for version, (name, checksum) in applied.items():
        migration = known.get(version)
        if migration is None:
            raise RuntimeError(f"Database migration {version} is newer than this application")
        if (name, checksum) != (migration.name, migration.checksum):
            raise RuntimeError(f"Database migration {version} checksum mismatch")


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = _read_applied(connection)
        _validate_applied(applied)
        for migration in MIGRATIONS:
            if migration.version in applied:
                continue
            migration.apply(connection)
            connection.execute(
                """
                INSERT INTO schema_migrations(version, name, checksum, applied_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )
        connection.commit()
    except BaseException:
        connection.rollback()
        raise


def migrate_database(database_path: str, connection: sqlite3.Connection) -> Path | None:
    applied = _read_applied(connection)
    _validate_applied(applied)
    pending = [migration for migration in MIGRATIONS if migration.version not in applied]
    backup = None
    if pending and not _is_memory_database(database_path):
        path = Path(database_path)
        if _has_user_schema(path):
            backup = _backup_database(path)
    if pending:
        apply_migrations(connection)
    return backup
