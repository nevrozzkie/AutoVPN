from __future__ import annotations

import asyncio
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient

import app.sprintbox as api
import app.sprintbox_rotation as rotation
from app import main
from app.db import create_client, create_operation, get_operation, get_setting, init_db, set_setting, update_operation
from app.ip_change import IpRotationTiming, run_ip_change
from app.vpn_state import IpChangeSafetyHoldError
from app.vpn_state import prepare_ip_change_operation
from app.sprintbox_review import prepare_cleanup, pending_review


TIMING = IpRotationTiming(.1, 120, .1, .05, .1, .05, .05, .1)
OLD = "203.0.113.10"
NEW = "203.0.113.20"


def test_api_form_auth_and_async_task_contract(monkeypatch):
    requests = []
    def respond(request):
        requests.append(request)
        assert request.headers["AUTH-LOGIN"] == "account-login"
        assert request.headers["AUTH-KEY"] == "secret-key"
        assert request.url.host == "api.from.sh"
        return httpx.Response(200, json={"data": {"id": "task-123"}, "error": None})
    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    client = api.SprintboxClient("account-login", "secret-key")
    assert asyncio.run(client.create_box("tariff", "ubuntu2404", "ssh-ed25519 AAAA", "marker")) == "task-123"
    assert parse_qs(requests[0].content.decode()) == {
        "service": ["tariff"], "distributive": ["ubuntu2404"], "arch": ["x64"],
        "pubkey": ["ssh-ed25519 AAAA"], "box_desc": ["marker"],
    }
    assert asyncio.run(client.delete_box("box-old")) == "task-123"
    assert requests[1].url.path == "/sprintbox/box/delete"
    assert parse_qs(requests[1].content.decode()) == {"box_name": ["box-old"]}


