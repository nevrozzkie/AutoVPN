from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import socket
import subprocess
import tempfile
import time
from typing import Any

from app.config import settings
from app.amnezia import build_amnezia_server_config
from app.db import (
    get_setting,
    list_clients,
    update_install_operation,
)
from app.runtime_config import (
    amnezia_port,
    eu_ssh_host,
    eu_ssh_key_path,
    eu_ssh_password,
    eu_ssh_port,
    eu_ssh_user,
    hysteria_port,
    ssh_connect_timeout_seconds,
    vless_port,
)
from app.protocol_status import refresh_protocol_statuses


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
    current_vless_port = vless_port()
    current_hysteria_port = hysteria_port()
    current_amnezia_port = amnezia_port()
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
    amnezia_config = ""
    if current_amnezia_port is not None:
        amnezia_config = build_amnezia_server_config(
            clients,
            server_private_key=server_private_key,
            obfuscation=amnezia_obfuscation,
            listen_port=current_amnezia_port,
        )
    xray_clients = [
        {
            "id": client["vless_uuid"],
            "email": xray_client_email(client),
            "flow": "xtls-rprx-vision",
        }
        for client in clients
    ]
    hysteria_password = get_setting("hysteria.password")

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
    if current_vless_port is not None:
        xray_config["inbounds"].append(
            {
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "port": current_vless_port,
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
        )
    if current_hysteria_port is not None:
        hysteria_config_script = f"""
HYSTERIA_SNI={shlex.quote(settings.vless_reality_server_name)}
if [ ! -f /etc/autovpn/hysteria.key ] || [ ! -f /etc/autovpn/hysteria.crt ] || ! openssl x509 -in /etc/autovpn/hysteria.crt -noout -subject | grep -Eq "CN ?= ?${{HYSTERIA_SNI}}"; then
  rm -f /etc/autovpn/hysteria.key /etc/autovpn/hysteria.crt
  openssl req -x509 -newkey rsa:2048 -nodes \\
    -keyout /etc/autovpn/hysteria.key \\
    -out /etc/autovpn/hysteria.crt \\
    -days 3650 \\
    -subj "/CN=${{HYSTERIA_SNI}}"
fi
chmod 644 /etc/autovpn/hysteria.crt
if id hysteria >/dev/null 2>&1; then
  chown root:hysteria /etc/autovpn/hysteria.key /etc/autovpn/hysteria.crt
  chmod 640 /etc/autovpn/hysteria.key
else
  chmod 644 /etc/autovpn/hysteria.key
fi

cat >/etc/hysteria/config.yaml <<'YAML'
listen: :{current_hysteria_port}

tls:
  cert: /etc/autovpn/hysteria.crt
  key: /etc/autovpn/hysteria.key

auth:
  type: password
  password: {json.dumps(hysteria_password)}

masquerade:
  type: proxy
  proxy:
    url: https://example.com/
    rewriteHost: true
YAML
"""
        hysteria_firewall_script = f"""
if command -v ufw >/dev/null 2>&1; then
  ufw allow {current_hysteria_port}/udp || true
fi
if command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p udp --dport {current_hysteria_port} -j ACCEPT 2>/dev/null || iptables -I INPUT -p udp --dport {current_hysteria_port} -j ACCEPT || true
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port={current_hysteria_port}/udp || true
  firewall-cmd --reload || true
fi
if command -v nft >/dev/null 2>&1; then
  nft list ruleset | grep -q "udp dport {current_hysteria_port} accept" || nft add rule inet filter input udp dport {current_hysteria_port} accept || true
fi
"""
        hysteria_service_script = """
systemctl enable hysteria-server
systemctl restart hysteria-server
"""
        hysteria_check_script = """
systemctl is-active --quiet hysteria-server
"""
        hysteria_status_script = """
systemctl --no-pager --full status hysteria-server || true
"""
    else:
        hysteria_config_script = """
rm -f /etc/hysteria/config.yaml
systemctl disable --now hysteria-server >/dev/null 2>&1 || true
"""
        hysteria_firewall_script = ""
        hysteria_service_script = ""
        hysteria_check_script = ""
        hysteria_status_script = ""

    if current_amnezia_port is not None:
        amnezia_config_script = f"""
cat >/etc/amnezia/amneziawg/awg0.conf <<'AWG'
{amnezia_config}
AWG
chmod 600 /etc/amnezia/amneziawg/awg0.conf
"""
        amnezia_service_script = """
if command -v awg-quick >/dev/null 2>&1; then
  systemctl enable awg-quick@awg0
  systemctl restart awg-quick@awg0
else
  echo "[autovpn] WARNING: awg-quick is unavailable; AmneziaWG service was not started."
fi
"""
        amnezia_check_script = """
if command -v awg-quick >/dev/null 2>&1; then
  systemctl is-active --quiet awg-quick@awg0
fi
"""
        amnezia_status_script = """
if command -v awg-quick >/dev/null 2>&1; then
  systemctl --no-pager --full status awg-quick@awg0 || true
fi
"""
    else:
        amnezia_config_script = """
rm -f /etc/amnezia/amneziawg/awg0.conf
systemctl disable --now awg-quick@awg0 >/dev/null 2>&1 || true
"""
        amnezia_service_script = ""
        amnezia_check_script = ""
        amnezia_status_script = ""

    return f"""#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

remove_amnezia_apt_sources() {{
  find /etc/apt/sources.list.d -maxdepth 1 -type f \\( -iname '*amnezia*' -o -iname '*ppa_amnezia_ppa*' \\) -delete 2>/dev/null || true
}}

install_amneziawg_best_effort() {{
  AMNEZIAWG_INSTALLED=0
  remove_amnezia_apt_sources
  if apt-get update && apt-cache show amneziawg >/dev/null 2>&1; then
    if apt-get install -y amneziawg; then
      AMNEZIAWG_INSTALLED=1
      return 0
    fi
  fi

  local codename=""
  if [ -r /etc/os-release ]; then
    codename="$(. /etc/os-release && printf "%s" "${{UBUNTU_CODENAME:-${{VERSION_CODENAME:-}}}}")"
  fi

  case "$codename" in
    focal|jammy|noble)
      add-apt-repository -y ppa:amnezia/ppa || true
      if apt-get update && apt-cache show amneziawg >/dev/null 2>&1; then
        if apt-get install -y amneziawg; then
          AMNEZIAWG_INSTALLED=1
          return 0
        fi
      fi
      ;;
    *)
      echo "[autovpn] WARNING: AmneziaWG PPA is not enabled for Ubuntu codename '$codename'. Skipping AmneziaWG install."
      ;;
  esac

  remove_amnezia_apt_sources
  apt-get update || true
  echo "[autovpn] WARNING: AmneziaWG was not installed. VLESS and Hysteria will still be configured."
  return 0
}}

echo "[autovpn] installing base packages"
remove_amnezia_apt_sources
apt-get update
apt-get install -y ca-certificates curl gnupg iptables openssl software-properties-common unzip

echo "[autovpn] installing xray"
bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" -- install

echo "[autovpn] installing hysteria2"
bash -c "$(curl -fsSL https://get.hy2.sh/)"

echo "[autovpn] installing amneziawg"
install_amneziawg_best_effort

echo "[autovpn] writing configs"
install -d -m 0755 /etc/autovpn /usr/local/etc/xray /etc/hysteria /etc/amnezia/amneziawg

cat >/usr/local/etc/xray/config.json <<'JSON'
{json.dumps(xray_config, indent=2, ensure_ascii=False)}
JSON

{hysteria_config_script}
{amnezia_config_script}

echo "[autovpn] opening firewall ports"
{hysteria_firewall_script}

echo "[autovpn] enabling services"
systemctl enable xray
systemctl restart xray
{hysteria_service_script}
{amnezia_service_script}
sleep 1

echo "[autovpn] checking services"
systemctl is-active --quiet xray
{hysteria_check_script}
{amnezia_check_script}

echo "[autovpn] status"
systemctl --no-pager --full status xray || true
{hysteria_status_script}
{amnezia_status_script}

echo "[autovpn] done"
"""


def build_ssh_command(host: str, remote_command: str = "printf 'autovpn-ssh-ok\\n' && uname -a") -> list[str]:
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
        command.extend(
            [
                "-o",
                "PreferredAuthentications=password",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
                "-o",
                "BatchMode=no",
            ]
        )
    elif eu_ssh_key_path():
        command.extend(["-i", eu_ssh_key_path()])
    command.extend([destination, remote_command])
    return command


def describe_ssh_command(host: str) -> str:
    command = " ".join(shlex.quote(part) for part in build_ssh_command(host))
    if eu_ssh_password():
        return f"{command}  # password auth via EU_SSH_PASSWORD"
    return command


def forget_ssh_known_host(host: str) -> str:
    if not host:
        raise EuInstallError("EU SSH host is not configured and current_ip is empty")

    targets = [host, f"[{host}]:{eu_ssh_port()}"]
    outputs: list[str] = []
    for target in dict.fromkeys(targets):
        try:
            result = subprocess.run(
                ["ssh-keygen", "-R", target],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise EuInstallError("ssh-keygen is not installed on this machine") from exc
        output = result.stdout.decode("utf-8", errors="replace").strip()
        if output:
            outputs.append(output)
    return "\n".join(outputs) or f"No known_hosts entries found for {host}"


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
        hint = f"SSH handshake failed for {target}: Paramiko could not complete the SSH handshake."
        try:
            banner = _probe_ssh_banner(host)
        except socket.timeout:
            return f"{hint} TCP probe timed out while waiting for SSH banner. Check firewall and SSH service status."
        except EOFError:
            return f"{hint} TCP probe connected, but the server closed the connection immediately."
        except OSError as probe_exc:
            return f"{hint} TCP probe failed: {probe_exc}."
        if banner.startswith("SSH-"):
            return (
                f"{hint} TCP probe saw SSH banner {banner!r}. "
                "The port is SSH, so this is likely a transient server-side drop/rate-limit or Paramiko compatibility issue. "
                "Retry in a minute; if it repeats, try SSH key auth or verify password login with ssh -vvv."
            )
        if banner:
            return f"{hint} TCP probe received non-SSH data: {banner[:120]!r}."
        return f"{hint} TCP probe received an empty response."
    return f"SSH connection failed for {target}: {message}"


def _redact_password(text: str, password: str) -> str:
    if not password:
        return text
    return text.replace(password, "***")


def _ssh_exec_system_password(host: str, command: str, stdin_data: str = "") -> tuple[int, str]:
    password = eu_ssh_password()
    if not password:
        raise EuInstallError("Configure EU_SSH_PASSWORD")

    askpass_path = ""
    try:
        with tempfile.NamedTemporaryFile("w", delete=False, prefix="autovpn-askpass-", dir=tempfile.gettempdir()) as file:
            askpass_path = file.name
            file.write("#!/bin/sh\n")
            file.write("printf '%s\\n' \"$AUTOVPN_SSH_PASSWORD\"\n")
        os.chmod(askpass_path, 0o700)

        env = os.environ.copy()
        env.update(
            {
                "AUTOVPN_SSH_PASSWORD": password,
                "SSH_ASKPASS": askpass_path,
                "SSH_ASKPASS_REQUIRE": "force",
                "DISPLAY": env.get("DISPLAY") or "autovpn:0",
            }
        )
        result = subprocess.run(
            build_ssh_command(host, command),
            input=stdin_data.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
            check=False,
        )
        output = result.stdout.decode("utf-8", errors="replace")
        return result.returncode, _redact_password(output, password)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EuInstallError(f"OpenSSH password session failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: {exc}") from exc
    finally:
        if askpass_path:
            try:
                os.unlink(askpass_path)
            except OSError:
                pass


def _ssh_exec(host: str, command: str, stdin_data: str = "") -> tuple[int, str]:
    import paramiko

    if not host:
        raise EuInstallError("EU SSH host is not configured and current_ip is empty")
    password = eu_ssh_password()
    key_path = eu_ssh_key_path()
    if not password and not key_path:
        raise EuInstallError("Configure EU_SSH_PASSWORD or EU_SSH_KEY_PATH")

    if password and os.name != "nt":
        return _ssh_exec_system_password(host, command, stdin_data)

    connect_kwargs: dict[str, Any] = {
        "hostname": host,
        "port": eu_ssh_port(),
        "username": eu_ssh_user(),
        "timeout": ssh_connect_timeout_seconds(),
        "banner_timeout": max(30, ssh_connect_timeout_seconds()),
        "auth_timeout": max(30, ssh_connect_timeout_seconds()),
        "look_for_keys": False,
        "allow_agent": False,
    }
    if password:
        connect_kwargs["password"] = password
    else:
        connect_kwargs["key_filename"] = key_path

    last_exc: Exception | None = None
    for attempt in range(1, 4):
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
        except (paramiko.AuthenticationException, paramiko.BadAuthenticationType) as exc:
            raise EuInstallError(
                f"SSH authentication failed for {eu_ssh_user()}@{host}:{eu_ssh_port()}: {exc}. "
                "Check SSH username and password/key in /setup."
            ) from exc
        except (paramiko.SSHException, socket.error, TimeoutError, EOFError) as exc:
            last_exc = exc
            message = str(exc)
            transient_banner_error = (
                "Error reading SSH protocol banner" in message
                or "No existing session" in message
            )
            if transient_banner_error and attempt < 3:
                time.sleep(1.5 * attempt)
                continue
            raise EuInstallError(_format_ssh_error(host, exc)) from exc
        finally:
            ssh.close()

    raise EuInstallError(_format_ssh_error(host, last_exc or RuntimeError("unknown SSH error")))


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
        update_install_operation(
            operation_id,
            current_step="remote_install_running",
            output="Remote install started. It can take several minutes while apt, xray, hysteria and amneziawg are installed.",
        )
        exit_code, output = await asyncio.to_thread(_run_script_over_ssh, host, script)
        output_tail = output[-12000:]
        if exit_code != 0:
            raise EuInstallError(
                f"SSH install failed with exit code {exit_code}\n{output_tail}"
            )
        await refresh_protocol_statuses(host)
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
