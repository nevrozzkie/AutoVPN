from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
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


def _snapshot_payload(
    connection: sqlite3.Connection,
) -> tuple[CapturedVpnConfig, str, str]:
    config = capture_vpn_config(connection)
    payload_json = canonical_vpn_config_json(config)
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    return config, payload_json, payload_sha256


def prepare_install_operation(target_host: str) -> PreparedInstall:
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        config, payload_json, payload_sha256 = _snapshot_payload(db)
        existing = db.execute(
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
            if existing["lifecycle"] != "APPLIED":
                db.execute(
                    """
                    UPDATE vpn_snapshots
                    SET lifecycle = 'PREPARED', failed_at = NULL, error_message = NULL
                    WHERE revision = ?
                    """,
                    (config.revision,),
                )
        else:
            db.execute(
                """
                INSERT INTO vpn_snapshots(
                    revision, payload_json, payload_sha256, lifecycle, prepared_at
                ) VALUES (?, ?, ?, 'PREPARED', ?)
                """,
                (config.revision, payload_json, payload_sha256, timestamp),
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


def get_vpn_snapshot(revision: int) -> dict[str, Any] | None:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM vpn_snapshots WHERE revision = ?", (revision,)
        ).fetchone()
