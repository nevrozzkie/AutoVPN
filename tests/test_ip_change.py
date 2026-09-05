from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.ip_change as ip_change
import app.main as main
from app.db import (
    create_client,
    create_operation,
    get_db,
    get_latest_operation,
    get_setting,
    get_vpn_state,
    init_db,
    set_setting,
    update_operation,
)
from app.deep_protocol_checks import DeepCheckResult
from app.ip_change import IpRotationTiming, run_ip_change
from app.vpn_state import IpChangeSafetyHoldError


OLD_IP = "203.0.113.10"
NEW_IP = "203.0.113.20"


class FakeAezaClient:
    events: list[str] = []
    fail_at: str = ""
    main_id: str = "old-id"

    def __init__(self, api_base: str, token: str, timeout: float = 30.0) -> None:
        del api_base, token
        self.timeout = timeout

    async def get_ipv4_list(self, service_id: str) -> list[dict[str, object]]:
        del service_id
        self.__class__.events.append("list")
        if self.__class__.fail_at == "list":
            raise TimeoutError("provider list timeout")
        return [
            {"id": "old-id", "ip": OLD_IP, "is_main": self.__class__.main_id == "old-id"},
            {"id": "new-id", "ip": NEW_IP, "is_main": self.__class__.main_id == "new-id"},
        ]

    async def add_ipv4(
        self, service_id: str, payment_method: str, domain: str
    ) -> dict[str, object]:
        del service_id, payment_method, domain
        self.__class__.events.append("buy")
        if self.__class__.fail_at == "buy":
            raise TimeoutError("purchase timeout secret-token")
        return {"id": "new-id", "ip": NEW_IP}

    async def make_main_ipv4(
        self, service_id: str, ipv4_id: str
    ) -> dict[str, object]:
        del service_id
        self.__class__.events.append(f"main:{ipv4_id}")
        if self.__class__.fail_at == "make-main" and ipv4_id == "new-id":
            raise TimeoutError("make-main timeout")
        if self.__class__.fail_at == "rollback" and ipv4_id == "old-id":
            raise TimeoutError("rollback timeout")
        if self.__class__.fail_at != "confirm-main":
            self.__class__.main_id = ipv4_id
        return {}

    async def delete_ipv4(
        self, service_id: str, ipv4_id: str
    ) -> dict[str, object]:
        del service_id, ipv4_id
        self.__class__.events.append("delete")
        if self.__class__.fail_at == "delete":
            raise TimeoutError("response lost after delete may have completed")
        return {}


