import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import create_client, init_db
from app.eu_install import build_eu_install_script, describe_ssh_command, forget_ssh_known_host


def test_build_eu_install_script_contains_enabled_client_credentials() -> None:
    init_db()
    client = create_client("Alice")

    script = build_eu_install_script()

    assert client["vless_uuid"] in script
    assert f"client{client['id']}" in script
    assert client["hysteria_password"] in script
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
    assert "ufw allow 8443/udp" in script
    assert "iptables -C INPUT -p udp --dport 8443 -j ACCEPT" in script
    assert "systemctl is-active --quiet hysteria-server" in script


def test_describe_ssh_command_marks_password_auth() -> None:
    from app.config import settings

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
