from __future__ import annotations

import asyncio

from app.db import get_setting, now_iso, set_setting
from app.deep_protocol_checks import run_deep_protocol_checks
from app.health import ProbeResult, tcp_probe, udp_probe
from app.runtime_config import amnezia_port, hysteria_port, vless_port


PROTOCOLS = [
    {"key": "vless", "name": "VLESS", "port": vless_port, "enabled": True},
    {"key": "hysteria", "name": "Hysteria", "port": hysteria_port, "enabled": False, "placeholder": True},
    {"key": "amnezia", "name": "AmneziaWG", "port": amnezia_port, "enabled": True, "udp": True},
]


def get_protocol_statuses() -> list[dict[str, str | int | bool | None]]:
    statuses = []
    for protocol in PROTOCOLS:
        key = protocol["key"]
        port = protocol["port"]()
        enabled = bool(protocol["enabled"]) and port is not None
        statuses.append(
            {
                **protocol,
                "port": port,
                "enabled": enabled,
                "status": get_setting(f"protocol.{key}.status", "UNKNOWN"),
                "last_checked_at": get_setting(f"protocol.{key}.last_checked_at"),
                "last_ok_at": get_setting(f"protocol.{key}.last_ok_at"),
                "failed_since": get_setting(f"protocol.{key}.failed_since"),
                "ping_ms": _get_ping_ms(key),
            }
        )
    return statuses


async def refresh_protocol_statuses(current_ip: str) -> list[dict[str, str | int | bool | None]]:
    checked_at = now_iso()
    deep_results = await run_deep_protocol_checks(current_ip)
    check_tasks: list[asyncio.Task[ProbeResult] | None] = []
    for protocol in PROTOCOLS:
        port = protocol["port"]()
        if current_ip and protocol["enabled"] and port is not None:
            if protocol.get("udp"):
                check_tasks.append(asyncio.create_task(udp_probe(current_ip, int(port))))
            else:
                check_tasks.append(asyncio.create_task(tcp_probe(current_ip, int(port))))
        else:
            check_tasks.append(None)

    results = await asyncio.gather(
        *(task for task in check_tasks if task is not None),
        return_exceptions=True,
    )
    result_index = 0
    statuses = []
    for index, protocol in enumerate(PROTOCOLS):
        key = protocol["key"]
        port = protocol["port"]()
        if check_tasks[index] is None:
            status = "PLACEHOLDER" if protocol.get("placeholder") and port is not None else "NOT_CONFIGURED"
            set_setting(f"protocol.{key}.failed_since", "")
            set_setting(f"protocol.{key}.ping_ms", "")
        else:
            result = results[result_index]
            result_index += 1
            ok = result.ok if isinstance(result, ProbeResult) else False
            if ok and protocol.get("udp"):
                status = "UDP_PACKET_SENT"
            elif ok:
                status = "TCP_REACHABLE"
            else:
                status = "FAILED"
            deep_result = deep_results.get(key)
            if deep_result and deep_result.verified:
                status = "VERIFIED"
            elif deep_result and not deep_result.verified:
                status = "FAILED"
            set_setting(f"protocol.{key}.last_checked_at", checked_at)
            if ok:
                set_setting(f"protocol.{key}.last_ok_at", checked_at)
                set_setting(f"protocol.{key}.failed_since", "")
                set_setting(f"protocol.{key}.ping_ms", str(result.latency_ms or ""))
            else:
                if not get_setting(f"protocol.{key}.failed_since"):
                    set_setting(f"protocol.{key}.failed_since", checked_at)
                set_setting(f"protocol.{key}.ping_ms", "")
        set_setting(f"protocol.{key}.status", status)
        statuses.append(
            {
                **protocol,
                "port": port,
                "enabled": bool(protocol["enabled"]) and port is not None,
                "status": status,
                "last_checked_at": get_setting(f"protocol.{key}.last_checked_at"),
                "last_ok_at": get_setting(f"protocol.{key}.last_ok_at"),
                "failed_since": get_setting(f"protocol.{key}.failed_since"),
                "ping_ms": _get_ping_ms(key),
            }
        )
    return statuses


def _get_ping_ms(key: str) -> int | None:
    value = get_setting(f"protocol.{key}.ping_ms")
    try:
        return int(value) if value else None
    except ValueError:
        return None