@pytest.mark.parametrize("status,body,error", [
    (400, {"data": None, "error": {"code": 41004, "description": "refused secret-key"}}, "41004"),
    (200, {"data": {"name": "box-123"}, "error": None}, "task ID"),
    (502, "not-json secret-key", "invalid response"),
    (302, {"data": None, "error": None}, "HTTP 302"),
])
def test_api_rejections_and_invalid_responses_do_not_retry_or_leak(monkeypatch, status, body, error):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, json=body) if isinstance(body, dict) else httpx.Response(status, text=body)
    original = httpx.AsyncClient
    monkeypatch.setattr(api.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(RuntimeError, match=error) as caught:
        asyncio.run(api.SprintboxClient("account", "secret-key").delete_box("box-old"))
    assert "secret-key" not in str(caught.value)
    assert len(calls) == 1


class FakeSprintbox:
    def __init__(self, *args, **kwargs):
        self.events = []
        self.marker = ""
        self.deleted = False
        self.fail = ""
        self.boxes = {"box-old": {"name": "box-old", "ipv4": [OLD], "state_description": "running"}}
        self.deleted_names = []
        self.created_count = 0

    async def get_box(self, name):
        self.events.append(f"get:{name}")
        return self.boxes[name]

    async def list_boxes(self):
        boxes = dict(self.boxes)
        if self.marker:
            if self.fail == "multiple":
                boxes["box-other"] = {"name": "box-other", "description": self.marker, "ipv4": ["203.0.113.30"]}
        return boxes

    async def create_box(self, service, distributive, pubkey, description):
        self.events.append("create")
        if self.fail == "create-rejected":
            raise api.SprintboxApiError("41002", "insufficient balance")
        if self.fail == "create-timeout":
            raise TimeoutError("secret-key account-login")
        self.marker = description
        self.created_count += 1
        name = "box-new" if self.created_count == 1 else f"box-new{self.created_count}"
        ip = NEW if self.created_count == 1 else f"203.0.113.{20 + self.created_count}"
        self.boxes[name] = {"name": name, "description": self.marker, "ipv4": [ip], "state_description": "running"}
        return "create-task"

    async def task_status(self, task):
        self.events.append(f"poll:{task}")
        if self.fail == "task-timeout":
            return "started"
        if self.fail == "delete-task-error" and task == "delete-task":
            raise api.SprintboxApiError("TASK_FAILED", "failed")
        return "finished"

    async def delete_box(self, name):
        assert name != get_setting("config.sprintbox_box_name")
        assert get_setting("current_ip") not in self.boxes[name]["ipv4"]
        self.events.append("delete")
        if self.fail == "delete-rejected":
            raise api.SprintboxApiError("41004", "Box can not delete today")
        if self.fail == "delete-timeout":
            raise TimeoutError("response lost secret-key")
        self.deleted = True
        self.deleted_names.append(name)
        del self.boxes[name]
        return "delete-task"


@pytest.fixture
def scenario(monkeypatch):
    init_db()
    for key, value in {
        "vps_provider": "sprintbox", "sprintbox_login": "account-login", "sprintbox_key": "secret-key",
        "sprintbox_box_name": "box-old", "sprintbox_service": "tariff", "sprintbox_distributive": "ubuntu2404",
        "eu_ssh_host": OLD, "eu_ssh_user": "root", "eu_ssh_port": "22", "vless_enabled": "1",
        "hysteria_enabled": "0", "amnezia_enabled": "0",
    }.items():
        set_setting(f"config.{key}", value)
    set_setting("current_ip", OLD)
    create_client("Existing client")
    fake = FakeSprintbox()
    monkeypatch.setattr(rotation, "SprintboxClient", lambda *a, **kw: fake)
    monkeypatch.setattr(rotation, "_public_key", lambda: "ssh-ed25519 AAAA")
    async def ssh(*args):
        fake.events.append("ssh")
    async def deploy(operation, host, config):
        assert config.current_ip != OLD
        assert config.enabled_clients[0].name == "Existing client"
        fake.events.append("deploy")
        if fake.fail == "deploy":
            raise RuntimeError("install failed")
    async def health(host, config, timing):
        fake.events.append("health")
        if fake.fail == "health":
            raise RuntimeError("health failed")
        return '{"vless":"VERIFIED"}'
    monkeypatch.setattr(rotation, "_wait_for_ssh", ssh)
    monkeypatch.setattr(rotation, "_deploy", deploy)
    monkeypatch.setattr(rotation, "_verify_protocol_health", health)
    return fake


def run(operation):
    async def no_sleep(_):
        pass
    asyncio.run(run_ip_change(operation, timing=TIMING, sleeper=no_sleep))


def accept(operation):
    async def no_sleep(_):
        pass
    prepare_cleanup(operation)
    asyncio.run(rotation.run_sprintbox_cleanup(operation, timing=TIMING, sleeper=no_sleep))


def test_rotation_publishes_before_deletion_and_waits_for_task(scenario):
    operation = create_operation()
    run(operation)
    result = get_operation(operation)
    assert result["status"] == "DONE" and result["action_state"] == "AWAITING_USER_REVIEW"
    assert "delete" not in scenario.events
    assert get_setting("current_ip") == NEW
    assert get_setting("config.eu_ssh_host") == ""
    accept(operation)
    assert scenario.events.index("health") < scenario.events.index("delete")
    assert get_operation(operation)["action_state"] == "DONE"
    assert "poll:delete-task" in scenario.events
    assert result["old_ip_id"] == "box-old" and result["new_ip_id"] == "box-new"


@pytest.mark.parametrize("failure", ["deploy", "health", "multiple", "task-timeout", "create-timeout"])
def test_failure_preserves_old_publication_and_never_deletes(scenario, failure):
    scenario.fail = failure
    operation = create_operation()
    run(operation)
    assert get_setting("current_ip") == OLD
    assert get_setting("config.sprintbox_box_name") == "box-old"
    assert "delete" not in scenario.events
    result = get_operation(operation)
    assert result["status"] == "AMBIGUOUS"
    assert "secret-key" not in result["error_message"]
    with pytest.raises(IpChangeSafetyHoldError):
        create_operation()


def test_explicit_create_rejection_is_not_ambiguous(scenario):
    scenario.fail = "create-rejected"
    operation = create_operation()
    run(operation)
    assert get_operation(operation)["status"] == "FAILED"
    assert scenario.events.count("create") == 1
    create_operation()  # user may try again after fixing balance


@pytest.mark.parametrize("failure,state", [("delete-rejected", "CLEANUP_REJECTED"), ("delete-timeout", "CLEANUP_AMBIGUOUS"), ("delete-task-error", "CLEANUP_REJECTED")])
def test_cleanup_failure_keeps_new_vpn_and_records_reason(scenario, failure, state):
    scenario.fail = failure
    operation = create_operation()
    run(operation)
    assert "delete" not in scenario.events
    accept(operation)
    result = get_operation(operation)
    assert result["status"] == "DONE" and result["action_state"] == state
    assert get_setting("current_ip") == NEW
    assert "box-old" in result["cleanup_warning"]
    assert "secret-key" not in result["cleanup_warning"]
    assert scenario.events.count("delete") == 1
    run(operation)
    assert scenario.events.count("delete") == 1


def test_awg_requires_manual_handshake_before_old_box_deletion(scenario):
    set_setting("config.amnezia_enabled", "1")
    operation = create_operation()
    run(operation)
    assert get_setting("current_ip") == NEW
    assert get_operation(operation)["action_state"] == "AWAITING_USER_REVIEW"
    assert "delete" not in scenario.events
    accept(operation)
    assert scenario.deleted_names == ["box-old"]


def test_try_another_ip_preserves_all_boxes_until_latest_accept(scenario):
    first = create_operation()
    run(first)
    assert pending_review()["id"] == first
    second = prepare_ip_change_operation(review_operation_id=first).operation_id
    with pytest.raises(ValueError):
        prepare_ip_change_operation(review_operation_id=first)
    run(second)
    assert "delete" not in scenario.events
    assert set(scenario.boxes) == {"box-old", "box-new", "box-new2"}
    assert pending_review()["retained_boxes"] == {"box-old": OLD, "box-new": NEW}
    with pytest.raises(ValueError):
        prepare_cleanup(first)
    scenario.boxes["unrelated"] = {"name": "unrelated", "ipv4": ["198.51.100.5"]}
    accept(second)
    assert set(scenario.deleted_names) == {"box-old", "box-new"}
    assert set(scenario.boxes) == {"box-new2", "unrelated"}
    assert get_setting("config.sprintbox_box_name") == "box-new2"
    with pytest.raises(ValueError):
        prepare_cleanup(second)
    assert pending_review() is None


def test_review_survives_panel_restart_and_ui_has_two_actions(scenario):
    first = create_operation()
    run(first)
    set_setting("config.admin_password", "test-password")
    with TestClient(main.app, base_url="http://panel.local") as client:
        page = client.get("/admin", auth=("admin", "test-password"))
        assert page.status_code == 200
        assert f'/admin/sprintbox/{first}/accept' in page.text
        assert f'/admin/sprintbox/{first}/retry' in page.text
        assert "Всё ок" in page.text and "Попробовать другой IP" in page.text
    assert pending_review()["id"] == first
    assert "delete" not in scenario.events


def test_accept_endpoint_requires_auth_and_is_not_replayed(scenario):
    first = create_operation()
    run(first)
    set_setting("config.admin_password", "test-password")
    with TestClient(main.app, base_url="http://panel.local") as client:
        url = f"/admin/sprintbox/{first}/accept"
        assert client.post(url, headers={"Origin": "http://panel.local"}).status_code == 401
        assert client.post(url, auth=("admin", "test-password"), headers={"Origin": "http://evil.local"}).status_code == 403
        response = client.post(url, auth=("admin", "test-password"), headers={"Origin": "http://panel.local"}, follow_redirects=False)
        assert response.status_code == 303
        assert scenario.deleted_names == ["box-old"]
        assert client.post(url, auth=("admin", "test-password"), headers={"Origin": "http://panel.local"}).status_code == 409
        assert scenario.deleted_names == ["box-old"]


def test_cleanup_continues_after_one_box_is_rejected(scenario, monkeypatch):
    first = create_operation()
    run(first)
    second = prepare_ip_change_operation(review_operation_id=first).operation_id
    run(second)
    original = scenario.delete_box
    async def delete(name):
        if name == "box-old":
            raise api.SprintboxApiError("41004", "cannot delete yet")
        return await original(name)
    monkeypatch.setattr(scenario, "delete_box", delete)
    accept(second)
    assert scenario.deleted_names == ["box-new"]
    assert set(scenario.boxes) == {"box-old", "box-new2"}
    result = get_operation(second)
    assert result["action_state"] == "CLEANUP_REJECTED"
    assert "вручную" in result["cleanup_warning"] and "box-old" in result["cleanup_warning"]


def test_changed_provider_settings_prevent_purchase(scenario):
    operation = create_operation()
    set_setting("config.sprintbox_service", "different-paid-tariff")
    run(operation)
    assert scenario.events == []
    assert get_operation(operation)["status"] == "FAILED"


def test_interrupted_operation_does_not_repeat_creation(scenario):
    operation = create_operation()
    update_operation(operation, purchase_state="SENDING")
    run(operation)
    assert scenario.events == []
    assert get_operation(operation)["status"] == "AMBIGUOUS"


def test_wrong_old_box_never_creates_or_deletes(scenario):
    set_setting("current_ip", "198.51.100.99")
    set_setting("config.eu_ssh_host", "")
    operation = create_operation()
    run(operation)
    assert "create" not in scenario.events and "delete" not in scenario.events
    assert get_operation(operation)["status"] == "FAILED"


def test_public_key_is_derived_without_sending_private_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "private_key"
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption()))
    monkeypatch.setattr(rotation, "eu_ssh_key_path", lambda: str(path))
    monkeypatch.setattr(rotation, "eu_ssh_user", lambda: "root")
    monkeypatch.setattr(rotation, "eu_ssh_port", lambda: 22)
    monkeypatch.setattr(rotation, "eu_ssh_password", lambda: "")
    public = rotation._public_key()
    assert public == key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    assert "PRIVATE" not in public


