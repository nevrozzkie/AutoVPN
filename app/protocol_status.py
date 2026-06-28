from __future__ import annotations

import asyncio

from app.db import get_setting, now_iso, set_setting
from app.deep_protocol_checks import DeepCheckResult, run_deep_protocol_checks
from app.health import ProbeResult, tcp_probe, udp_probe
from app.runtime_config import amnezia_port, hysteria_port, vless_port


# Each protocol row is checked in two layers:
#   * a network probe from the control plane (tcp/udp reachability), and
#   * a server-side "deep" check over SSH (real handshake / liveness).
#
# Hysteria is tracked in two internal rows: service liveness and a full tunnel
# check. The client page shows only the tunnel row with a plain product name.
PROTOCOLS = [
    {
        "key": "vless",
        "name": "VLESS",
        "port": vless_port,
        "enabled": True,
        "probe": "tcp",
        "deep_key": "vless",
        "kind": "generic",
    },
    {
        "key": "hysteria_quic",
        "name": "Hysteria · порт/сервис (QUIC)",
        "port": hysteria_port,
        "enabled": True,
        "probe": "udp",
        "udp": True,
        "deep_key": "hysteria_quic",
        "kind": "hysteria_service",
    },
    {
        "key": "hysteria_salamander",
        "name": "Hysteria · туннель",
        "port": hysteria_port,
        "enabled": True,
        "probe": None,
        "deep_key": "hysteria_salamander",
        "kind": "hysteria_tunnel",
    },
    {
        "key": "amnezia",
        "name": "AmneziaWG",
        "port": amnezia_port,
        "enabled": True,
        "probe": "udp",
        "udp": True,
        "deep_key": "amnezia",
        "kind": "generic",
    },
]


def _status_dict(protocol: dict, port: int, status: str) -> dict[str, str | int | bool | None]:
    key = protocol["key"]
    return {
        **protocol,
        "port": port,
        "enabled": bool(protocol["enabled"]) and port is not None,
        "status": status,
        "last_checked_at": get_setting(f"protocol.{key}.last_checked_at"),
        "last_ok_at": get_setting(f"protocol.{key}.last_ok_at"),
        "failed_since": get_setting(f"protocol.{key}.failed_since"),
        "ping_ms": _get_ping_ms(key),
    }


def get_protocol_statuses() -> list[dict[str, str | int | bool | None]]:
    statuses = []
    for protocol in PROTOCOLS:
        port = protocol["port"]()
        if port is None:
            continue
        status = get_setting(f"protocol.{protocol['key']}.status", "UNKNOWN")
        statuses.append(_status_dict(protocol, port, status))
    return statuses


def get_client_protocol_statuses() -> list[dict[str, str | int | bool | None]]:
    statuses = []
    for status in get_protocol_statuses():
        if status["key"] == "hysteria_quic":
            continue
        if status["key"] == "hysteria_salamander":
            status = {**status, "name": "Hysteria", "note": ""}
        statuses.append(status)
    return statuses


def _compute_status(protocol: dict, probe_ok: bool | None, deep: DeepCheckResult | None) -> str:
    kind = protocol["kind"]
    if kind == "hysteria_service":
        # "QUIC/transport" row: port reachable + hysteria-server running.
        if deep is not None and deep.verified:
            return "SERVICE_ACTIVE"
        if deep is not None and not deep.verified:
            return "FAILED"
        if probe_ok:
            return "UDP_PACKET_SENT"
        return "FAILED"
    if kind == "hysteria_tunnel":
        # Tunnel row: purely the end-to-end deep check.
        if deep is None:
            return "UNKNOWN"
        return "VERIFIED" if deep.verified else "FAILED"
    # Generic protocols (VLESS / AmneziaWG): reachability, overridden by deep.
    if probe_ok:
        base = "UDP_PACKET_SENT" if protocol.get("udp") else "TCP_REACHABLE"
    else:
        base = "FAILED"
    if deep is not None:
        return "VERIFIED" if deep.verified else "FAILED"
    return base


async def refresh_protocol_statuses(current_ip: str) -> list[dict[str, str | int | bool | None]]:
    checked_at = now_iso()
    deep_results = await run_deep_protocol_checks(current_ip)

    probe_tasks: list[asyncio.Task[ProbeResult] | None] = []
    for protocol in PROTOCOLS:
        port = protocol["port"]()
        if port is None:
            set_setting(f"protocol.{protocol['key']}.status", "NOT_CONFIGURED")
            set_setting(f"protocol.{protocol['key']}.failed_since", "")
            set_setting(f"protocol.{protocol['key']}.ping_ms", "")
            probe_tasks.append(None)
        elif current_ip and protocol["enabled"] and protocol["probe"] == "tcp":
            probe_tasks.append(asyncio.create_task(tcp_probe(current_ip, int(port))))
        elif current_ip and protocol["enabled"] and protocol["probe"] == "udp":
            probe_tasks.append(asyncio.create_task(udp_probe(current_ip, int(port))))
        else:
            # No network probe for tunnel-only rows.
            probe_tasks.append(None)

    gathered = await asyncio.gather(
        *(task for task in probe_tasks if task is not None),
        return_exceptions=True,
    )
    probe_results: list[ProbeResult | None] = []
    gathered_index = 0
    for task in probe_tasks:
        if task is None:
            probe_results.append(None)
        else:
            result = gathered[gathered_index]
            gathered_index += 1
            probe_results.append(result if isinstance(result, ProbeResult) else None)

    statuses = []
    for index, protocol in enumerate(PROTOCOLS):
        key = protocol["key"]
        port = protocol["port"]()
        if port is None:
            continue

        probe_result = probe_results[index]
        probe_ok = probe_result.ok if isinstance(probe_result, ProbeResult) else None
        deep = deep_results.get(protocol["deep_key"])
        status = _compute_status(protocol, probe_ok, deep)

        set_setting(f"protocol.{key}.last_checked_at", checked_at)
        ok_for_bookkeeping = status in ("VERIFIED", "SERVICE_ACTIVE", "TCP_REACHABLE", "UDP_PACKET_SENT")
        if status == "UNKNOWN":
            # No data this round (e.g. hysteria client missing on the VPS):
            # leave failed_since untouched, just clear the ping.
            set_setting(f"protocol.{key}.ping_ms", "")
        elif ok_for_bookkeeping:
            set_setting(f"protocol.{key}.last_ok_at", checked_at)
            set_setting(f"protocol.{key}.failed_since", "")
            ping = probe_result.latency_ms if isinstance(probe_result, ProbeResult) else None
            set_setting(f"protocol.{key}.ping_ms", str(ping or ""))
        else:  # FAILED
            if not get_setting(f"protocol.{key}.failed_since"):
                set_setting(f"protocol.{key}.failed_since", checked_at)
            set_setting(f"protocol.{key}.ping_ms", "")

        set_setting(f"protocol.{key}.status", status)
        statuses.append(_status_dict(protocol, port, status))
    return statuses


def _get_ping_ms(key: str) -> int | None:
    value = get_setting(f"protocol.{key}.ping_ms")
    try:
        return int(value) if value else None
    except ValueError:
        return None
