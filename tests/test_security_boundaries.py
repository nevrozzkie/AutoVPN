from __future__ import annotations

from html.parser import HTMLParser

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.db import create_client, get_db, get_setting, init_db, set_setting
from app.security import hash_password
from app.vpn_state import prepare_install_operation


def _admin_client(*, raise_server_exceptions: bool = True) -> TestClient:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", hash_password("old-password"))
    return TestClient(
        main.app,
        base_url="http://panel.local",
        raise_server_exceptions=raise_server_exceptions,
    )


def _settings_snapshot() -> list[dict[str, str]]:
    with get_db() as connection:
        return connection.execute("SELECT key, value FROM settings ORDER BY key").fetchall()


def _valid_setup_form() -> dict[str, str]:
    return {
        "admin_username_value": "new-admin",
        "admin_password_value": "new-password",
        "admin_password_confirm_value": "new-password",
        "current_ip": "203.0.113.20",
        "eu_ssh_host_value": "ssh.example.test",
        "eu_ssh_user_value": "deploy",
        "eu_ssh_port_value": "2222",
        "eu_ssh_password_value": "new-ssh-secret",
        "eu_ssh_key_path_value": "/new/key",
        "vless_enabled_value": "on",
        "vless_port_value": "443",
        "hysteria_enabled_value": "on",
        "hysteria_port_value": "8443",
        "amnezia_enabled_value": "on",
        "amnezia_port_value": "51820",
        "autovpn2_settings_present": "1",
        "transactional_vpn_apply_enabled_value": "on",
        "safe_aeza_ip_rotation_enabled_value": "on",
        "router_api_enabled_value": "on",
        "aeza_token_value": "new-aeza-secret",
        "aeza_service_id_value": "new-service",
        "aeza_ipv4_domain_value": "new-domain",
    }


@pytest.mark.parametrize(
    "overrides",
    (
        {
            "admin_password_value": "one-password",
            "admin_password_confirm_value": "other-password",
        },
        {"vless_port_value": "not-a-port"},
        {"hysteria_port_value": "443"},
        {"eu_ssh_port_value": "0"},
    ),
)
def test_invalid_setup_does_not_partially_change_any_setting(overrides: dict[str, str]) -> None:
    client = _admin_client()
    set_setting("current_ip", "198.51.100.5")
    set_setting("config.eu_ssh_password", "old-ssh-secret")
    set_setting("config.aeza_token", "old-aeza-secret")
    before = _settings_snapshot()
    form = {**_valid_setup_form(), **overrides}

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _settings_snapshot() == before


def test_setup_preserves_empty_secret_fields_and_current_ip() -> None:
    client = _admin_client()
    set_setting("current_ip", "198.51.100.5")
    set_setting("config.eu_ssh_password", "old-ssh-secret")
    set_setting("config.aeza_token", "old-aeza-secret")
    form = {
        **_valid_setup_form(),
        "admin_password_value": "",
        "admin_password_confirm_value": "",
        "current_ip": "",
        "eu_ssh_password_value": "",
        "eu_ssh_key_path_value": "",
        "aeza_token_value": "",
    }

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("current_ip") == "198.51.100.5"
    assert get_setting("config.eu_ssh_password") == "old-ssh-secret"
    assert get_setting("config.aeza_token") == "old-aeza-secret"


def test_setup_allows_duplicate_port_when_one_protocol_is_disabled() -> None:
    client = _admin_client()
    form = {
        **_valid_setup_form(),
        "hysteria_port_value": "443",
    }
    form.pop("hysteria_enabled_value")

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("config.vless_enabled") == "1"
    assert get_setting("config.hysteria_enabled") == "0"
    assert get_setting("config.vless_port") == "443"
    assert get_setting("config.hysteria_port") == "443"


def test_setup_persists_autovpn_2_feature_switches() -> None:
    client = _admin_client()
    form = _valid_setup_form()
    form.pop("transactional_vpn_apply_enabled_value")
    form.pop("safe_aeza_ip_rotation_enabled_value")
    form.pop("router_api_enabled_value")

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("config.enable_transactional_vpn_apply") == "0"
    assert get_setting("config.enable_safe_aeza_ip_rotation") == "0"
    assert get_setting("config.enable_router_api") == "0"
    assert main.transactional_vpn_apply_enabled() is False
    assert main.safe_aeza_ip_rotation_enabled() is False
    assert main.router_api_enabled() is False
    assert client.get("/api/v2/router/snapshot").status_code == 404


def test_setup_rejects_safe_rotation_without_transactional_apply_atomically() -> None:
    client = _admin_client()
    before = _settings_snapshot()
    form = _valid_setup_form()
    form.pop("transactional_vpn_apply_enabled_value")

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 400
    assert _settings_snapshot() == before


