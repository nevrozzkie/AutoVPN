from __future__ import annotations

import asyncio
import json
import re
import shlex
import socket
from typing import Any

from app.config import settings
from app.amnezia import build_amnezia_server_config
from app.db import (
    get_setting,
    list_clients,
    update_install_operation,
)
from app.runtime_config import (
    eu_ssh_host,
    eu_ssh_key_path,
    eu_ssh_password,
    eu_ssh_port,
    eu_ssh_user,
    ssh_connect_timeout_seconds,
)


class EuInstallError(RuntimeError):
    pass


def resolve_eu_host() -> str:
    return eu_ssh_host() or get_setting("current_ip")


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    return label.strip("-") or "client"


def xray_client_email(client: dict[str, Any]) -> str:
    return f"{_safe_label(client['name'])}-{client['id']}"


def _enabled_clients() -> list[dict[str, Any]]:
    return [client for client in list_clients() if client["enabled"]]


def build_eu_install_script() -> str:
    clients = _enabled_clients()
    reality_private_key = get_setting("vless.reality_private_key")
    reality_short_id = get_setting("vless.reality_short_id")
    server_private_key = get_setting("amnezia.server_private_key")
    amnezia_obfuscation = {
        "jc": int(get_setting("amnezia.jc", "5")),
        "jmin": int(get_setting("amnezia.jmin", "40")),
        "jmax": int(get_setting("amnezia.jmax", "1000")),
        "s1": int(get_setting("amnezia.s1", "64")),
        "s2": int(get_setting("amnezia.s2", "128")),
        "h1": int(get_setting("amnezia.h1", "1")),
        "h2": int(get_setting("amnezia.h2", "2")),
        "h3": int(get_setting("amnezia.h3", "3")),
        "h4": int(get_setting("amnezia.h4", "4")),
    }
    amnezia_config = build_amnezia_server_config(
        clients,
        server_private_key=server_private_key,
        obfuscation=amnezia_obfuscation,
    )
    xray_clients = [
        {
            "id": client["vless_uuid"],
            "email": xray_client_email(client),
            "flow": "xtls-rprx-vision",
        }
        for client in clients
    ]
    hysteria_users = {
        f"client{client['id']}": client["hysteria_password"]
        for client in clients
    }

    xray_config = {
        "log": {"loglevel": "warning"},
        "api": {
            "tag": "api",
            "services": ["StatsService"],
        },
        "stats": {},
        "policy": {
            "levels": {
                "0": {
                    "statsUserUplink": True,
                    "statsUserDownlink": True,
                }
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            },
        },
        "inbounds": [
            {
                "tag": "api",
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            },
            {
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "port": settings.vless_port,
                "protocol": "vless",
                "settings": {
                    "clients": xray_clients,
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": settings.vless_reality_target,
                        "serverNames": settings.vless_reality_server_names,
                        "privateKey": reality_private_key,
                        "shortIds": [reality_short_id],
                    },
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            }
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "blocked"},
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api"],
                    "outboundTag": "api",
                }
            ]
        },
    }

    hysteria_user_lines = "\n".join(
        f"    client{client['id']}: {json.dumps(client['hysteria_password'])}"
        for client in clients
    )
    if not hysteria_user_lines:
        hysteria_user_lines = "    disabled: \"no-enabled-clients\""

    return f"""#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

echo "[autovpn] installing base packages"
apt-get update
apt-get install -y ca-certificates curl gnupg iptables openssl software-properties-common unzip

echo "[autovpn] installing xray"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" -- install

echo "[autovpn] installing hysteria2"
bash -c "$(curl -fsSL https://get.hy2.sh/)"

echo "[autovpn] installing amneziawg"
add-apt-repository -y ppa:amnezia/ppa
apt-get update
apt-get install -y amneziawg

echo "[autovpn] writing configs"
install -d -m 0755 /etc/autovpn /usr/local/etc/xray /etc/hysteria /etc/amnezia/amneziawg

cat >/usr/local/etc/xray/config.json <<'JSON'
{json.dumps(xray_config, indent=2, ensure_ascii=False)}
JSON

if [ ! -f /etc/autovpn/hysteria.key ] || [ ! -f /etc/autovpn/hysteria.crt ]; then
  openssl req -x509 -newkey rsa:2048 -nodes \\
    -keyout /etc/autovpn/hysteria.key \\
    -out /etc/autovpn/hysteria.crt \\
    -days 3650 \\
    -subj "/CN=autovpn-eu"
fi

cat >/etc/hysteria/config.yaml <<'YAML'
listen: :{settings.hysteria_port}

tls:
  cert: /etc/autovpn/hysteria.crt
  key: /etc/autovpn/hysteria.key

auth:
  type: userpass
  userpass:
{hysteria_user_lines}

masquerade:
  type: proxy
  proxy:
    url: https://example.com/
    rewriteHost: true
YAML

cat >/etc/amnezia/amneziawg/awg0.conf <<'AWG'
{amnezia_config}
AWG
chmod 600 /etc/amnezia/amneziawg/awg0.conf

echo "[autovpn] enabling services"
systemctl enable xray hysteria-server awg-quick@awg0
systemctl restart xray
systemctl restart hysteria-server
systemctl restart awg-quick@awg0

echo "[autovpn] status"
systemctl --no-pager --full status xray || true
systemctl --no-pager --full status hysteria-server || true
systemctl --no-pager --full status awg-quick@awg0 || true

echo "[autovpn] done"
"""


