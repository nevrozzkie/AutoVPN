from __future__ import annotations

import json
import shlex
from dataclasses import dataclass

from app.config import settings
from app.db import get_setting, list_clients
from app.runtime_config import hysteria_port, vless_port
from app.subscriptions import hysteria_auth


@dataclass(frozen=True)
class DeepCheckResult:
    verified: bool
    detail: str = ""


async def run_deep_protocol_checks(current_ip: str) -> dict[str, DeepCheckResult]:
    client = _first_enabled_client()
    if not client:
        return {}

    from app.eu_install import resolve_eu_host, run_remote_command

    host = resolve_eu_host()
    if not host:
        return {}

    script = build_deep_check_script(client, current_ip)
    try:
        exit_code, output = await run_remote_command(host, "bash -s", stdin_data=script)
    except Exception:
        return {}
    if exit_code != 0:
        return {
            "vless": DeepCheckResult(False, "deep check failed over SSH"),
            "hysteria_quic": DeepCheckResult(False, "deep check failed over SSH"),
            "hysteria_salamander": DeepCheckResult(False, "deep check failed over SSH"),
        }
    return parse_deep_check_output(output)


def build_deep_check_script(client: dict, current_ip: str) -> str:
    enabled_clients = [item for item in list_clients() if item["enabled"]]
    amnezia_public_keys = [
        item["amnezia_public_key"]
        for item in enabled_clients
        if item.get("amnezia_public_key")
    ]
    current_vless_port = vless_port()
    xray_config = None
    if current_vless_port is not None:
        xray_config = {
            "log": {"loglevel": "warning"},
            "inbounds": [
                {
                    "listen": "127.0.0.1",
                    "port": 19080,
                    "protocol": "socks",
                    "settings": {"auth": "noauth", "udp": True},
                }
            ],
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {
                        "vnext": [
                            {
                                "address": current_ip,
                                "port": current_vless_port,
                                "users": [
                                    {
                                        "id": client["vless_uuid"],
                                        "encryption": "none",
                                        "flow": "xtls-rprx-vision",
                                    }
                                ],
                            }
                        ]
                    },
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {
                            "serverName": settings.vless_reality_server_name,
                            "fingerprint": settings.vless_reality_fingerprint,
                            "publicKey": get_setting("vless.reality_public_key"),
                            "shortId": get_setting("vless.reality_short_id"),
                            "spiderX": settings.vless_reality_spider_x,
                        },
                    },
                }
            ],
        }
    current_hysteria_port = hysteria_port()
    hysteria_obfs_password = get_setting("hysteria.obfs_password")
    hysteria_obfs_yaml = ""
    if hysteria_obfs_password:
        hysteria_obfs_yaml = (
            "obfs:\n"
            "  type: salamander\n"
            "  salamander:\n"
            f"    password: {shlex.quote(hysteria_obfs_password)}\n"
        )

    return f"""#!/usr/bin/env bash
set +e

WORKDIR="$(mktemp -d)"
XRAY_PID=""
HYSTERIA_PID=""

cleanup() {{
  if [ -n "$XRAY_PID" ]; then kill "$XRAY_PID" >/dev/null 2>&1 || true; fi
  if [ -n "$HYSTERIA_PID" ]; then kill "$HYSTERIA_PID" >/dev/null 2>&1 || true; fi
  wait >/dev/null 2>&1 || true
  rm -rf "$WORKDIR"
}}
trap cleanup EXIT

wait_tcp_port() {{
  local port="$1"
  for _ in $(seq 1 30); do
    bash -c ":</dev/tcp/127.0.0.1/$port" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  return 1
}}

check_socks() {{
  local port="$1"
  curl -fsS --max-time 10 --socks5-hostname "127.0.0.1:$port" \\
    https://www.google.com/generate_204 -o /dev/null >/dev/null 2>&1
}}

if [ {shlex.quote("1" if xray_config else "0")} = "1" ] && command -v curl >/dev/null 2>&1 && command -v xray >/dev/null 2>&1; then
  cat >"$WORKDIR/xray.json" <<'JSON'
{json.dumps(xray_config or {}, indent=2, ensure_ascii=False)}
JSON
  xray run -c "$WORKDIR/xray.json" >"$WORKDIR/xray.log" 2>&1 &
  XRAY_PID="$!"
  if wait_tcp_port 19080 && check_socks 19080; then
    echo "VLESS_DEEP=VERIFIED"
  else
    echo "VLESS_DEEP=FAILED"
  fi
else
  echo "VLESS_DEEP=UNAVAILABLE"
fi

if [ {shlex.quote("1" if current_hysteria_port is not None else "0")} = "1" ]; then
  if systemctl is-active --quiet hysteria-server 2>/dev/null; then
    echo "HYSTERIA_SERVICE=active"
  else
    echo "HYSTERIA_SERVICE=inactive"
  fi
else
  echo "HYSTERIA_SERVICE=UNAVAILABLE"
fi

if [ {shlex.quote("1" if current_hysteria_port is not None else "0")} = "1" ] && command -v curl >/dev/null 2>&1 && command -v hysteria >/dev/null 2>&1; then
  cat >"$WORKDIR/hysteria.yaml" <<'YAML'
server: {shlex.quote(f"{current_ip}:{current_hysteria_port}" if current_hysteria_port is not None else "")}
auth: {shlex.quote(hysteria_auth(client))}
{hysteria_obfs_yaml}tls:
  sni: {shlex.quote(settings.vless_reality_server_name)}
  insecure: true
socks5:
  listen: 127.0.0.1:19081
YAML
  hysteria client -c "$WORKDIR/hysteria.yaml" >"$WORKDIR/hysteria.log" 2>&1 &
  HYSTERIA_PID="$!"
  if wait_tcp_port 19081 && check_socks 19081; then
    echo "HYSTERIA_DEEP=VERIFIED"
  else
    echo "HYSTERIA_DEEP=FAILED"
  fi
else
  echo "HYSTERIA_DEEP=UNAVAILABLE"
fi

if command -v awg >/dev/null 2>&1; then
  AMNEZIA_KEYS={shlex.quote(" ".join(amnezia_public_keys))}
  AMNEZIA_VERIFIED=0
  awg show awg0 dump >"$WORKDIR/awg.dump" 2>/dev/null
  while IFS= read -r line; do
    set -- $line
    peer_key="$1"
    for expected_key in $AMNEZIA_KEYS; do
      if [ "$peer_key" = "$expected_key" ]; then
        AMNEZIA_VERIFIED=1
      fi
    done
  done <"$WORKDIR/awg.dump"
  if [ "$AMNEZIA_VERIFIED" = "1" ]; then
    echo "AMNEZIA_DEEP=VERIFIED"
  else
    echo "AMNEZIA_DEEP=FAILED"
  fi
else
  echo "AMNEZIA_DEEP=UNAVAILABLE"
fi
"""


