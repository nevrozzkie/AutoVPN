from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import app.operation_coordinator as coordinator
from app.db import (
    create_operation,
    fail_incomplete_install_operations,
    get_db,
    init_db,
    update_operation,
)
from app.operation_coordinator import (
    OperationBusyError,
    acquire_vps_lease,
    release_vps_lease,
)
from app.vpn_state import fail_install_operation, get_vpn_snapshot, prepare_install_operation


def test_two_competing_creates_persist_exactly_one_operation_and_lock() -> None:
    init_db()

    operation_id = create_operation()
    with pytest.raises(OperationBusyError) as error:
        create_operation()

    assert error.value.owner.owner_type == "IP_CHANGE"
    assert error.value.owner.owner_id == operation_id
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM ip_change_operations").fetchone()[
            "count"
        ] == 1
        assert db.execute("SELECT owner_type, owner_id FROM operation_leases").fetchone() == {
            "owner_type": "IP_CHANGE",
            "owner_id": operation_id,
        }


def test_install_and_ip_operations_are_mutually_exclusive() -> None:
    init_db()

    install = prepare_install_operation("203.0.113.10")
    with pytest.raises(OperationBusyError):
        create_operation()

    fail_install_operation(install.operation_id, "test release")
    ip_operation_id = create_operation()
    with pytest.raises(OperationBusyError):
        prepare_install_operation("203.0.113.10")

    update_operation(ip_operation_id, status="FAILED", current_step="failed")
    assert prepare_install_operation("203.0.113.10").operation_id > install.operation_id


def test_release_only_succeeds_for_current_owner() -> None:
    init_db()
    operation_id = create_operation()

    with get_db() as db:
        assert release_vps_lease(db, "INSTALL", operation_id) is False
        assert db.execute("SELECT owner_type, owner_id FROM operation_leases").fetchone() == {
            "owner_type": "IP_CHANGE",
            "owner_id": operation_id,
        }
        assert release_vps_lease(db, "IP_CHANGE", operation_id) is True
        assert db.execute("SELECT 1 FROM operation_leases").fetchone() is None


def test_expired_lease_fails_old_owner_before_new_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    started = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(coordinator, "_now", lambda: started)
    first_id = create_operation()

    with get_db() as db:
        cursor = db.execute(
            """
            INSERT INTO ip_change_operations(status, current_step, created_at, updated_at)
            VALUES ('PENDING', 'queued', ?, ?)
            """,
            (started.isoformat(), started.isoformat()),
        )
        second_id = int(cursor.lastrowid)

    monkeypatch.setattr(coordinator, "_now", lambda: started + timedelta(hours=2))
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        acquire_vps_lease(db, "IP_CHANGE", second_id)

    with get_db() as db:
        assert db.execute(
            "SELECT status FROM ip_change_operations WHERE id = ?", (first_id,)
        ).fetchone()["status"] == "FAILED"
        assert db.execute("SELECT owner_id FROM operation_leases").fetchone()[
            "owner_id"
        ] == second_id


def test_startup_recovery_closes_all_jobs_and_never_resends_reboot() -> None:
    init_db()
    install = prepare_install_operation("203.0.113.10")
    timestamp = datetime.now(UTC).isoformat(timespec="seconds")
    with get_db() as db:
        ip_id = int(
            db.execute(
                """
                INSERT INTO ip_change_operations(status, current_step, created_at, updated_at)
                VALUES ('RUNNING', 'wait_vps_health', ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
        )
        status_id = int(
            db.execute(
                """
                INSERT INTO server_operations(
                    kind, status, current_step, action_state, created_at, updated_at
                ) VALUES ('STATUS', 'RUNNING', 'provider_status', 'NOT_STARTED', ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
        )
        reboot_id = int(
            db.execute(
                """
                INSERT INTO server_operations(
                    kind, status, current_step, action_state, created_at, updated_at
                ) VALUES ('REBOOT', 'RUNNING', 'send_reboot', 'SENDING', ?, ?)
                """,
                (timestamp, timestamp),
            ).lastrowid
        )

    fail_incomplete_install_operations("x" * 2000)

    with get_db() as db:
        assert db.execute(
            "SELECT status FROM vpn_install_operations WHERE id = ?",
            (install.operation_id,),
        ).fetchone()["status"] == "FAILED"
        assert get_vpn_snapshot(install.revision)["lifecycle"] == "FAILED"
        assert db.execute(
            "SELECT status FROM ip_change_operations WHERE id = ?", (ip_id,)
        ).fetchone()["status"] == "FAILED"
        assert db.execute(
            "SELECT status FROM server_operations WHERE id = ?", (status_id,)
        ).fetchone()["status"] == "FAILED"
        reboot = db.execute(
            "SELECT status, action_state, error_message FROM server_operations WHERE id = ?",
            (reboot_id,),
        ).fetchone()
        assert reboot["status"] == "AMBIGUOUS"
        assert reboot["action_state"] == "AMBIGUOUS"
        assert len(reboot["error_message"]) == 1000
        assert db.execute("SELECT 1 FROM operation_leases").fetchone() is None
        assert db.execute(
            """
            SELECT 1 FROM (
                SELECT status FROM vpn_install_operations
                UNION ALL SELECT status FROM ip_change_operations
                UNION ALL SELECT status FROM server_operations
            ) WHERE status IN ('PENDING', 'RUNNING')
            """
        ).fetchone() is None
