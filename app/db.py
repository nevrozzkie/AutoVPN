from __future__ import annotations

import ipaddress
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from app.amnezia import (
    generate_obfuscation_settings,
    generate_preshared_key,
    generate_private_key,
    generate_public_key,
)
from app.config import settings
from app.migrations import migrate_database
from app.operation_coordinator import (
    TERMINAL_STATUSES,
    acquire_vps_lease,
    heartbeat_vps_lease,
    recover_incomplete_vps_operations,
    release_vps_lease,
)
from app.reality import (
    generate_reality_private_key,
    generate_reality_public_key,
    generate_reality_short_id,
)


DATABASE_BUSY_TIMEOUT_MS = 5_000


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}


def _is_memory_database(database_path: str) -> bool:
    return database_path == ":memory:" or (
        database_path.startswith("file:") and "mode=memory" in database_path
    )


def _secure_database_files(database_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{database_path}{suffix}")
        if path.exists():
            os.chmod(path, 0o600)


@contextmanager
def get_db(*, enable_wal: bool = True) -> Iterator[sqlite3.Connection]:
    database_path = settings.database_path
    is_memory = _is_memory_database(database_path)
    if not is_memory:
        db_path = Path(database_path)
        if db_path.parent != Path("."):
            os.makedirs(db_path.parent, mode=0o700, exist_ok=True)
            os.chmod(db_path.parent, 0o700)
    conn = sqlite3.connect(
        database_path,
        timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
        uri=database_path.startswith("file:"),
    )
    conn.row_factory = dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
    if not is_memory:
        _secure_database_files(database_path)
        if enable_wal:
            conn.execute("PRAGMA journal_mode = WAL")
            _secure_database_files(database_path)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    database_path = settings.database_path
    with get_db(enable_wal=False) as db:
        migrate_database(settings.database_path, db)
        if not _is_memory_database(database_path):
            db.execute("PRAGMA journal_mode = WAL")
            _secure_database_files(database_path)
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("current_ip", ""),
        )
        db.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            ("last_healthcheck_status", "UNKNOWN"),
        )
        vpn_changed = any(
            (
                _ensure_amnezia_settings(db),
                _ensure_reality_settings(db),
                _ensure_hysteria_settings(db),
                _ensure_client_amnezia_material(db),
            )
        )
        if vpn_changed:
            _mark_vpn_config_updated(db)


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