@pytest.fixture
def prepared_operation(monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(ip_change, "AezaClient", FakeAezaClient)
    return _prepare_operation(amnezia_enabled=False)


def _prepare_operation(*, amnezia_enabled: bool) -> int:
    init_db()
    set_setting("config.aeza_token", "secret-token")
    set_setting("config.aeza_service_id", "service-id")
    set_setting("config.aeza_ipv4_payment_method", "balance")
    set_setting("config.aeza_ipv4_domain", "auto-vpn")
    set_setting("config.eu_ssh_port", "2222")
    set_setting("config.vless_enabled", "1")
    set_setting("config.hysteria_enabled", "1")
    set_setting("config.amnezia_enabled", "1" if amnezia_enabled else "0")
    set_setting("current_ip", OLD_IP)
    create_client("Bound client")
    FakeAezaClient.events = []
    FakeAezaClient.fail_at = ""
    FakeAezaClient.main_id = "old-id"
    return create_operation()


@pytest.fixture
def timing() -> IpRotationTiming:
    return IpRotationTiming(
        provider_timeout=0.2,
        after_purchase_delay=120,
        ip_appear_timeout=0.2,
        ip_appear_interval=0.1,
        ssh_timeout=0.2,
        ssh_interval=0.1,
        ssh_probe_timeout=0.1,
        command_timeout=0.2,
    )


def _operation(operation_id: int) -> dict[str, Any]:
    with get_db() as db:
        return db.execute(
            "SELECT * FROM ip_change_operations WHERE id = ?", (operation_id,)
        ).fetchone()


async def _no_sleep(_: float) -> None:
    return None


def _verified_results() -> dict[str, DeepCheckResult]:
    return {
        "vless": DeepCheckResult(True),
        "hysteria_quic": DeepCheckResult(True),
        "hysteria_salamander": DeepCheckResult(True),
    }


def test_provider_main_must_match_published_current_ip() -> None:
    with pytest.raises(ip_change.IpChangeError, match="does not match"):
        ip_change._find_main_ip(
            [{"id": "other", "ip": "198.51.100.9", "is_main": True}],
            OLD_IP,
        )


def test_new_ip_selection_never_uses_an_existing_secondary_by_guess() -> None:
    existing = [
        {"id": "old-id", "ip": OLD_IP, "is_main": True},
        {"id": "secondary-id", "ip": "198.51.100.7", "is_main": False},
    ]

    selected = ip_change._find_new_ip(
        existing,
        "old-id",
        OLD_IP,
        "",
        "",
        {"old-id", "secondary-id"},
        {OLD_IP, "198.51.100.7"},
    )

    assert selected is None


def _install_success_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def ok_tcp(*_: object, **__: object) -> bool:
        return True

    async def ok_remote(*_: object, **__: object) -> tuple[int, str]:
        return 0, ""

    async def ok_deep(*_: object, **__: object) -> dict[str, DeepCheckResult]:
        return _verified_results()

    monkeypatch.setattr(ip_change, "tcp_check", ok_tcp)
    monkeypatch.setattr(ip_change, "run_remote_command", ok_remote)
    monkeypatch.setattr(ip_change, "run_deep_protocol_checks", ok_deep)


@pytest.mark.anyio
async def test_safe_rotation_orders_actions_and_publishes_only_after_verification(
    prepared_operation: int,
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = FakeAezaClient.events
    ssh_ports: list[int] = []
    scripts: list[str] = []

    async def fake_tcp(host: str, port: int, timeout: float) -> bool:
        assert host == NEW_IP
        assert get_setting("current_ip") == OLD_IP
        ssh_ports.append(port)
        events.append("ssh")
        return True

    async def fake_remote(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        assert (host, command, timeout) == (NEW_IP, "bash -s", timing.command_timeout)
        assert get_setting("current_ip") == OLD_IP
        assert "apt-get install" not in stdin_data
        scripts.append(stdin_data)
        events.append("apply")
        return 0, "not persisted"

    async def fake_deep(*args: object, **kwargs: object) -> dict[str, DeepCheckResult]:
        assert args[0] == NEW_IP
        assert kwargs["target_host"] == NEW_IP
        assert get_setting("current_ip") == OLD_IP
        events.append("health")
        return _verified_results()

    original_delete = FakeAezaClient.delete_ipv4

    async def assert_published_then_delete(
        self: FakeAezaClient, service_id: str, ipv4_id: str
    ) -> dict[str, object]:
        assert get_setting("current_ip") == NEW_IP
        state = get_vpn_state()
        assert state["applied_revision"] == state["desired_revision"]
        return await original_delete(self, service_id, ipv4_id)

    monkeypatch.setattr(ip_change, "tcp_check", fake_tcp)
    monkeypatch.setattr(ip_change, "run_remote_command", fake_remote)
    monkeypatch.setattr(ip_change, "run_deep_protocol_checks", fake_deep)
    monkeypatch.setattr(FakeAezaClient, "delete_ipv4", assert_published_then_delete)

    await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    operation = _operation(prepared_operation)
    assert operation["status"] == "DONE"
    assert operation["error_message"] == ""
    assert operation["cleanup_warning"] == ""
    assert operation["published_revision"] == operation["revision"] + 1
    assert ssh_ports == [2222]
    assert len(scripts) == 1
    assert events == [
        "list",
        "buy",
        "list",
        "main:new-id",
        "list",
        "ssh",
        "apply",
        "health",
        "delete",
    ]


@pytest.mark.anyio
async def test_awg_never_deletes_safety_ip_without_real_new_endpoint_probe(
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ip_change, "AezaClient", FakeAezaClient)
    prepared_operation = _prepare_operation(amnezia_enabled=True)
    _install_success_fakes(monkeypatch)

    await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    operation = _operation(prepared_operation)
    assert operation["status"] == "DONE"
    assert operation["current_step"] == "manual_verification_required"
    assert operation["action_state"] == "PUBLISHED_AWAITING_AWG_CHECK"
    assert "no real AmneziaWG probe" in operation["cleanup_warning"]
    assert "delete" not in FakeAezaClient.events
    assert get_setting("current_ip") == NEW_IP
    assert get_setting("last_healthcheck_status") == "WARNING"
    assert "NEW_ENDPOINT_NOT_VERIFIED" in operation["healthcheck_result"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "ambiguous", "rollback"),
    (
        ("list", False, False),
        ("buy", True, False),
        ("make-main", False, True),
        ("confirm-main", False, True),
        ("ssh", False, True),
        ("apply", False, True),
        ("health", False, True),
    ),
)
async def test_failures_never_publish_or_delete_old_ip_and_rollback_after_main(
    failure: str,
    ambiguous: bool,
    rollback: bool,
    prepared_operation: int,
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAezaClient.fail_at = (
        failure
        if failure in {"list", "buy", "make-main", "confirm-main"}
        else ""
    )

    async def fake_tcp(*_: object, **__: object) -> bool:
        return failure != "ssh"

    async def fake_remote(*_: object, **__: object) -> tuple[int, str]:
        return (1, "secret output") if failure == "apply" else (0, "")

    async def fake_deep(*_: object, **__: object) -> dict[str, DeepCheckResult]:
        if failure == "health":
            return {
                "vless": DeepCheckResult(False),
                "hysteria_salamander": DeepCheckResult(True),
            }
        return _verified_results()

    monkeypatch.setattr(ip_change, "tcp_check", fake_tcp)
    monkeypatch.setattr(ip_change, "run_remote_command", fake_remote)
    monkeypatch.setattr(ip_change, "run_deep_protocol_checks", fake_deep)

    with pytest.raises((TimeoutError, ip_change.IpChangeError)):
        await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    operation = _operation(prepared_operation)
    assert operation["status"] == ("AMBIGUOUS" if ambiguous else "FAILED")
    assert get_setting("current_ip") == OLD_IP
    assert operation["published_revision"] is None
    assert "delete" not in FakeAezaClient.events
    assert ("main:old-id" in FakeAezaClient.events) is rollback
    assert "secret-token" not in operation["error_message"]


@pytest.mark.anyio
async def test_new_ip_appearance_timeout_keeps_old_ip_and_skips_make_main(
    prepared_operation: int,
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def old_ip_only(
        self: FakeAezaClient, service_id: str
    ) -> list[dict[str, object]]:
        nonlocal calls
        del self, service_id
        calls += 1
        FakeAezaClient.events.append("list")
        return [{"id": "old-id", "ip": OLD_IP, "is_main": True}]

    monkeypatch.setattr(FakeAezaClient, "get_ipv4_list", old_ip_only)

    with pytest.raises(ip_change.IpChangeError, match="did not appear"):
        await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    assert calls >= 2
    assert get_setting("current_ip") == OLD_IP
    assert not any(event.startswith("main:") for event in FakeAezaClient.events)
    assert "delete" not in FakeAezaClient.events


@pytest.mark.anyio
async def test_cleanup_failure_is_done_with_separate_warning(
    prepared_operation: int,
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeAezaClient.fail_at = "delete"
    _install_success_fakes(monkeypatch)

    await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    operation = get_latest_operation()
    assert operation["status"] == "DONE"
    assert operation["error_message"] == ""
    assert operation["action_state"] == "CLEANUP_AMBIGUOUS"
    assert "outcome is ambiguous" in operation["cleanup_warning"]
    assert "check Aeza" in operation["cleanup_warning"]
    assert "was preserved" not in operation["cleanup_warning"]
    assert operation["rollback_outcome"] is None
    assert get_setting("current_ip") == NEW_IP


@pytest.mark.anyio
async def test_concurrent_desired_change_uses_bound_snapshot_and_rolls_back(
    prepared_operation: int,
    timing: IpRotationTiming,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    remote_calls = 0

    async def mutate_after_main(*_: object, **__: object) -> bool:
        set_setting("config.vless_port", "9443")
        return True

    async def forbidden_remote(*_: object, **__: object) -> tuple[int, str]:
        nonlocal remote_calls
        remote_calls += 1
        return 0, ""

    monkeypatch.setattr(ip_change, "tcp_check", mutate_after_main)
    monkeypatch.setattr(ip_change, "run_remote_command", forbidden_remote)

    with pytest.raises(ip_change.IpChangeError, match="bound operation"):
        await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    assert remote_calls == 0
    assert get_setting("current_ip") == OLD_IP
    assert FakeAezaClient.events[-1] == "main:old-id"


@pytest.mark.anyio
async def test_interrupted_external_action_is_never_resent(
    prepared_operation: int,
    timing: IpRotationTiming,
) -> None:
    update_operation(
        prepared_operation,
        status="RUNNING",
        purchase_state="SENDING",
        action_state="PURCHASE_SENDING",
    )

    await run_ip_change(prepared_operation, timing=timing, sleeper=_no_sleep)

    operation = _operation(prepared_operation)
    assert operation["status"] == "AMBIGUOUS"
    assert operation["current_step"] == "manual_verification_required"
    assert FakeAezaClient.events == []
    with pytest.raises(IpChangeSafetyHoldError):
        create_operation()
    with get_db() as db:
        assert db.execute(
            "SELECT COUNT(*) AS count FROM ip_change_operations"
        ).fetchone()["count"] == 1


def _admin_client() -> TestClient:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", main.hash_password("secret"))
    set_setting("config.aeza_token", "token")
    set_setting("config.aeza_service_id", "service-id")
    return TestClient(main.app, base_url="http://panel.local")


def test_feature_flag_off_refuses_old_rotation_without_background_or_aeza(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    monkeypatch.setattr(main, "safe_aeza_ip_rotation_enabled", lambda: False)
    monkeypatch.setattr(main, "create_operation", lambda: pytest.fail("operation created"))

    response = client.post(
        "/admin/ip/refresh",
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ip/confirm?safe_rotation_required=1"


def test_feature_flag_on_schedules_only_the_durable_bound_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    ran: list[int] = []

    async def fake_runner(operation_id: int) -> None:
        ran.append(operation_id)

    monkeypatch.setattr(main, "safe_aeza_ip_rotation_enabled", lambda: True)
    monkeypatch.setattr(main, "transactional_vpn_apply_enabled", lambda: True)
    monkeypatch.setattr(main, "create_operation", lambda: 41)
    monkeypatch.setattr(main, "run_ip_change", fake_runner)

    response = client.post(
        "/admin/ip/refresh",
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin"
    assert ran == [41]


def test_safe_rotation_cannot_bypass_transactional_apply_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    monkeypatch.setattr(main, "safe_aeza_ip_rotation_enabled", lambda: True)
    monkeypatch.setattr(main, "transactional_vpn_apply_enabled", lambda: False)
    monkeypatch.setattr(main, "create_operation", lambda: pytest.fail("operation created"))

    response = client.post(
        "/admin/ip/refresh",
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/admin/ip/confirm?transactional_apply_required=1"
    )


@pytest.mark.parametrize(
    ("path", "data"),
    (
        ("/admin/ip/buy", {}),
        ("/admin/ip/hidden-id/make-main", {"ip": "198.51.100.99"}),
        (
            "/admin/ip/hidden-id/delete",
            {"ip": "198.51.100.99", "is_main": "0"},
        ),
    ),
)
def test_manual_aeza_mutation_urls_are_preserved_but_blocked(
    path: str,
    data: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    create_operation()
    monkeypatch.setattr(
        main,
        "get_aeza_client",
        lambda: pytest.fail("manual route called Aeza"),
    )

    response = client.post(
        path,
        data=data,
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/admin/ip/confirm?manual_disabled=1"


def test_terminal_safety_hold_requires_csrf_and_explicit_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _admin_client()
    operation_id = create_operation()
    update_operation(
        operation_id,
        status="AMBIGUOUS",
        current_step="manual_verification_required",
        action_state="AMBIGUOUS",
        purchase_state="AMBIGUOUS",
    )
    monkeypatch.setattr(
        main,
        "get_aeza_client",
        lambda: pytest.fail("reconciliation called Aeza"),
    )
    monkeypatch.setattr(
        ip_change,
        "run_remote_command",
        lambda *_args, **_kwargs: pytest.fail("reconciliation called SSH"),
    )

    confirm_path = f"/admin/ip/operations/{operation_id}/reconcile/confirm"
    post_path = f"/admin/ip/operations/{operation_id}/reconcile"
    assert client.get(confirm_path).status_code == 401
    confirm_page = client.get(confirm_path, auth=("admin", "secret"))
    assert confirm_page.status_code == 200
    assert "RECONCILE" in confirm_page.text

    cross_origin = client.post(
        post_path,
        data={"confirm": "RECONCILE", "note": "checked in Aeza"},
        auth=("admin", "secret"),
        headers={"Origin": "https://evil.example"},
    )
    assert cross_origin.status_code == 403

    wrong_confirmation = client.post(
        post_path,
        data={"confirm": "wrong", "note": "checked in Aeza"},
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
    )
    assert wrong_confirmation.status_code == 400

    reconciled = client.post(
        post_path,
        data={"confirm": "RECONCILE", "note": "old IP verified in Aeza"},
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )
    assert reconciled.status_code == 303
    assert reconciled.headers["location"] == "/admin/operations#ip-operations"
    operation = _operation(operation_id)
    assert operation["action_state"] == "RECONCILED"
    assert operation["reconciled_at"]
    assert operation["reconciliation_note"] == "old IP verified in Aeza"
    assert create_operation() > operation_id


def test_running_or_non_hold_ip_operation_cannot_be_reconciled() -> None:
    client = _admin_client()
    operation_id = create_operation()

    response = client.post(
        f"/admin/ip/operations/{operation_id}/reconcile",
        data={"confirm": "RECONCILE", "note": "not actually terminal"},
        auth=("admin", "secret"),
        headers={"Origin": "http://panel.local"},
    )

    assert response.status_code == 409
    assert _operation(operation_id)["action_state"] == "NOT_STARTED"