def test_fresh_box_does_not_use_old_password(monkeypatch):
    monkeypatch.setattr(rotation, "eu_ssh_user", lambda: "root")
    monkeypatch.setattr(rotation, "eu_ssh_port", lambda: 22)
    monkeypatch.setattr(rotation, "eu_ssh_password", lambda: "old-password")
    with pytest.raises(RuntimeError, match="no SSH password"):
        rotation._public_key()


def test_provider_setup_cannot_change_during_rotation(scenario):
    from app.db import set_settings
    from app.operation_coordinator import OperationBusyError
    create_operation()
    with pytest.raises(OperationBusyError):
        set_settings({"config.vps_provider": "aeza"}, require_vps_idle=True)
    assert get_setting("config.vps_provider") == "sprintbox"


def test_sprintbox_reboot_waits_for_async_task(scenario, monkeypatch):
    import app.server_operations as server
    fake = scenario
    polls = iter(["started", "finished"])
    async def reboot(name):
        assert name == "box-old"
        fake.events.append("reboot")
        return "reboot-task"
    async def task_status(task):
        assert task == "reboot-task"
        fake.events.append("poll")
        return next(polls)
    async def get_service(name):
        fake.events.append("status")
        return {"ip": OLD, "provider_status": "running"}
    async def tcp(*args, **kwargs):
        return True
    async def health(*args, **kwargs):
        fake.events.append("health")
        return {}, {}, ""
    async def no_sleep(_):
        pass
    monkeypatch.setattr(fake, "reboot_service", reboot, raising=False)
    monkeypatch.setattr(fake, "task_status", task_status)
    monkeypatch.setattr(fake, "get_service", get_service, raising=False)
    monkeypatch.setattr(server, "_provider_client", lambda timeout: fake)
    monkeypatch.setattr(server, "tcp_check", tcp)
    monkeypatch.setattr(server, "_service_and_protocol_health", health)
    operation = server.create_server_operation("REBOOT")
    asyncio.run(server.run_server_reboot(operation, timing=server.ServerTiming(.1, 1, .1, .1, .1), sleeper=no_sleep))
    assert server.get_server_operation(operation)["status"] == "DONE"
    assert fake.events == ["status", "reboot", "poll", "poll", "status", "health"]
    assert get_setting(f"server_operation.{operation}.sprintbox_task") == "reboot-task"


