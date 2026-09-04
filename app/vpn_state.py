from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, replace
from typing import Any

from app.db import get_db, now_iso, purge_applied_deleted_clients
from app.operation_coordinator import acquire_vps_lease, release_vps_lease
from app.vpn_config import (
    CapturedVpnConfig,
    canonical_vpn_config_json,
    capture_vpn_config,
    captured_vpn_config_from_json,
)


@dataclass(frozen=True)
class PreparedInstall:
    operation_id: int
    revision: int
    payload_sha256: str


@dataclass(frozen=True)
class PreparedIpChange:
    operation_id: int
    revision: int
    payload_sha256: str


class IpChangeSafetyHoldError(RuntimeError):
    """A previous external IP action needs manual reconciliation."""


def _snapshot_payload(
    connection: sqlite3.Connection,
) -> tuple[CapturedVpnConfig, str, str]:
    config = capture_vpn_config(connection)
    payload_json = canonical_vpn_config_json(config)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return config, payload_json, payload_sha256


def _ensure_snapshot(
    connection: sqlite3.Connection,
    config: CapturedVpnConfig,
    payload_json: str,
    payload_sha256: str,
    timestamp: str,
    *,
    reprepare_failed: bool,
) -> None:
    existing = connection.execute(
        "SELECT * FROM vpn_snapshots WHERE revision = ?", (config.revision,)
    ).fetchone()
    if existing:
        if (
            existing["payload_json"] != payload_json
            or existing["payload_sha256"] != payload_sha256
        ):
            raise RuntimeError(
                f"VPN revision {config.revision} already has different immutable content"
            )
        if reprepare_failed and existing["lifecycle"] != "APPLIED":
            connection.execute(
                """
                UPDATE vpn_snapshots
                SET lifecycle = 'PREPARED', failed_at = NULL, error_message = NULL
                WHERE revision = ?
                """,
                (config.revision,),
            )
        return
    connection.execute(
        """
        INSERT INTO vpn_snapshots(
            revision, payload_json, payload_sha256, lifecycle, prepared_at
        ) VALUES (?, ?, ?, 'PREPARED', ?)
        """,
        (config.revision, payload_json, payload_sha256, timestamp),
    )


def prepare_install_operation(target_host: str) -> PreparedInstall:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        config, payload_json, payload_sha256 = _snapshot_payload(db)
        _ensure_snapshot(
            db,
            config,
            payload_json,
            payload_sha256,
            timestamp,
            reprepare_failed=True,
        )
        cursor = db.execute(
            """
            INSERT INTO vpn_install_operations(
                status, target_host, current_step, revision, created_at, updated_at
            ) VALUES ('PENDING', ?, 'queued', ?, ?, ?)
            """,
            (target_host, config.revision, timestamp, timestamp),
        )
        operation_id = int(cursor.lastrowid)
        acquire_vps_lease(db, "INSTALL", operation_id)
        return PreparedInstall(
            operation_id=operation_id,
            revision=config.revision,
            payload_sha256=payload_sha256,
        )


def prepare_ip_change_operation() -> PreparedIpChange:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        unresolved = db.execute(
            """
            SELECT id FROM ip_change_operations
            WHERE (status = 'AMBIGUOUS' AND action_state != 'RECONCILED')
               OR action_state IN (
                    'AMBIGUOUS',
                    'CLEANUP_AMBIGUOUS',
                    'PUBLISHED_AWAITING_AWG_CHECK'
               )
            ORDER BY id DESC LIMIT 1
            """
        ).fetchone()
        if unresolved:
            raise IpChangeSafetyHoldError(
                "A previous Aeza IP operation requires manual verification "
                f"(operation {unresolved['id']})"
            )
        config, payload_json, payload_sha256 = _snapshot_payload(db)
        _ensure_snapshot(
            db,
            config,
            payload_json,
            payload_sha256,
            timestamp,
            reprepare_failed=False,
        )
        cursor = db.execute(
            """
            INSERT INTO ip_change_operations(
                status, current_step, revision, action_state, purchase_state,
                make_main_state, apply_state, created_at, updated_at
            ) VALUES (
                'PENDING', 'queued', ?, 'NOT_STARTED', 'NOT_STARTED',
                'NOT_STARTED', 'NOT_STARTED', ?, ?
            )
            """,
            (config.revision, timestamp, timestamp),
        )
        operation_id = int(cursor.lastrowid)
        acquire_vps_lease(db, "IP_CHANGE", operation_id)
        return PreparedIpChange(
            operation_id=operation_id,
            revision=config.revision,
            payload_sha256=payload_sha256,
        )


