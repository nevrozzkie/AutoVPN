from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
import app.server_operations as server_operations
from app.db import (
    create_operation,
    fail_incomplete_install_operations,
    get_db,
    init_db,
    set_setting,
)
from app.operation_coordinator import OperationBusyError
from app.server_operations import (
    ServerTiming,
    create_server_operation,
    get_active_vps_operation,
    get_server_operation,
    mark_reboot_sending,
    run_server_reboot,
    run_server_status,
    update_server_operation,
)
from app.vpn_state import prepare_install_operation


AUTH = ("admin", "pw12345")
TIMING = ServerTiming(
    request_timeout=0.1,
    reboot_timeout=2,
    poll_interval=1,
    ssh_probe_timeout=0.1,
    command_timeout=0.1,
)


def _configure(*, aeza: bool = True) -> None:
    init_db()
    set_setting("config.admin_username", AUTH[0])
    set_setting("config.admin_password", AUTH[1])
    set_setting("current_ip", "203.0.113.10")
    set_setting("config.eu_ssh_port", "2222")
    if aeza:
        set_setting("config.aeza_token", "aeza-secret-token")
        set_setting("config.aeza_service_id", "service-123")


def _client(*, aeza: bool = True) -> TestClient:
    _configure(aeza=aeza)
    return TestClient(main.app, base_url="http://panel.local")


class FakeAezaClient:
    services: list[object] = []
    reboot_calls = 0
    reboot_error: BaseException | None = None

    def __init__(self, api_base: str, token: str, timeout: float = 30.0) -> None:
        assert token == "aeza-secret-token"
        self.timeout = timeout

    async def get_service(self, service_id: str) -> dict[str, object]:
        assert service_id == "service-123"
        value = self.__class__.services.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value  # type: ignore[return-value]

    async def reboot_service(self, service_id: str) -> dict[str, object]:
        assert service_id == "service-123"
        self.__class__.reboot_calls += 1
        if self.__class__.reboot_error:
            raise self.__class__.reboot_error
        return {}


def _install_runner_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    services: list[object],
    ssh: list[bool],
    service_output: str = (
        "xray=active\nhysteria-server=active\nawg-quick@awg0=active\n"
    ),
    protocols: list[dict[str, object]] | None = None,
    reboot_error: BaseException | None = None,
) -> tuple[list[int], list[float | None]]:
    FakeAezaClient.services = list(services)
    FakeAezaClient.reboot_calls = 0
    FakeAezaClient.reboot_error = reboot_error
    ssh_ports: list[int] = []
    command_timeouts: list[float | None] = []

    async def fake_tcp_check(host: str, port: int, timeout: float) -> bool:
        assert host == "203.0.113.10"
        ssh_ports.append(port)
        return ssh.pop(0)

    async def fake_remote_command(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        assert host == "203.0.113.10"
        assert "systemctl is-active" in command
        assert stdin_data == ""
        command_timeouts.append(timeout)
        return 0, service_output

    async def fake_protocol_statuses(
        host: str, *, command_timeout: float | None = None
    ) -> list[dict[str, object]]:
        assert host == "203.0.113.10"
        assert command_timeout == TIMING.command_timeout
        return protocols or [
            {"key": "vless", "status": "VERIFIED"},
            {"key": "hysteria_quic", "status": "SERVICE_ACTIVE"},
            {"key": "amnezia", "status": "VERIFIED"},
        ]

    monkeypatch.setattr(server_operations, "AezaClient", FakeAezaClient)
    monkeypatch.setattr(server_operations, "tcp_check", fake_tcp_check)
    monkeypatch.setattr(server_operations, "run_remote_command", fake_remote_command)
    monkeypatch.setattr(
        server_operations, "refresh_protocol_statuses", fake_protocol_statuses
    )
    return ssh_ports, command_timeouts


async def _no_sleep(_: float) -> None:
    return None


def test_server_routes_require_basic_auth_and_post_csrf() -> None:
    client = _client()

    for path in (
        "/admin/server",
        "/admin/server/reboot/confirm",
        "/admin/server/operations/1",
    ):
        assert client.get(path).status_code == 401

    for path, data in (
        ("/admin/server/status/refresh", {}),
        ("/admin/server/reboot", {"confirm": "REBOOT"}),
    ):
        response = client.post(
            path,
            data=data,
            auth=AUTH,
            headers={"Origin": "http://evil.example"},
        )
        assert response.status_code == 403


def test_reboot_confirmation_is_side_effect_free_and_exact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    reboot_calls: list[int] = []

    async def forbidden_background(operation_id: int) -> None:
        reboot_calls.append(operation_id)

    monkeypatch.setattr(main, "_run_server_reboot_background", forbidden_background)

    confirmation = client.get("/admin/server/reboot/confirm", auth=AUTH)
    invalid = client.post(
        "/admin/server/reboot",
        data={"confirm": "reboot"},
        auth=AUTH,
        headers={"Origin": "http://panel.local"},
    )

    assert confirmation.status_code == 200
    assert "Aeza" in confirmation.text
    assert invalid.status_code == 400
    assert reboot_calls == []
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM server_operations").fetchone()[
            "count"
        ] == 0