def build_ssh_command(host: str) -> list[str]:
    if not host:
        raise EuInstallError("EU SSH host is not configured and current_ip is empty")

    destination = f"{eu_ssh_user()}@{host}"
    command = [
        "ssh",
        "-p",
        str(eu_ssh_port()),
        "-o",
        f"ConnectTimeout={ssh_connect_timeout_seconds()}",
        "-o",
        "StrictHostKeyChecking=accept-new",
    ]
    if eu_ssh_password():
        command.extend(["-o", "PreferredAuthentications=password"])
    elif eu_ssh_key_path():
        command.extend(["-i", eu_ssh_key_path()])
    command.extend([destination, "bash -s"])
    return command


def describe_ssh_command(host: str) -> str:
    command = " ".join(shlex.quote(part) for part in build_ssh_command(host))
    if eu_ssh_password():
        return f"{command}  # password auth via EU_SSH_PASSWORD"
    return command


def _probe_ssh_banner(host: str) -> str:
    timeout = max(1, min(5, ssh_connect_timeout_seconds()))
    with socket.create_connection((host, eu_ssh_port()), timeout=timeout) as sock:
        sock.settimeout(timeout)
        data = sock.recv(256)
    return data.decode("utf-8", errors="replace").strip()


def _format_ssh_error(host: str, exc: Exception) -> str:
    target = f"{eu_ssh_user()}@{host}:{eu_ssh_port()}"
    message = str(exc) or exc.__class__.__name__
    if "Error reading SSH protocol banner" in message or "No existing session" in message:
        hint = (
            f"SSH handshake failed for {target}: TCP connection opened, but the server did not "
            "send a valid SSH banner. Check that this is the VPS public IP, SSH is listening on "
            "this port, firewall allows it, and the VPS is fully booted."
        )
        try:
            banner = _probe_ssh_banner(host)
        except socket.timeout:
            return f"{hint} TCP probe timed out while waiting for SSH banner."
        except EOFError:
            return f"{hint} TCP probe connected, but the server closed the connection immediately."
        except OSError as probe_exc:
            return f"{hint} TCP probe failed: {probe_exc}."
        if banner.startswith("SSH-"):
            return f"{hint} TCP probe saw SSH banner {banner!r}, so retrying later may help."
        if banner:
            return f"{hint} TCP probe received non-SSH data: {banner[:120]!r}."
        return f"{hint} TCP probe received an empty response."
    return f"SSH connection failed for {target}: {message}"