def load_ip_change_snapshot(
    operation_id: int,
) -> tuple[dict[str, Any], CapturedVpnConfig]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT operation.*, snapshot.payload_json
            FROM ip_change_operations AS operation
            JOIN vpn_snapshots AS snapshot ON snapshot.revision = operation.revision
            WHERE operation.id = ?
            """,
            (operation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(
            f"IP change operation {operation_id} has no bound VPN snapshot"
        )
    return row, captured_vpn_config_from_json(row["payload_json"])


def publish_ip_change(
    operation_id: int,
    new_ip: str,
    healthcheck_result: str,
) -> int:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            """
            SELECT operation.revision, snapshot.payload_json
            FROM ip_change_operations AS operation
            JOIN vpn_snapshots AS snapshot ON snapshot.revision = operation.revision
            WHERE operation.id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None or row["revision"] is None:
            raise RuntimeError(
                f"IP change operation {operation_id} has no bound VPN snapshot"
            )
        bound_revision = int(row["revision"])
        state = db.execute(
            "SELECT desired_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone()
        if int(state["desired_revision"]) != bound_revision:
            raise RuntimeError(
                "VPN configuration changed during IP rotation; refusing to publish"
            )

        published_revision = bound_revision + 1
        bound_config = captured_vpn_config_from_json(row["payload_json"])
        published_config = replace(
            bound_config,
            current_ip=new_ip,
            config_updated_at=timestamp,
            revision=published_revision,
        )
        payload_json = canonical_vpn_config_json(published_config)
        payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        db.execute(
            """
            INSERT INTO vpn_snapshots(
                revision, payload_json, payload_sha256, lifecycle,
                prepared_at, applied_at
            ) VALUES (?, ?, ?, 'APPLIED', ?, ?)
            """,
            (
                published_revision,
                payload_json,
                payload_sha256,
                timestamp,
                timestamp,
            ),
        )
        db.execute(
            """
            UPDATE vpn_snapshots
            SET lifecycle = 'APPLIED', applied_at = COALESCE(applied_at, ?),
                failed_at = NULL, error_message = NULL
            WHERE revision = ?
            """,
            (timestamp, bound_revision),
        )
        db.executemany(
            """
            INSERT INTO settings(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (
                ("current_ip", new_ip),
                ("vpn.config_updated_at", timestamp),
                ("last_healthcheck_status", "OK"),
            ),
        )
        db.execute(
            """
            UPDATE vpn_state
            SET desired_revision = ?, applied_revision = ?,
                desired_updated_at = ?, applied_updated_at = ?
            WHERE singleton = 1
            """,
            (published_revision, published_revision, timestamp, timestamp),
        )
        purge_applied_deleted_clients(db)
        db.execute(
            """
            UPDATE ip_change_operations
            SET status = 'RUNNING', current_step = 'delete_old_ipv4',
                action_state = 'PUBLISHED', published_revision = ?,
                published_at = ?, healthcheck_result = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                published_revision,
                timestamp,
                healthcheck_result,
                timestamp,
                operation_id,
            ),
        )
        return published_revision


def load_install_snapshot(
    operation_id: int,
) -> tuple[dict[str, Any], CapturedVpnConfig]:
    with get_db() as db:
        row = db.execute(
            """
            SELECT operation.*, snapshot.payload_json
            FROM vpn_install_operations AS operation
            JOIN vpn_snapshots AS snapshot ON snapshot.revision = operation.revision
            WHERE operation.id = ?
            """,
            (operation_id,),
        ).fetchone()
    if row is None:
        raise RuntimeError(f"Install operation {operation_id} has no prepared VPN snapshot")
    return row, captured_vpn_config_from_json(row["payload_json"])


def complete_install_operation(operation_id: int, output: str) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        operation = db.execute(
            "SELECT revision FROM vpn_install_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if not operation or operation["revision"] is None:
            raise RuntimeError(f"Install operation {operation_id} has no revision")
        revision = int(operation["revision"])
        updated = db.execute(
            """
            UPDATE vpn_snapshots
            SET lifecycle = 'APPLIED', applied_at = ?, failed_at = NULL,
                error_message = NULL
            WHERE revision = ?
            """,
            (timestamp, revision),
        )
        if updated.rowcount != 1:
            raise RuntimeError(f"VPN snapshot {revision} does not exist")
        db.execute(
            """
            UPDATE vpn_state
            SET applied_revision = CASE
                    WHEN applied_revision IS NULL OR applied_revision < ? THEN ?
                    ELSE applied_revision
                END,
                applied_updated_at = ?
            WHERE singleton = 1
            """,
            (revision, revision, timestamp),
        )
        purge_applied_deleted_clients(db)
        db.execute(
            """
            UPDATE vpn_install_operations
            SET status = 'DONE', current_step = 'done', output = ?,
                error_message = NULL, updated_at = ?
            WHERE id = ?
            """,
            (output, timestamp, operation_id),
        )
        release_vps_lease(db, "INSTALL", operation_id)


def fail_install_operation(operation_id: int, error_message: str) -> None:
    timestamp = now_iso()
    with get_db() as db:
        operation = db.execute(
            "SELECT revision FROM vpn_install_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if operation and operation["revision"] is not None:
            db.execute(
                """
                UPDATE vpn_snapshots
                SET lifecycle = 'FAILED', failed_at = ?, error_message = ?
                WHERE revision = ? AND lifecycle != 'APPLIED'
                """,
                (timestamp, error_message, operation["revision"]),
            )
        db.execute(
            """
            UPDATE vpn_install_operations
            SET status = 'FAILED', current_step = 'failed', error_message = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (error_message, timestamp, operation_id),
        )
        release_vps_lease(db, "INSTALL", operation_id)


def mark_install_operation_ambiguous(operation_id: int, error_message: str) -> None:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        operation = db.execute(
            "SELECT revision FROM vpn_install_operations WHERE id = ?", (operation_id,)
        ).fetchone()
        if operation and operation["revision"] is not None:
            db.execute(
                """
                UPDATE vpn_snapshots
                SET lifecycle = 'FAILED', failed_at = ?, error_message = ?
                WHERE revision = ? AND lifecycle != 'APPLIED'
                """,
                (timestamp, error_message, operation["revision"]),
            )
        db.execute(
            """
            UPDATE vpn_install_operations
            SET status = 'AMBIGUOUS', current_step = 'ambiguous', error_message = ?,
                updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """,
            (error_message, timestamp, operation_id),
        )
        release_vps_lease(db, "INSTALL", operation_id)


def get_vpn_snapshot(revision: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM vpn_snapshots WHERE revision = ?", (revision,)
        ).fetchone()