def test_server_actions_are_unavailable_without_aeza_configuration() -> None:
    client = _client(aeza=False)

    status_response = client.post(
        "/admin/server/status/refresh",
        auth=AUTH,
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )
    reboot_response = client.get(
        "/admin/server/reboot/confirm", auth=AUTH, follow_redirects=False
    )

    assert status_response.status_code == 303
    assert status_response.headers["location"] == "/admin/server?aeza_required=1"
    assert reboot_response.status_code == 303
    assert reboot_response.headers["location"] == "/admin/server?aeza_required=1"


def test_server_page_escapes_aeza_status_and_links_to_history() -> None:
    client = _client()
    operation_id = create_server_operation("STATUS")
    update_server_operation(
        operation_id,
        status="DONE",
        provider_status="<script>alert(1)</script>",
        services_json='{"xray":"active"}',
        completed_at="2026-01-01T00:00:00+00:00",
    )

    response = client.get("/admin/server", auth=AUTH)

    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text
    assert f'/admin/server/operations/{operation_id}' in response.text
    assert "xray" in response.text


def test_double_submit_and_other_operations_cannot_bypass_vps_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    scheduled: list[int] = []

    async def leave_pending(operation_id: int) -> None:
        scheduled.append(operation_id)

    monkeypatch.setattr(main, "_run_server_reboot_background", leave_pending)
    first = client.post(
        "/admin/server/reboot",
        data={"confirm": "REBOOT"},
        auth=AUTH,
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )
    second = client.post(
        "/admin/server/reboot",
        data={"confirm": "REBOOT"},
        auth=AUTH,
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert first.status_code == 303
    assert first.headers["location"].startswith("/admin/server/operations/")
    assert second.status_code == 303
    assert second.headers["location"] == "/admin/server?operation_busy=1"
    assert len(scheduled) == 1
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM server_operations").fetchone()[
            "count"
        ] == 1
    with pytest.raises(OperationBusyError):
        create_operation()
    with pytest.raises(OperationBusyError):
        prepare_install_operation("203.0.113.10")


@pytest.mark.anyio
async def test_status_refresh_uses_configured_ssh_port_and_bounded_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    ssh_ports, command_timeouts = _install_runner_fakes(
        monkeypatch,
        services=[
            {
                "provider_status": "run\nning",
                "ip": "203.0.113.10",
                "raw": {"secret": "must-not-be-stored"},
            }
        ],
        ssh=[True],
    )
    operation_id = create_server_operation("STATUS")

    await run_server_status(operation_id, timing=TIMING)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "DONE"
    assert operation["provider_status"] == "run ning"
    assert operation["ssh_status"] == "UP"
    assert "must-not-be-stored" not in repr(operation)
    assert ssh_ports == [2222]
    assert command_timeouts == [TIMING.command_timeout]
    assert get_active_vps_operation() is None


@pytest.mark.anyio
async def test_status_provider_timeout_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    _install_runner_fakes(
        monkeypatch,
        services=[TimeoutError("provider timeout")],
        ssh=[],
    )
    operation_id = create_server_operation("STATUS")

    await run_server_status(operation_id, timing=TIMING)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "TIMED_OUT"
    assert "Aeza" in operation["error_message"]
    assert get_active_vps_operation() is None