def _ssh_exec(host: str, command: str, stdin_data: str = "") -> tuple[int, str]:
    import paramiko

    if not host:
        raise EuInstallError("EU SSH host is not configured and current_ip is empty")
    password = eu_ssh_password()
    key_path = eu_ssh_key_path()
    if not password and not key_path:
        raise EuInstallError("Configure EU_SSH_PASSWORD or EU_SSH_KEY_PATH")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs: dict[str, Any] = {
        "hostname": host,
        "port": eu_ssh_port(),
        "username": eu_ssh_user(),
        "timeout": ssh_connect_timeout_seconds(),
        "banner_timeout": ssh_connect_timeout_seconds(),
        "auth_timeout": ssh_connect_timeout_seconds(),
        "look_for_keys": False,
        "allow_agent": False,
    }
    if password:
        connect_kwargs["password"] = password
    else:
        connect_kwargs["key_filename"] = key_path

    try:
        banner = _probe_ssh_banner(host)
        if not banner:
            raise EuInstallError(
                f"SSH handshake failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: "
                "TCP connection opened, but the server closed it before sending an SSH banner. "
                "Check that SSH is running on this port and the VPS is fully booted."
            )
        if not banner.startswith("SSH-"):
            raise EuInstallError(
                f"SSH handshake failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: "
                f"port is open, but it does not look like SSH. Received: {banner[:120]!r}"
            )
    except (socket.timeout, TimeoutError) as exc:
        raise EuInstallError(
            f"SSH handshake failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: "
            "TCP connection opened, but no SSH banner arrived before timeout. "
            "Check firewall, SSH service status, and whether the VPS is still booting."
        ) from exc
    except OSError as exc:
        raise EuInstallError(_format_ssh_error(host, exc)) from exc

    try:
        ssh.connect(**connect_kwargs)
        stdin, stdout, stderr = ssh.exec_command(command, get_pty=True)
        if stdin_data:
            stdin.write(stdin_data)
        stdin.channel.shutdown_write()
        output = stdout.read().decode("utf-8", errors="replace")
        error_output = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, output + error_output
    except EuInstallError:
        raise
    except (paramiko.AuthenticationException, paramiko.BadAuthenticationType) as exc:
        raise EuInstallError(
            f"SSH authentication failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: {exc}. "
            "Check SSH username and password/key in /setup."
        ) from exc
    except (paramiko.SSHException, socket.error, TimeoutError, EOFError) as exc:
        raise EuInstallError(_format_ssh_error(host, exc)) from exc
    finally:
        ssh.close()


def _run_script_over_ssh(host: str, script: str) -> tuple[int, str]:
    return _ssh_exec(host, "bash -s", script)


async def run_remote_command(
    host: str,
    command: str,
    stdin_data: str = "",
) -> tuple[int, str]:
    return await asyncio.to_thread(_ssh_exec, host, command, stdin_data)


async def run_eu_install(operation_id: int) -> None:
    host = resolve_eu_host()
    script = build_eu_install_script()

    try:
        update_install_operation(
            operation_id,
            status="RUNNING",
            target_host=host,
            current_step="ssh_connect",
        )
        exit_code, output = await asyncio.to_thread(_run_script_over_ssh, host, script)
        output_tail = output[-12000:]
        if exit_code != 0:
            raise EuInstallError(
                f"SSH install failed with exit code {exit_code}\n{output_tail}"
            )
        update_install_operation(
            operation_id,
            status="DONE",
            current_step="done",
            output=output_tail,
        )
    except Exception as exc:
        update_install_operation(
            operation_id,
            status="FAILED",
            current_step="failed",
            error_message=str(exc)[-12000:],
        )
        raise
