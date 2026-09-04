from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

import app.main as main
from app.config import settings
from app.db import get_db, init_db
from app.vpn_state import get_vpn_snapshot, prepare_install_operation


def test_readyz_reports_current_migrated_database() -> None:
    init_db()

    response = TestClient(main.app).get("/readyz")

    assert (response.status_code, response.content) == (200, b'{"status":"ready"}')


def test_readyz_does_not_create_a_missing_database() -> None:
    path = Path(settings.database_path)
    assert not path.exists()

    response = TestClient(main.app).get("/readyz")

    assert (response.status_code, response.content) == (
        503,
        b'{"status":"not_ready"}',
    )
    assert not path.exists()


def test_readyz_rejects_outdated_schema() -> None:
    init_db()
    with get_db() as db:
        db.execute("DELETE FROM schema_migrations WHERE version = 6")

    response = TestClient(main.app).get("/readyz")

    assert (response.status_code, response.json()) == (503, {"status": "not_ready"})


def test_readyz_hides_unavailable_database_details() -> None:
    Path(settings.database_path).write_bytes(b"not a sqlite database")

    response = TestClient(main.app).get("/readyz")

    assert (response.status_code, response.json()) == (503, {"status": "not_ready"})
    assert settings.database_path.encode() not in response.content


def test_lifespan_initializes_database_and_recovers_incomplete_work() -> None:
    init_db()
    prepared = prepare_install_operation("203.0.113.10")

    with TestClient(main.app) as client:
        assert client.get("/readyz").status_code == 200

    with get_db() as db:
        operation = db.execute(
            "SELECT status FROM vpn_install_operations WHERE id = ?",
            (prepared.operation_id,),
        ).fetchone()
        assert operation["status"] == "FAILED"
        assert db.execute("SELECT 1 FROM operation_leases").fetchone() is None
    assert get_vpn_snapshot(prepared.revision)["lifecycle"] == "FAILED"