def test_legacy_setup_post_without_autovpn_2_fields_preserves_switches() -> None:
    client = _admin_client()
    set_setting("config.enable_transactional_vpn_apply", "1")
    set_setting("config.enable_safe_aeza_ip_rotation", "1")
    set_setting("config.enable_router_api", "1")
    form = _valid_setup_form()
    form.pop("autovpn2_settings_present")
    form.pop("transactional_vpn_apply_enabled_value")
    form.pop("safe_aeza_ip_rotation_enabled_value")
    form.pop("router_api_enabled_value")

    response = client.post(
        "/admin/setup",
        data=form,
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert get_setting("config.enable_transactional_vpn_apply") == "1"
    assert get_setting("config.enable_safe_aeza_ip_rotation") == "1"
    assert get_setting("config.enable_router_api") == "1"


def test_setup_database_failure_rolls_back_the_whole_settings_update() -> None:
    client = _admin_client(raise_server_exceptions=False)
    before = _settings_snapshot()
    with get_db() as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_setup_update
            BEFORE UPDATE OF value ON settings
            WHEN OLD.key = 'config.admin_password'
            BEGIN
                SELECT RAISE(ABORT, 'simulated settings failure');
            END
            """
        )

    response = client.post(
        "/admin/setup",
        data=_valid_setup_form(),
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert _settings_snapshot() == before


def test_reset_route_atomically_refuses_active_vps_operation() -> None:
    client = _admin_client()
    prepared = prepare_install_operation("203.0.113.10")

    response = client.post(
        "/admin/reset-server",
        auth=("admin", "old-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )

    assert response.status_code == 409
    with get_db() as connection:
        assert connection.execute(
            "SELECT status FROM vpn_install_operations WHERE id = ?",
            (prepared.operation_id,),
        ).fetchone()["status"] == "PENDING"
        assert connection.execute(
            "SELECT owner_id FROM operation_leases WHERE owner_type = 'INSTALL'"
        ).fetchone()["owner_id"] == prepared.operation_id


def test_admin_ip_get_is_read_only_when_aeza_main_ip_differs(monkeypatch) -> None:
    client = _admin_client()
    set_setting("current_ip", "198.51.100.5")
    before = _settings_snapshot()

    class FakeAezaClient:
        async def get_ipv4_list(self, service_id: str) -> list[dict[str, object]]:
            assert service_id == "service-id"
            return [{"id": "new", "ip": "203.0.113.20", "is_main": False}]

        async def get_service(self, service_id: str) -> dict[str, str]:
            assert service_id == "service-id"
            return {"ip": "203.0.113.20"}

    async def fake_price() -> None:
        return None

    monkeypatch.setattr(main, "aeza_ip_rotation_available", lambda: True)
    monkeypatch.setattr(main, "aeza_service_id", lambda: "service-id")
    monkeypatch.setattr(main, "get_aeza_client", FakeAezaClient)
    monkeypatch.setattr(main, "fetch_aeza_ipv4_price", fake_price)

    response = client.get("/admin/ip", auth=("admin", "old-password"))

    assert response.status_code == 200
    assert get_setting("current_ip") == "198.51.100.5"
    assert _settings_snapshot() == before


class _ExecutableSurfaceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._inside_script = False
        self.executable_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._inside_script = True
        self.executable_text.extend(value or "" for name, value in attrs if name.startswith("on"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inside_script = False

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self.executable_text.append(data)


def _assert_not_executable(html: str, marker: str) -> None:
    parser = _ExecutableSurfaceParser()
    parser.feed(html)
    assert marker not in "\n".join(parser.executable_text)


def test_untrusted_client_ssh_aeza_and_error_values_are_not_inline_javascript(
    monkeypatch,
) -> None:
    client = _admin_client()
    marker = "window.__autovpn_xss = true"
    payload = f"</script><script>{marker}</script>\"'"
    create_client(payload)
    set_setting("current_ip", "198.51.100.5")
    set_setting("config.eu_ssh_host", payload)
    set_setting("config.aeza_service_id", payload)
    set_setting("config.aeza_ipv4_domain", payload)
    with get_db() as connection:
        connection.execute(
            """
            INSERT INTO vpn_install_operations(
                status, target_host, current_step, output, error_message, created_at, updated_at
            ) VALUES ('FAILED', ?, 'failed', '', ?, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (payload, f"Host key verification failed: {payload}"),
        )

    class FakeAezaClient:
        async def get_ipv4_list(self, _: str) -> list[dict[str, object]]:
            return [{"id": payload, "ip": payload, "is_main": False}]

        async def get_service(self, _: str) -> dict[str, str]:
            return {}

    async def fake_price() -> None:
        return None

    monkeypatch.setattr(main, "aeza_ip_rotation_available", lambda: True)
    monkeypatch.setattr(main, "get_aeza_client", FakeAezaClient)
    monkeypatch.setattr(main, "fetch_aeza_ipv4_price", fake_price)

    responses = (
        client.get("/admin/clients", auth=("admin", "old-password")),
        client.get("/admin", auth=("admin", "old-password")),
        client.get("/admin/setup", auth=("admin", "old-password")),
        client.get(
            "/admin/ip",
            params={"error": payload},
            auth=("admin", "old-password"),
        ),
    )

    for response in responses:
        assert response.status_code == 200
        assert payload not in response.text
        _assert_not_executable(response.text, marker)
    assert "data-confirm-message=" in responses[0].text
    assert "data-confirm-message=" in responses[1].text


def test_missing_public_token_is_not_echoed_in_error_detail() -> None:
    init_db()
    client = TestClient(main.app)
    token = "do-not-echo-this-secret"

    response = client.get(f"/sub/{token}")

    assert response.status_code == 404
    assert token not in response.text
    assert response.json() == {"detail": "Not Found"}
