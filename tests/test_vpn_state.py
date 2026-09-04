from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

import app.eu_install as eu_install
import app.main as main
from app.db import (
    create_client,
    delete_client,
    get_client_by_token,
    get_db,
    get_latest_install_operation,
    get_vpn_state,
    init_db,
    list_clients,
    purge_applied_deleted_clients,
    set_client_enabled,
    set_setting,
    set_settings,
    update_client_name,
    upsert_client_stats,
)
from app.eu_install import EuInstallError, build_eu_install_script, run_eu_install
from app.security import hash_password
from app.vpn_config import capture_vpn_config
from app.vpn_state import (
    get_vpn_snapshot,
    load_install_snapshot,
    prepare_install_operation,
)


def _revisions() -> tuple[int, int | None]:
    state = get_vpn_state()
    applied = state["applied_revision"]
    return int(state["desired_revision"]), int(applied) if applied is not None else None


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    exit_code: int = 0,
    scripts: list[str] | None = None,
) -> None:
    def fake_run(host: str, script: str) -> tuple[int, str]:
        if scripts is not None:
            scripts.append(script)
        return exit_code, "fake ssh output"

    async def fake_refresh(host: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(eu_install, "_run_script_over_ssh", fake_run)
    monkeypatch.setattr(eu_install, "refresh_protocol_statuses", fake_refresh)


def test_vpn_mutations_advance_desired_revision_once_each() -> None:
    init_db()
    initial_desired, applied = _revisions()
    assert applied is None
    init_db()
    assert _revisions() == (initial_desired, None)

    set_setting("stats.last_error", "not a VPN setting")
    assert _revisions() == (initial_desired, None)

    set_setting("current_ip", "203.0.113.10")
    assert _revisions() == (initial_desired + 1, None)
    set_setting("current_ip", "203.0.113.10")
    assert _revisions() == (initial_desired + 1, None)

    set_settings(
        {"config.vless_port": "9443", "config.hysteria_port": "9444"},
        mark_vpn_config_updated=True,
    )
    assert _revisions() == (initial_desired + 2, None)
    set_settings(
        {"config.vless_port": "9443", "config.hysteria_port": "9444"},
        mark_vpn_config_updated=True,
    )
    set_settings(
        {"config.admin_username": "another-admin"},
        mark_vpn_config_updated=True,
    )
    assert _revisions() == (initial_desired + 2, None)

    client = create_client("Alice")
    assert _revisions() == (initial_desired + 3, None)
    update_client_name(client["id"], "Alice")
    set_client_enabled(client["id"], True)
    assert _revisions() == (initial_desired + 3, None)
    update_client_name(client["id"], "Bob")
    assert _revisions() == (initial_desired + 4, None)
    set_client_enabled(client["id"], False)
    assert _revisions() == (initial_desired + 5, None)
    set_client_enabled(client["id"], False)
    assert _revisions() == (initial_desired + 5, None)

    upsert_client_stats(
        client["id"],
        vless_uplink=1,
        vless_downlink=2,
        amnezia_rx=3,
        amnezia_tx=4,
        amnezia_latest_handshake=5,
        last_seen_at=None,
        raw="stats",
    )
    assert _revisions() == (initial_desired + 5, None)

    delete_client(client["id"])
    assert _revisions() == (initial_desired + 6, None)


def test_snapshot_payload_and_hash_are_canonical_and_stable() -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    create_client("Alice")

    first = prepare_install_operation("203.0.113.10")
    first_snapshot = get_vpn_snapshot(first.revision)
    second = prepare_install_operation("203.0.113.10")
    second_snapshot = get_vpn_snapshot(second.revision)

    assert first.revision == second.revision
    assert first.payload_sha256 == second.payload_sha256
    assert first_snapshot == second_snapshot
    assert first_snapshot is not None
    assert first_snapshot["payload_json"].startswith('{"amnezia":')
    assert hashlib.sha256(first_snapshot["payload_json"].encode()).hexdigest() == (
        first_snapshot["payload_sha256"]
    )


def test_unknown_applied_state_is_dirty_and_cannot_purge() -> None:
    init_db()
    client = create_client("Alice")
    delete_client(client["id"])

    with get_db() as db:
        assert purge_applied_deleted_clients(db) == 0
        assert db.execute(
            "SELECT token FROM clients WHERE id = ?", (client["id"],)
        ).fetchone()["token"] == client["token"]


def test_dashboard_labels_unknown_applied_state_as_unconfirmed() -> None:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", hash_password("secret"))
    response = TestClient(main.app).get("/admin", auth=("admin", "secret"))

    assert response.status_code == 200
    assert "applied не подтверждено" in response.text
    assert "Состояние VPS не подтверждено — нужна синхронизация" in response.text


def test_install_post_atomically_binds_prepared_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", hash_password("secret"))
    set_setting("current_ip", "203.0.113.10")

    async def do_not_start_ssh(operation_id: int) -> None:
        return None

    monkeypatch.setattr(main, "_run_install_background", do_not_start_ssh)
    response = TestClient(main.app).post(
        "/admin/install/run",
        auth=("admin", "secret"),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    operation = get_latest_install_operation()
    assert operation["revision"] == get_vpn_state()["desired_revision"]
    assert get_vpn_snapshot(operation["revision"])["lifecycle"] == "PREPARED"


def test_prepared_install_script_ignores_later_live_mutation() -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Alice")
    prepared = prepare_install_operation("203.0.113.10")
    before = get_vpn_snapshot(prepared.revision)

    update_client_name(client["id"], "Bob")
    operation, stored = load_install_snapshot(prepared.operation_id)
    script = build_eu_install_script(stored)

    assert operation["revision"] == prepared.revision
    assert stored.clients[0].name == "Alice"
    assert capture_vpn_config().clients[0].name == "Bob"
    assert f"Alice-{client['id']}" in script
    assert f"Bob-{client['id']}" not in script
    assert get_vpn_snapshot(prepared.revision) == before


def test_snapshot_excludes_disabled_and_deleted_clients() -> None:
    init_db()
    disabled = create_client("Disabled")
    deleted = create_client("Deleted")
    set_client_enabled(disabled["id"], False)
    delete_client(deleted["id"])

    prepared = prepare_install_operation("203.0.113.10")
    _, stored = load_install_snapshot(prepared.operation_id)

    assert stored.clients == ()


@pytest.mark.anyio
async def test_failed_apply_keeps_previous_applied_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Alice")
    first = prepare_install_operation("203.0.113.10")
    _install_fakes(monkeypatch)
    await run_eu_install(first.operation_id)
    assert _revisions() == (first.revision, first.revision)

    update_client_name(client["id"], "Bob")
    failed = prepare_install_operation("203.0.113.10")
    _install_fakes(monkeypatch, exit_code=1)
    with pytest.raises(EuInstallError, match="exit code 1"):
        await run_eu_install(failed.operation_id)

    assert _revisions() == (failed.revision, first.revision)
    assert get_vpn_snapshot(first.revision)["lifecycle"] == "APPLIED"
    assert get_vpn_snapshot(failed.revision)["lifecycle"] == "FAILED"


@pytest.mark.anyio
async def test_success_applies_job_revision_but_keeps_newer_desired_dirty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Alice")
    prepared = prepare_install_operation("203.0.113.10")
    delete_client(client["id"])
    scripts: list[str] = []
    _install_fakes(monkeypatch, scripts=scripts)

    await run_eu_install(prepared.operation_id)

    assert _revisions() == (prepared.revision + 1, prepared.revision)
    assert f"Alice-{client['id']}" in scripts[0]
    with get_db() as db:
        retained = db.execute(
            "SELECT deleted_revision FROM clients WHERE id = ?", (client["id"],)
        ).fetchone()
    assert retained["deleted_revision"] == prepared.revision + 1


@pytest.mark.anyio
async def test_soft_delete_is_retained_until_successful_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Alice")
    initial = prepare_install_operation("203.0.113.10")
    _install_fakes(monkeypatch)
    await run_eu_install(initial.operation_id)

    delete_client(client["id"])
    assert get_client_by_token(client["token"]) is None
    assert list_clients() == []
    with get_db() as db:
        retained = db.execute(
            "SELECT token, amnezia_ipv4, deleted_revision FROM clients WHERE id = ?",
            (client["id"],),
        ).fetchone()
    assert retained["token"] == client["token"]
    deleted_revision = int(retained["deleted_revision"])
    while_deleted = create_client("While deleted")
    assert while_deleted["amnezia_ipv4"] != client["amnezia_ipv4"]

    failed = prepare_install_operation("203.0.113.10")
    assert failed.revision > deleted_revision
    _install_fakes(monkeypatch, exit_code=1)
    with pytest.raises(EuInstallError):
        await run_eu_install(failed.operation_id)
    with get_db() as db:
        assert purge_applied_deleted_clients(db) == 0
        assert db.execute("SELECT 1 FROM clients WHERE id = ?", (client["id"],)).fetchone()

    retry = prepare_install_operation("203.0.113.10")
    _install_fakes(monkeypatch)
    await run_eu_install(retry.operation_id)
    with get_db() as db:
        assert db.execute("SELECT 1 FROM clients WHERE id = ?", (client["id"],)).fetchone() is None
    replacement = create_client("Replacement")
    assert replacement["amnezia_ipv4"] == client["amnezia_ipv4"]
    assert _revisions() == (retry.revision + 1, retry.revision)
