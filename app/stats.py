from __future__ import annotations

import json
import re
from typing import Any

from app.db import get_client_stats, list_clients, now_iso, set_setting, upsert_client_stats
from app.eu_install import resolve_eu_host, run_remote_command, xray_client_email
from app.runtime_config import (
    aeza_token,
    eu_ssh_password,
    server_command_timeout_seconds,
)
from app.secret_sanitization import sanitize_error


XRAY_STATS_COMMAND = (
    "/usr/local/bin/xray api statsquery "
    "--server=127.0.0.1:10085 "
    "-pattern 'user>>>'"
)
AMNEZIA_STATS_COMMAND = "awg show awg0 dump"


def parse_xray_stats_output(output: str) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    pattern = re.compile(
        r'name:\s*"user>>>(?P<email>[^"]+)>>>traffic>>>(?P<direction>uplink|downlink)"'
        r"\s+value:\s*(?P<value>\d+)",
        re.MULTILINE,
    )
    for match in pattern.finditer(output):
        email = match.group("email")
        direction = match.group("direction")
        value = int(match.group("value"))
        stats.setdefault(email, {"uplink": 0, "downlink": 0})[direction] = value
    return stats


def parse_amnezia_dump_output(output: str) -> dict[str, dict[str, int]]:
    stats: dict[str, dict[str, int]] = {}
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) < 8:
            continue
        public_key = parts[0]
        if public_key == "private-key":
            continue
        try:
            latest_handshake = int(parts[4])
            transfer_rx = int(parts[5])
            transfer_tx = int(parts[6])
        except ValueError:
            continue
        stats[public_key] = {
            "rx": transfer_rx,
            "tx": transfer_tx,
            "latest_handshake": latest_handshake,
        }
    return stats


def format_bytes(value: int | None) -> str:
    size = float(value or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


async def refresh_client_stats() -> dict[str, Any]:
    host = resolve_eu_host()
    if not host:
        raise RuntimeError("EU SSH host is not configured and current_ip is empty")

    errors: list[str] = []

    command_timeout = float(server_command_timeout_seconds())
    xray_parsed: dict[str, dict[str, int]] = {}
    try:
        xray_exit_code, xray_output = await run_remote_command(
            host, XRAY_STATS_COMMAND, timeout=command_timeout
        )
    except Exception as exc:
        xray_exit_code, xray_output = -1, ""
        errors.append(
            f"Xray stats failed: {sanitize_error(exc, aeza_token(), eu_ssh_password())}"
        )
    if xray_exit_code == 0:
        xray_parsed = parse_xray_stats_output(xray_output)
    elif xray_output:
        errors.append(
            "Xray stats failed with exit code "
            f"{xray_exit_code}: "
            f"{sanitize_error(xray_output[-2000:], aeza_token(), eu_ssh_password())}"
        )

    amnezia_parsed: dict[str, dict[str, int]] = {}
    try:
        amnezia_exit_code, amnezia_output = await run_remote_command(
            host, AMNEZIA_STATS_COMMAND, timeout=command_timeout
        )
    except Exception as exc:
        amnezia_exit_code, amnezia_output = -1, ""
        errors.append(
            "AmneziaWG stats failed: "
            f"{sanitize_error(exc, aeza_token(), eu_ssh_password())}"
        )
    if amnezia_exit_code == 0:
        amnezia_parsed = parse_amnezia_dump_output(amnezia_output)
    elif amnezia_output:
        errors.append(
            "AmneziaWG stats failed with exit code "
            f"{amnezia_exit_code}: "
            f"{sanitize_error(amnezia_output[-2000:], aeza_token(), eu_ssh_password())}"
        )

    if errors and xray_exit_code != 0 and amnezia_exit_code != 0:
        raise RuntimeError("Stats collection failed: " + " | ".join(errors))

    refreshed = 0
    seen_at = now_iso()
    for client in list_clients():
        email = xray_client_email(client)
        previous = get_client_stats(int(client["id"]))
        previous_xray = {
            "uplink": int(previous["vless_uplink"]) if previous else 0,
            "downlink": int(previous["vless_downlink"]) if previous else 0,
        }
        previous_amnezia = {
            "rx": int(previous["amnezia_rx"]) if previous else 0,
            "tx": int(previous["amnezia_tx"]) if previous else 0,
            "latest_handshake": int(previous["amnezia_latest_handshake"])
            if previous
            else 0,
        }
        xray_values = (
            xray_parsed.get(email, {"uplink": 0, "downlink": 0})
            if xray_exit_code == 0
            else previous_xray
        )
        amnezia_values = (
            amnezia_parsed.get(
                client.get("amnezia_public_key") or "",
                {"rx": 0, "tx": 0, "latest_handshake": 0},
            )
            if amnezia_exit_code == 0
            else previous_amnezia
        )
        uplink = xray_values["uplink"]
        downlink = xray_values["downlink"]
        amnezia_rx = amnezia_values["rx"]
        amnezia_tx = amnezia_values["tx"]
        amnezia_latest_handshake = amnezia_values["latest_handshake"]
        last_seen_at = None
        xray_seen = xray_exit_code == 0 and (
            uplink > previous_xray["uplink"]
            or downlink > previous_xray["downlink"]
        )
        amnezia_seen = amnezia_exit_code == 0 and (
            amnezia_rx > previous_amnezia["rx"]
            or amnezia_tx > previous_amnezia["tx"]
            or amnezia_latest_handshake > previous_amnezia["latest_handshake"]
        )
        if xray_seen or amnezia_seen:
            last_seen_at = seen_at
        upsert_client_stats(
            int(client["id"]),
            vless_uplink=uplink,
            vless_downlink=downlink,
            amnezia_rx=amnezia_rx,
            amnezia_tx=amnezia_tx,
            amnezia_latest_handshake=amnezia_latest_handshake,
            last_seen_at=last_seen_at,
            raw=json.dumps(
                {
                    "xray": xray_values,
                    "amnezia": amnezia_values,
                    "errors": errors,
                }
            ),
        )
        refreshed += 1

    set_setting("stats.last_refresh_at", seen_at)
    set_setting("stats.last_error", " | ".join(errors))
    return {"refreshed": refreshed, "updated_at": seen_at, "errors": errors}
