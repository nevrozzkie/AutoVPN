import pytest

import app.eu_install as eu_install
from app.db import create_client, get_setting, init_db, set_setting
from app.eu_install import (
    build_eu_install_script,
    describe_ssh_command,
    forget_ssh_known_host,
    run_remote_command,
)


def test_build_eu_install_script_contains_enabled_client_credentials() -> None:
    init_db()
    set_setting("config.vless_enabled", "1")
    set_setting("config.vless_port", "443")
    set_setting("config.hysteria_enabled", "1")
    set_setting("config.hysteria_port", "8443")
    set_setting("config.amnezia_enabled", "1")
    set_setting("config.amnezia_port", "51820")
    client = create_client("Alice")

    script = build_eu_install_script()

    assert client["vless_uuid"] in script
    assert f"client{client['id']}" not in script
    assert f"password: {get_setting('hysteria.password')!r}".replace("'", '"') in script
    assert "type: password" in script
    assert "type: salamander" in script
    assert get_setting("hysteria.obfs_password") in script
    assert "userpass:" not in script
    assert "systemctl restart xray" in script
    assert "systemctl restart hysteria-server" in script
    assert '"security": "reality"' in script
    assert '"target": "ok.ru:443"' in script
    assert '"www.ok.ru"' in script
    assert '"flow": "xtls-rprx-vision"' in script
    assert "apt-get install -y amneziawg" in script
    assert "systemctl restart awg-quick@awg0" in script
    assert "chown root:hysteria /etc/autovpn/hysteria.key /etc/autovpn/hysteria.crt" in script
    assert "chmod 640 /etc/autovpn/hysteria.key" in script
    assert "HYSTERIA_SNI=ok.ru" in script
    assert "ufw allow 443/tcp" in script
    assert "iptables -C INPUT -p tcp --dport 443 -j ACCEPT" in script
    assert "firewall-cmd --permanent --add-port=443/tcp" in script
    assert "ufw allow 8443/udp" in script
    assert "iptables -C INPUT -p udp --dport 8443 -j ACCEPT" in script
    assert "firewall-cmd --permanent --add-port=8443/udp" in script
    assert "systemctl is-active --quiet hysteria-server" in script


def test_build_eu_install_script_disables_protocols_with_empty_ports() -> None:
    init_db()
    create_client("Alice")
    set_setting("config.vless_enabled", "0")
    set_setting("config.hysteria_enabled", "0")
    set_setting("config.amnezia_enabled", "0")
    try:
        script = build_eu_install_script()

        assert '"tag": "vless-in"' not in script
        assert "systemctl disable --now hysteria-server" in script
        assert "listen: :8443" not in script
        assert "ufw allow 8443/udp" not in script
        assert "systemctl restart hysteria-server" not in script
        assert "systemctl disable --now awg-quick@awg0" in script
        assert "ListenPort = 51820" not in script
        assert "systemctl restart awg-quick@awg0" not in script
    finally:
        set_setting("config.vless_enabled", "1")
        set_setting("config.vless_port", "443")
        set_setting("config.hysteria_enabled", "0")
        set_setting("config.hysteria_port", "8443")
        set_setting("config.amnezia_enabled", "1")
        set_setting("config.amnezia_port", "51820")


def test_describe_ssh_command_marks_password_auth() -> None:
    from app.config import settings

    init_db()
    original_password = settings.eu_ssh_password
    original_key_path = settings.eu_ssh_key_path
    try:
        object.__setattr__(settings, "eu_ssh_password", "secret")
        object.__setattr__(settings, "eu_ssh_key_path", "/tmp/key")

        command = describe_ssh_command("203.0.113.10")

        assert "PreferredAuthentications=password" in command
        assert "PubkeyAuthentication=no" in command
        assert "NumberOfPasswordPrompts=1" in command
        assert "BatchMode=no" in command
        assert "secret" not in command
        assert "password auth via EU_SSH_PASSWORD" in command
        assert "autovpn-ssh-ok" in command
        assert "bash -s" not in command
    finally:
        object.__setattr__(settings, "eu_ssh_password", original_password)
        object.__setattr__(settings, "eu_ssh_key_path", original_key_path)


def test_forget_ssh_known_host_uses_ssh_keygen(monkeypatch) -> None:
    from app.config import settings
    import app.eu_install as eu_install

    init_db()
    calls: list[list[str]] = []

    class Result:
        stdout = b"removed\n"

    def fake_run(command: list[str], **_: object) -> Result:
        calls.append(command)
        return Result()

    original_port = settings.eu_ssh_port
    try:
        object.__setattr__(settings, "eu_ssh_port", 2222)
        monkeypatch.setattr(eu_install.subprocess, "run", fake_run)

        output = forget_ssh_known_host("203.0.113.10")

        assert "removed" in output
        assert calls == [
            ["ssh-keygen", "-R", "203.0.113.10"],
            ["ssh-keygen", "-R", "[203.0.113.10]:2222"],
        ]
    finally:
        object.__setattr__(settings, "eu_ssh_port", original_port)


@pytest.mark.anyio
async def test_run_remote_command_passes_bounded_timeout_to_ssh_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_ssh_exec(
        host: str,
        command: str,
        stdin_data: str = "",
        command_timeout: float | None = None,
    ) -> tuple[int, str]:
        captured.update(
            host=host,
            command=command,
            stdin_data=stdin_data,
            command_timeout=command_timeout,
        )
        return 0, "ok"

    monkeypatch.setattr(eu_install, "_ssh_exec", fake_ssh_exec)

    assert await run_remote_command("203.0.113.10", "true", timeout=2.5) == (0, "ok")
    assert captured == {
        "host": "203.0.113.10",
        "command": "true",
        "stdin_data": "",
        "command_timeout": 2.5,
    }


@pytest.mark.anyio
async def test_run_remote_command_has_bounded_default_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    captured: list[float | None] = []

    def fake_ssh_exec(
        host: str,
        command: str,
        stdin_data: str = "",
        command_timeout: float | None = None,
    ) -> tuple[int, str]:
        captured.append(command_timeout)
        return 0, "ok"

    monkeypatch.setattr(eu_install, "_ssh_exec", fake_ssh_exec)

    assert await run_remote_command("203.0.113.10", "true") == (0, "ok")
    assert captured and captured[0] is not None and captured[0] > 0


def test_deploy_script_forwards_explicit_timeout_to_ssh_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_ssh_exec(
        host: str,
        command: str,
        stdin_data: str = "",
        command_timeout: float | None = None,
    ) -> tuple[int, str]:
        captured.update(
            host=host,
            command=command,
            stdin_data=stdin_data,
            command_timeout=command_timeout,
        )
        return 0, "ok"

    monkeypatch.setattr(eu_install, "_ssh_exec", fake_ssh_exec)

    assert eu_install._run_script_over_ssh(
        "203.0.113.10", "deploy-script", 123.0
    ) == (0, "ok")
    assert captured == {
        "host": "203.0.113.10",
        "command": "bash -s",
        "stdin_data": "deploy-script",
        "command_timeout": 123.0,
    }
