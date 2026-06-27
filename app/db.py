from __future__ import annotations

import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.config import settings
from app.amnezia import (
    client_amnezia_address,
    generate_obfuscation_settings,
    generate_preshared_key,
    generate_private_key,
    generate_public_key,
)
from app.reality import (
    generate_reality_private_key,
    generate_reality_public_key,
    generate_reality_short_id,
)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    db_path = Path(settings.database_path)
    if db_path.parent != Path("."):
        os.makedirs(db_path.parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                enabled INTEGER NOT NULL DEFAULT 1,
                vless_uuid TEXT NOT NULL,
                hysteria_password TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

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
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS vpn_install_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                status TEXT NOT NULL,
                target_host TEXT,
                current_step TEXT NOT NULL,
                output TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

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
            );
            """
        )
        _ensure_client_columns(db)
        _ensure_client_stats_columns(db)
        _ensure_ip_operation_columns(db)
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("current_ip", ""),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("last_healthcheck_status", "UNKNOWN"),
        )
        _ensure_amnezia_settings(db)
        _ensure_reality_settings(db)
        _ensure_client_amnezia_material(db)


def _column_exists(db: sqlite3.Connection, table: str, column: str) -> bool:
    rows = db.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _ensure_client_columns(db: sqlite3.Connection) -> None:
    columns = {
        "amnezia_private_key": "TEXT",
        "amnezia_public_key": "TEXT",
        "amnezia_preshared_key": "TEXT",
        "amnezia_ipv4": "TEXT",
    }
    for column, column_type in columns.items():
        if not _column_exists(db, "clients", column):
            db.execute(f"ALTER TABLE clients ADD COLUMN {column} {column_type}")


def _ensure_ip_operation_columns(db: sqlite3.Connection) -> None:
    columns = {
        "server_reachable_at": "TEXT",
        "healthcheck_result": "TEXT",
    }
    for column, column_type in columns.items():
        if not _column_exists(db, "ip_change_operations", column):
            db.execute(f"ALTER TABLE ip_change_operations ADD COLUMN {column} {column_type}")


def _ensure_client_stats_columns(db: sqlite3.Connection) -> None:
    columns = {
        "amnezia_rx": "INTEGER NOT NULL DEFAULT 0",
        "amnezia_tx": "INTEGER NOT NULL DEFAULT 0",
        "amnezia_latest_handshake": "INTEGER NOT NULL DEFAULT 0",
    }
    for column, column_type in columns.items():
        if not _column_exists(db, "client_stats", column):
            db.execute(f"ALTER TABLE client_stats ADD COLUMN {column} {column_type}")


def _setting(db: sqlite3.Connection, key: str) -> str:
    row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def _set_setting(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        """
        INSERT INTO settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _ensure_amnezia_settings(db: sqlite3.Connection) -> None:
    private_key = _setting(db, "amnezia.server_private_key")
    if not private_key:
        private_key = generate_private_key()
        _set_setting(db, "amnezia.server_private_key", private_key)
    if not _setting(db, "amnezia.server_public_key"):
        _set_setting(db, "amnezia.server_public_key", generate_public_key(private_key))

    obfuscation = generate_obfuscation_settings()
    for key, value in obfuscation.items():
        setting_key = f"amnezia.{key}"
        if not _setting(db, setting_key):
            _set_setting(db, setting_key, str(value))


def _ensure_reality_settings(db: sqlite3.Connection) -> None:
    private_key = _setting(db, "vless.reality_private_key")
    if not private_key:
        private_key = generate_reality_private_key()
        _set_setting(db, "vless.reality_private_key", private_key)
    if not _setting(db, "vless.reality_public_key"):
        _set_setting(db, "vless.reality_public_key", generate_reality_public_key(private_key))
    if not _setting(db, "vless.reality_short_id"):
        _set_setting(db, "vless.reality_short_id", generate_reality_short_id())


def _ensure_client_amnezia_material(db: sqlite3.Connection) -> None:
    clients = db.execute("SELECT * FROM clients ORDER BY id").fetchall()
    for client in clients:
        private_key = client.get("amnezia_private_key") or generate_private_key()
        public_key = client.get("amnezia_public_key") or generate_public_key(private_key)
        preshared_key = client.get("amnezia_preshared_key") or generate_preshared_key()
        address = client.get("amnezia_ipv4") or client_amnezia_address(int(client["id"]))
        db.execute(
            """
            UPDATE clients
            SET amnezia_private_key = ?,
                amnezia_public_key = ?,
                amnezia_preshared_key = ?,
                amnezia_ipv4 = ?
            WHERE id = ?
            """,
            (private_key, public_key, preshared_key, address, client["id"]),
        )


def get_setting(key: str, default: str = "") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as db:
        db.execute(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )


def list_clients() -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute("SELECT * FROM clients ORDER BY id DESC").fetchall()


def list_clients_with_stats() -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute(
            """
            SELECT
                clients.*,
                COALESCE(client_stats.vless_uplink, 0) AS vless_uplink,
                COALESCE(client_stats.vless_downlink, 0) AS vless_downlink,
                COALESCE(client_stats.amnezia_rx, 0) AS amnezia_rx,
                COALESCE(client_stats.amnezia_tx, 0) AS amnezia_tx,
                COALESCE(client_stats.amnezia_latest_handshake, 0) AS amnezia_latest_handshake,
                client_stats.last_seen_at AS stats_last_seen_at,
                client_stats.updated_at AS stats_updated_at
            FROM clients
            LEFT JOIN client_stats ON client_stats.client_id = clients.id
            ORDER BY clients.id DESC
            """
        ).fetchall()


def get_client_by_token(token: str) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute("SELECT * FROM clients WHERE token = ?", (token,)).fetchone()


