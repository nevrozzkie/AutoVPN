from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


VPN_VPS_RESOURCE = "vpn_vps"
DEFAULT_LEASE_SECONDS = 60 * 60
TERMINAL_STATUSES = {"DONE", "FAILED", "TIMED_OUT", "AMBIGUOUS"}


@dataclass(frozen=True)
class LeaseOwner:
    owner_type: str
    owner_id: int


class OperationBusyError(RuntimeError):
    def __init__(self, owner: LeaseOwner) -> None:
        self.owner = owner
        super().__init__(
            f"VPN VPS is busy with {owner.owner_type} operation {owner.owner_id}"
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _bounded_reason(reason: str) -> str:
    return reason.strip()[:1000] or "Interrupted operation recovered"


def _fail_owner(
    connection: sqlite3.Connection,
    owner: LeaseOwner,
    reason: str,
    timestamp: str,
) -> None:
    if owner.owner_type == "INSTALL":
        operation = connection.execute(
            "SELECT revision FROM vpn_install_operations WHERE id = ?", (owner.owner_id,)
        ).fetchone()
        if operation and operation["revision"] is not None:
            connection.execute(
                """
                UPDATE vpn_snapshots
                SET lifecycle = 'FAILED', failed_at = ?, error_message = ?
                WHERE revision = ? AND lifecycle = 'PREPARED'
                """,
                (timestamp, reason, operation["revision"]),
            )
        connection.execute(
            """
            UPDATE vpn_install_operations
            SET status = 'FAILED', current_step = 'interrupted',
                error_message = ?, updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """,
            (reason, timestamp, owner.owner_id),
        )
    elif owner.owner_type == "IP_CHANGE":
        operation = connection.execute(
            """
            SELECT purchase_state, make_main_state, apply_state
            FROM ip_change_operations WHERE id = ?
            """,
            (owner.owner_id,),
        ).fetchone()
        external_action_started = bool(
            operation
            and any(
                operation[field] != "NOT_STARTED"
                for field in ("purchase_state", "make_main_state", "apply_state")
            )
        )
        connection.execute(
            """
            UPDATE ip_change_operations
            SET status = ?, current_step = 'interrupted',
                action_state = ?, error_message = ?, updated_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """,
            (
                "AMBIGUOUS" if external_action_started else "FAILED",
                "AMBIGUOUS" if external_action_started else "NOT_STARTED",
                reason,
                timestamp,
                owner.owner_id,
            ),
        )
    elif owner.owner_type == "SERVER":
        operation = connection.execute(
            "SELECT kind, action_state FROM server_operations WHERE id = ?",
            (owner.owner_id,),
        ).fetchone()
        if not operation:
            return
        may_have_sent_reboot = operation["kind"] == "REBOOT" and operation[
            "action_state"
        ] in {"SENDING", "SENT", "AMBIGUOUS"}
        status = "AMBIGUOUS" if may_have_sent_reboot else "FAILED"
        action_state = "AMBIGUOUS" if may_have_sent_reboot else operation["action_state"]
        connection.execute(
            """
            UPDATE server_operations
            SET status = ?, action_state = ?, current_step = 'interrupted',
                error_message = ?, updated_at = ?, completed_at = ?
            WHERE id = ? AND status IN ('PENDING', 'RUNNING')
            """,
            (status, action_state, reason, timestamp, timestamp, owner.owner_id),
        )


def acquire_vps_lease(
    connection: sqlite3.Connection,
    owner_type: str,
    owner_id: int,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> None:
    now = _now()
    existing = connection.execute(
        "SELECT * FROM operation_leases WHERE resource = ?", (VPN_VPS_RESOURCE,)
    ).fetchone()
    if existing:
        existing_owner = LeaseOwner(str(existing["owner_type"]), int(existing["owner_id"]))
        if _parse(str(existing["expires_at"])) > now:
            raise OperationBusyError(existing_owner)
        _fail_owner(
            connection,
            existing_owner,
            "Operation lease expired before completion",
            _iso(now),
        )
        release_vps_lease(
            connection, existing_owner.owner_type, existing_owner.owner_id
        )

    timestamp = _iso(now)
    expires_at = _iso(now + timedelta(seconds=max(1, lease_seconds)))
    connection.execute(
        """
        INSERT INTO operation_leases(
            resource, owner_type, owner_id, acquired_at, heartbeat_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (VPN_VPS_RESOURCE, owner_type, owner_id, timestamp, timestamp, expires_at),
    )


def heartbeat_vps_lease(
    connection: sqlite3.Connection,
    owner_type: str,
    owner_id: int,
    *,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    now = _now()
    cursor = connection.execute(
        """
        UPDATE operation_leases
        SET heartbeat_at = ?, expires_at = ?
        WHERE resource = ? AND owner_type = ? AND owner_id = ?
        """,
        (
            _iso(now),
            _iso(now + timedelta(seconds=max(1, lease_seconds))),
            VPN_VPS_RESOURCE,
            owner_type,
            owner_id,
        ),
    )
    return cursor.rowcount == 1


def release_vps_lease(
    connection: sqlite3.Connection, owner_type: str, owner_id: int
) -> bool:
    cursor = connection.execute(
        """
        DELETE FROM operation_leases
        WHERE resource = ? AND owner_type = ? AND owner_id = ?
        """,
        (VPN_VPS_RESOURCE, owner_type, owner_id),
    )
    return cursor.rowcount == 1


def recover_incomplete_vps_operations(
    connection: sqlite3.Connection, reason: str
) -> None:
    bounded = _bounded_reason(reason)
    timestamp = _iso(_now())

    for row in connection.execute(
        "SELECT id FROM vpn_install_operations WHERE status IN ('PENDING', 'RUNNING')"
    ).fetchall():
        _fail_owner(connection, LeaseOwner("INSTALL", int(row["id"])), bounded, timestamp)
    for row in connection.execute(
        "SELECT id FROM ip_change_operations WHERE status IN ('PENDING', 'RUNNING')"
    ).fetchall():
        _fail_owner(connection, LeaseOwner("IP_CHANGE", int(row["id"])), bounded, timestamp)
    for row in connection.execute(
        "SELECT id FROM server_operations WHERE status IN ('PENDING', 'RUNNING')"
    ).fetchall():
        _fail_owner(connection, LeaseOwner("SERVER", int(row["id"])), bounded, timestamp)
    connection.execute("DELETE FROM operation_leases WHERE resource = ?", (VPN_VPS_RESOURCE,))