@pytest.mark.anyio
async def test_reboot_observes_ssh_down_then_up_and_healthy_services(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    ssh_ports, _ = _install_runner_fakes(
        monkeypatch,
        services=[
            {"provider_status": "running", "ip": "203.0.113.10"},
            {"provider_status": "rebooting", "ip": "203.0.113.10"},
            {"provider_status": "running", "ip": "203.0.113.10"},
        ],
        ssh=[False, True],
    )
    operation_id = create_server_operation("REBOOT")

    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "DONE"
    assert operation["action_state"] == "SENT"
    assert operation["ssh_status"] == "UP"
    assert operation["warning_message"] == ""
    assert FakeAezaClient.reboot_calls == 1
    assert ssh_ports == [2222, 2222]
    assert get_active_vps_operation() is None


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider_statuses", "ssh_values", "expected_step", "expected_text"),
    [
        (["rebooting", "rebooting"], [True, True], "wait_provider", "Aeza"),
        (["running", "running"], [False, False], "wait_ssh", "SSH"),
    ],
)
async def test_reboot_provider_and_ssh_timeouts_are_distinct(
    monkeypatch: pytest.MonkeyPatch,
    provider_statuses: list[str],
    ssh_values: list[bool],
    expected_step: str,
    expected_text: str,
) -> None:
    _configure()
    _install_runner_fakes(
        monkeypatch,
        services=[
            {"provider_status": "running", "ip": "203.0.113.10"},
            *[
                {"provider_status": value, "ip": "203.0.113.10"}
                for value in provider_statuses
            ],
        ],
        ssh=ssh_values,
    )
    operation_id = create_server_operation("REBOOT")

    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "TIMED_OUT"
    assert operation["current_step"] == expected_step
    assert expected_text in operation["error_message"]
    assert FakeAezaClient.reboot_calls == 1


@pytest.mark.anyio
async def test_reboot_records_unhealthy_service_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    _install_runner_fakes(
        monkeypatch,
        services=[
            {"provider_status": "running", "ip": "203.0.113.10"},
            {"provider_status": "running", "ip": "203.0.113.10"},
        ],
        ssh=[True],
        service_output=(
            "xray=inactive\nhysteria-server=active\nawg-quick@awg0=active\n"
        ),
    )
    operation_id = create_server_operation("REBOOT")

    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "FAILED"
    assert "inactive services: xray" in operation["error_message"]
    assert FakeAezaClient.reboot_calls == 1


@pytest.mark.anyio
async def test_ambiguous_reboot_is_never_sent_again_and_redacts_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    _install_runner_fakes(
        monkeypatch,
        services=[
            {"provider_status": "running", "ip": "203.0.113.10"},
            {"provider_status": "running", "ip": "203.0.113.10"},
        ],
        ssh=[True],
        reboot_error=TimeoutError("timeout aeza-secret-token\nfrom Aeza"),
    )
    operation_id = create_server_operation("REBOOT")

    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)
    first = get_server_operation(operation_id)
    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)
    second = get_server_operation(operation_id)

    assert first is not None and second is not None
    assert first["status"] == second["status"] == "AMBIGUOUS"
    assert first["action_state"] == second["action_state"] == "AMBIGUOUS"
    assert "aeza-secret-token" not in repr(second)
    assert FakeAezaClient.reboot_calls == 1
    assert get_active_vps_operation() is None


@pytest.mark.anyio
async def test_recovered_sending_reboot_is_not_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure()
    _install_runner_fakes(monkeypatch, services=[], ssh=[])
    operation_id = create_server_operation("REBOOT")
    assert mark_reboot_sending(operation_id) is True
    fail_incomplete_install_operations("process restarted during VPS operation")

    await run_server_reboot(operation_id, timing=TIMING, sleeper=_no_sleep)

    operation = get_server_operation(operation_id)
    assert operation is not None
    assert operation["status"] == "AMBIGUOUS"
    assert operation["action_state"] == "AMBIGUOUS"
    assert FakeAezaClient.reboot_calls == 0


def test_terminal_update_releases_only_its_own_lock() -> None:
    _configure()
    operation_id = create_server_operation("STATUS")
    with get_db() as db:
        db.execute(
            "UPDATE operation_leases SET owner_type = 'INSTALL', owner_id = 999 "
            "WHERE resource = 'vpn_vps'"
        )

    update_server_operation(operation_id, status="FAILED", error_message="test")

    active = get_active_vps_operation()
    assert active is not None
    assert (active["owner_type"], active["owner_id"]) == ("INSTALL", 999)