def get_client_by_id(client_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def create_client(name: str) -> dict[str, Any]:
    created_at = now_iso()
    token = secrets.token_urlsafe(32)
    vless_uuid = str(uuid.uuid4())
    hysteria_password = secrets.token_urlsafe(24)
    amnezia_private_key = generate_private_key()
    amnezia_public_key = generate_public_key(amnezia_private_key)
    amnezia_preshared_key = generate_preshared_key()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO clients(
                name, token, enabled, vless_uuid, hysteria_password,
                amnezia_private_key, amnezia_public_key, amnezia_preshared_key,
                amnezia_ipv4, created_at
            )
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                token,
                vless_uuid,
                hysteria_password,
                amnezia_private_key,
                amnezia_public_key,
                amnezia_preshared_key,
                "",
                created_at,
            ),
        )
        client_id = int(cursor.lastrowid)
        db.execute(
            "UPDATE clients SET amnezia_ipv4 = ? WHERE id = ?",
            (client_amnezia_address(client_id), client_id),
        )
        return db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def set_client_enabled(client_id: int, enabled: bool) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE clients SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, client_id),
        )


def update_client_name(client_id: int, name: str) -> None:
    with get_db() as db:
        db.execute(
            "UPDATE clients SET name = ? WHERE id = ?",
            (name, client_id),
        )


def delete_client(client_id: int) -> None:
    with get_db() as db:
        db.execute("DELETE FROM clients WHERE id = ?", (client_id,))


def get_client_stats(client_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM client_stats WHERE client_id = ?",
            (client_id,),
        ).fetchone()


def upsert_client_stats(
    client_id: int,
    *,
    vless_uplink: int,
    vless_downlink: int,
    amnezia_rx: int,
    amnezia_tx: int,
    amnezia_latest_handshake: int,
    last_seen_at: str | None,
    raw: str,
) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            INSERT INTO client_stats(
                client_id,
                vless_uplink,
                vless_downlink,
                amnezia_rx,
                amnezia_tx,
                amnezia_latest_handshake,
                last_seen_at,
                updated_at,
                raw
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(client_id) DO UPDATE SET
                vless_uplink = excluded.vless_uplink,
                vless_downlink = excluded.vless_downlink,
                amnezia_rx = excluded.amnezia_rx,
                amnezia_tx = excluded.amnezia_tx,
                amnezia_latest_handshake = excluded.amnezia_latest_handshake,
                last_seen_at = COALESCE(excluded.last_seen_at, client_stats.last_seen_at),
                updated_at = excluded.updated_at,
                raw = excluded.raw
            """,
            (
                client_id,
                vless_uplink,
                vless_downlink,
                amnezia_rx,
                amnezia_tx,
                amnezia_latest_handshake,
                last_seen_at,
                timestamp,
                raw,
            ),
        )


def list_operations(limit: int = 100) -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM ip_change_operations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_latest_operation() -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM ip_change_operations ORDER BY id DESC LIMIT 1"
        ).fetchone()


def has_running_operation() -> bool:
    with get_db() as db:
        row = db.execute(
            """
            SELECT 1 FROM ip_change_operations
            WHERE status IN ('PENDING', 'RUNNING')
            LIMIT 1
            """
        ).fetchone()
        return row is not None


def create_operation() -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO ip_change_operations(status, current_step, created_at, updated_at)
            VALUES ('PENDING', 'queued', ?, ?)
            """,
            (timestamp, timestamp),
        )
        return int(cursor.lastrowid)


def update_operation(operation_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(operation_id)
    with get_db() as db:
        db.execute(
            f"UPDATE ip_change_operations SET {assignments} WHERE id = ?",
            values,
        )


def list_install_operations(limit: int = 100) -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM vpn_install_operations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_latest_install_operation() -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM vpn_install_operations ORDER BY id DESC LIMIT 1"
        ).fetchone()


def has_running_install_operation() -> bool:
    with get_db() as db:
        row = db.execute(
            """
            SELECT 1 FROM vpn_install_operations
            WHERE status IN ('PENDING', 'RUNNING')
            LIMIT 1
            """
        ).fetchone()
        return row is not None


def fail_incomplete_install_operations(reason: str) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute(
            """
            UPDATE vpn_install_operations
            SET status = 'FAILED',
                current_step = 'interrupted',
                error_message = ?,
                updated_at = ?
            WHERE status IN ('PENDING', 'RUNNING')
            """,
            (reason, timestamp),
        )


def create_install_operation(target_host: str) -> int:
    timestamp = now_iso()
    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO vpn_install_operations(status, target_host, current_step, created_at, updated_at)
            VALUES ('PENDING', ?, 'queued', ?, ?)
            """,
            (target_host, timestamp, timestamp),
        )
        return int(cursor.lastrowid)


def update_install_operation(operation_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(operation_id)
    with get_db() as db:
        db.execute(
            f"UPDATE vpn_install_operations SET {assignments} WHERE id = ?",
            values,
        )


def reset_server_and_aeza_state() -> None:
    with get_db() as db:
        db.execute(
            """
            DELETE FROM settings
            WHERE key IN (
                'current_ip',
                'last_healthcheck_status',
                'ssh.known_host_reset_last_output',
                'stats.last_refresh_at',
                'stats.last_error'
            )
            OR key LIKE 'config.eu_%'
            OR key LIKE 'config.aeza_%'
            OR key LIKE 'protocol.%'
            """
        )
        db.execute("DELETE FROM ip_change_operations")
        db.execute("DELETE FROM vpn_install_operations")
        db.execute("DELETE FROM client_stats")
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("current_ip", ""),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("last_healthcheck_status", "UNKNOWN"),
        )