def parse_deep_check_output(output: str) -> dict[str, DeepCheckResult]:
    results: dict[str, DeepCheckResult] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line == "VLESS_DEEP=VERIFIED":
            results["vless"] = DeepCheckResult(True)
        elif line == "VLESS_DEEP=FAILED":
            results["vless"] = DeepCheckResult(False, "request through VLESS failed")
        elif line == "HYSTERIA_SERVICE=active":
            results["hysteria_quic"] = DeepCheckResult(True)
        elif line == "HYSTERIA_SERVICE=inactive":
            results["hysteria_quic"] = DeepCheckResult(False, "hysteria-server is not active")
        elif line == "HYSTERIA_DEEP=VERIFIED":
            results["hysteria_salamander"] = DeepCheckResult(True)
        elif line == "HYSTERIA_DEEP=FAILED":
            results["hysteria_salamander"] = DeepCheckResult(
                False, "Hysteria tunnel handshake failed"
            )
        elif line == "AMNEZIA_DEEP=VERIFIED":
            results["amnezia"] = DeepCheckResult(True)
        elif line == "AMNEZIA_DEEP=FAILED":
            results["amnezia"] = DeepCheckResult(False, "recent AmneziaWG handshake not found")
    return results


def _first_enabled_client() -> dict | None:
    for client in list_clients():
        if client["enabled"]:
            return client
    return None