def test_sprintbox_ui_has_no_aeza_price_request_or_key_leak(scenario, monkeypatch):
    main.app.dependency_overrides[main.require_admin] = lambda: "admin"
    async def forbidden_price(*args):
        pytest.fail("SprintBOX UI contacted Aeza")
    monkeypatch.setattr(main.AezaClient, "get_ipv4_price", forbidden_price)
    try:
        with TestClient(main.app) as client:
            page = client.get("/admin/ip/confirm")
            assert page.status_code == 200 and "Новый бокс SprintBOX" in page.text
            assert "secret-key" not in page.text
            dashboard = client.get("/admin")
            assert dashboard.status_code == 200 and "SprintBOX" in dashboard.text
            assert "secret-key" not in dashboard.text
    finally:
        main.app.dependency_overrides.clear()


def test_setup_saves_sprintbox_parameters_and_preserves_blank_api_key(scenario):
    set_setting("config.admin_password", "test-password")
    form = {
        "vps_provider_value": "sprintbox", "sprintbox_login_value": "account-login",
        "sprintbox_key_value": "", "sprintbox_box_name_value": "box-old",
        "sprintbox_service_value": "new-tariff", "sprintbox_distributive_value": "ubuntu2404",
        "current_ip": OLD, "eu_ssh_user_value": "root", "eu_ssh_port_value": "22",
        "eu_ssh_key_path_value": "/tmp/panel-key", "vless_port_value": "443",
        "hysteria_port_value": "8443", "amnezia_port_value": "51820", "vless_enabled_value": "on",
    }
    with TestClient(main.app, base_url="http://panel.local") as client:
        response = client.post("/admin/setup", data=form, auth=("admin", "test-password"),
                               headers={"Origin": "http://panel.local"}, follow_redirects=False)
        assert response.status_code == 303
        assert get_setting("config.sprintbox_service") == "new-tariff"
        assert get_setting("config.sprintbox_key") == "secret-key"
        page = client.get("/admin/setup", auth=("admin", "test-password"))
        assert page.status_code == 200 and "new-tariff" in page.text
        assert "secret-key" not in page.text
        create_operation()
        form["sprintbox_service_value"] = "different-tariff"
        blocked = client.post("/admin/setup", data=form, auth=("admin", "test-password"),
                              headers={"Origin": "http://panel.local"}, follow_redirects=False)
        assert blocked.status_code == 409
        assert get_setting("config.sprintbox_service") == "new-tariff"