def _mark_vpn_config_updated(db: sqlite3.Connection) -> int:
    timestamp = now_iso()
    _set_setting(db, "vpn.config_updated_at", timestamp)
    db.execute(
        """
        UPDATE vpn_state
        SET desired_revision = desired_revision + 1,
            desired_updated_at = ?
        WHERE singleton = 1
        """,
        (timestamp,),
    )
    return int(
        db.execute(
            "SELECT desired_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone()["desired_revision"]
    )


def _is_vpn_setting(key: str) -> bool:
    return key == "current_ip" or key.startswith(
        (
            "vless.",
            "hysteria.",
            "amnezia.",
            "config.vless_",
            "config.hysteria_",
            "config.amnezia_",
        )
    )


def _ensure_amnezia_settings(db: sqlite3.Connection) -> bool:
    changed = False
    private_key = _setting(db, "amnezia.server_private_key")
    if not private_key:
        private_key = generate_private_key()
        _set_setting(db, "amnezia.server_private_key", private_key)
        changed = True
    if not _setting(db, "amnezia.server_public_key"):
        _set_setting(db, "amnezia.server_public_key", generate_public_key(private_key))
        changed = True

    obfuscation = generate_obfuscation_settings()
    for key, value in obfuscation.items():
        setting_key = f"amnezia.{key}"
        if not _setting(db, setting_key):
            _set_setting(db, setting_key, str(value))
            changed = True
    return changed


def _ensure_reality_settings(db: sqlite3.Connection) -> bool:
    changed = False
    private_key = _setting(db, "vless.reality_private_key")
    if not private_key:
        private_key = generate_reality_private_key()
        _set_setting(db, "vless.reality_private_key", private_key)
        changed = True
    if not _setting(db, "vless.reality_public_key"):
        _set_setting(db, "vless.reality_public_key", generate_reality_public_key(private_key))
        changed = True
    if not _setting(db, "vless.reality_short_id"):
        _set_setting(db, "vless.reality_short_id", generate_reality_short_id())
        changed = True
    return changed


def _ensure_hysteria_settings(db: sqlite3.Connection) -> bool:
    changed = False
    if not _setting(db, "hysteria.password"):
        client = db.execute("SELECT hysteria_password FROM clients ORDER BY id LIMIT 1").fetchone()
        password = client["hysteria_password"] if client else secrets.token_urlsafe(24)
        _set_setting(db, "hysteria.password", password)
        changed = True
    # Hysteria transport password. Must be identical on server and client;
    # both configs are generated from this value.
    if not _setting(db, "hysteria.obfs_password"):
        _set_setting(db, "hysteria.obfs_password", secrets.token_urlsafe(16))
        changed = True
    return changed


def _ensure_client_amnezia_material(db: sqlite3.Connection) -> bool:
    changed = False
    clients = db.execute("SELECT * FROM clients ORDER BY id").fetchall()
    for client in clients:
        private_key = client.get("amnezia_private_key") or generate_private_key()
        public_key = client.get("amnezia_public_key") or generate_public_key(private_key)
        preshared_key = client.get("amnezia_preshared_key") or generate_preshared_key()
        address = client.get("amnezia_ipv4") or _legacy_or_free_amnezia_address(
            db, int(client["id"])
        )
        if not all(
            (
                client.get("amnezia_private_key"),
                client.get("amnezia_public_key"),
                client.get("amnezia_preshared_key"),
                client.get("amnezia_ipv4"),
            )
        ):
            changed = True
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
    return changed


def _amnezia_network(db: sqlite3.Connection) -> ipaddress.IPv4Network:
    prefix = _setting(db, "config.amnezia_network_prefix") or settings.amnezia_network_prefix
    try:
        network = ipaddress.ip_network(f"{prefix}.0/24", strict=True)
    except ValueError as exc:
        raise ValueError(f"Invalid AmneziaWG /24 network prefix: {prefix}") from exc
    if not isinstance(network, ipaddress.IPv4Network):
        raise ValueError(f"AmneziaWG network must be IPv4: {prefix}")
    return network


def _allocate_amnezia_address(db: sqlite3.Connection) -> str:
    network = _amnezia_network(db)
    used = {
        str(row["amnezia_ipv4"])
        for row in db.execute(
            "SELECT amnezia_ipv4 FROM clients WHERE amnezia_ipv4 IS NOT NULL AND amnezia_ipv4 != ''"
        )
    }
    # The first usable host is the server. Every row reserves its address,
    # including disabled clients and (after migration v2) soft-deleted clients.
    for address in list(network.hosts())[1:]:
        candidate = str(address)
        if candidate not in used:
            return candidate
    raise RuntimeError(f"AmneziaWG address pool {network} is exhausted")


def _legacy_or_free_amnezia_address(
    db: sqlite3.Connection, client_id: int
) -> str:
    network = _amnezia_network(db)
    preferred_octet = client_id + 1
    if 2 <= preferred_octet <= 254:
        preferred = str(network.network_address + preferred_octet)
        in_use = db.execute(
            "SELECT 1 FROM clients WHERE amnezia_ipv4 = ? LIMIT 1", (preferred,)
        ).fetchone()
        if not in_use:
            return preferred
    return _allocate_amnezia_address(db)


def get_setting(key: str, default: str = "") -> str:
    with get_db() as db:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = _setting(db, key)
        _set_setting(db, key, value)
        if _is_vpn_setting(key) and previous != value:
            _mark_vpn_config_updated(db)


def set_settings(values: dict[str, str], *, mark_vpn_config_updated: bool = False) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        vpn_changed = mark_vpn_config_updated and any(
            _is_vpn_setting(key) and _setting(db, key) != value
            for key, value in values.items()
        )
        db.executemany(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            values.items(),
        )
        if vpn_changed:
            _mark_vpn_config_updated(db)


def mark_vpn_config_updated() -> None:
    with get_db() as db:
        _mark_vpn_config_updated(db)


def list_clients() -> list[dict[str, Any]]:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM clients WHERE deleted_at IS NULL ORDER BY id DESC"
        ).fetchall()


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
            WHERE clients.deleted_at IS NULL
            ORDER BY clients.id DESC
            """
        ).fetchall()


def get_client_by_token(token: str) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM clients WHERE token = ? AND deleted_at IS NULL", (token,)
        ).fetchone()


def get_client_by_id(client_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)
        ).fetchone()


def create_client(name: str) -> dict[str, Any]:
    created_at = now_iso()
    token = secrets.token_urlsafe(32)
    vless_uuid = str(uuid.uuid4())
    hysteria_password = secrets.token_urlsafe(24)
    amnezia_private_key = generate_private_key()
    amnezia_public_key = generate_public_key(amnezia_private_key)
    amnezia_preshared_key = generate_preshared_key()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        amnezia_ipv4 = _allocate_amnezia_address(db)
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
                amnezia_ipv4,
                created_at,
            ),
        )
        client_id = int(cursor.lastrowid)
        _mark_vpn_config_updated(db)
        return db.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def set_client_enabled(client_id: int, enabled: bool) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        client = db.execute(
            "SELECT enabled FROM clients WHERE id = ? AND deleted_at IS NULL",
            (client_id,),
        ).fetchone()
        desired_value = 1 if enabled else 0
        if client is None or int(client["enabled"]) == desired_value:
            return
        db.execute(
            "UPDATE clients SET enabled = ? WHERE id = ? AND deleted_at IS NULL",
            (desired_value, client_id),
        )
        _mark_vpn_config_updated(db)


def update_client_name(client_id: int, name: str) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        client = db.execute(
            "SELECT name FROM clients WHERE id = ? AND deleted_at IS NULL",
            (client_id,),
        ).fetchone()
        if client is None or client["name"] == name:
            return
        db.execute(
            "UPDATE clients SET name = ? WHERE id = ? AND deleted_at IS NULL",
            (name, client_id),
        )
        _mark_vpn_config_updated(db)


def delete_client(client_id: int) -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute(
            "SELECT 1 FROM clients WHERE id = ? AND deleted_at IS NULL", (client_id,)
        ).fetchone()
        if not exists:
            return
        revision = _mark_vpn_config_updated(db)
        db.execute(
            """
            UPDATE clients
            SET deleted_at = ?, deleted_revision = ?
            WHERE id = ? AND deleted_at IS NULL
            """,
            (now_iso(), revision, client_id),
        )


def purge_applied_deleted_clients(db: sqlite3.Connection) -> int:
    applied_value = db.execute(
        "SELECT applied_revision FROM vpn_state WHERE singleton = 1"
    ).fetchone()["applied_revision"]
    if applied_value is None:
        return 0
    applied_revision = int(applied_value)
    cursor = db.execute(
        """
        DELETE FROM clients
        WHERE deleted_revision IS NOT NULL AND deleted_revision <= ?
        """,
        (applied_revision,),
    )
    return cursor.rowcount


def get_vpn_state() -> dict[str, Any]:
    with get_db() as db:
        return db.execute("SELECT * FROM vpn_state WHERE singleton = 1").fetchone()


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
        db.execute("BEGIN IMMEDIATE")
        cursor = db.execute(
            """
            INSERT INTO ip_change_operations(status, current_step, created_at, updated_at)
            VALUES ('PENDING', 'queued', ?, ?)
            """,
            (timestamp, timestamp),
        )
        operation_id = int(cursor.lastrowid)
        acquire_vps_lease(db, "IP_CHANGE", operation_id)
        return operation_id


def update_operation(operation_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(operation_id)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            f"UPDATE ip_change_operations SET {assignments} WHERE id = ?",
            values,
        )
        if fields.get("status") in TERMINAL_STATUSES:
            release_vps_lease(db, "IP_CHANGE", operation_id)
        else:
            heartbeat_vps_lease(db, "IP_CHANGE", operation_id)


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
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        recover_incomplete_vps_operations(db, reason)


def create_install_operation(target_host: str) -> int:
    # Compatibility wrapper for callers outside the HTTP handler. Importing
    # lazily avoids a module cycle while preserving the atomic prepare path.
    from app.vpn_state import prepare_install_operation

    return prepare_install_operation(target_host).operation_id


def update_install_operation(operation_id: int, **fields: Any) -> None:
    if not fields:
        return
    fields["updated_at"] = now_iso()
    assignments = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values())
    values.append(operation_id)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            f"UPDATE vpn_install_operations SET {assignments} WHERE id = ?",
            values,
        )
        if fields.get("status") in TERMINAL_STATUSES:
            release_vps_lease(db, "INSTALL", operation_id)
        else:
            heartbeat_vps_lease(db, "INSTALL", operation_id)


def reset_server_and_aeza_state() -> None:
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        vpn_changed = db.execute(
            """
            SELECT 1 FROM settings
            WHERE (key = 'current_ip' AND value != '')
               OR key LIKE 'config.vless_%'
               OR key LIKE 'config.hysteria_%'
               OR key LIKE 'config.amnezia_%'
            LIMIT 1
            """
        ).fetchone() is not None
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
        if vpn_changed:
            _mark_vpn_config_updated(db)
