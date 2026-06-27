from __future__ import annotations

import asyncio

from app.config import settings
from app.db import get_setting, now_iso, set_setting
from app.health import tcp_check


PROTOCOLS = [
    {"key": "vless", "name": "VLESS", "port": settings.vless_port, "enabled": True},
    {"key": "hysteria", "name": "Hysteria", "port": settings.hysteria_port, "enabled": True},
    {"key": "amnezia", "name": "AmneziaWG", "port": settings.amnezia_port, "enabled": True},
]


async def refresh_protocol_statuses(current_ip: str) -> list[dict[str, str | int | bool | None]]:
    checked_at = now_iso()
    check_tasks = []
    for protocol in PROTOCOLS:
        port = protocol["port"]
        if current_ip and protocol["enabled"] and port is not None and protocol["key"] != "amnezia":
            check_tasks.append(tcp_check(current_ip, int(port)))
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
        if check_tasks[index] is None:
            if protocol["key"] == "amnezia" and current_ip:
                status = "CONFIGURED_UDP"
                set_setting(f"protocol.{key}.last_checked_at", checked_at)
            else:
                status = "NOT_CONFIGURED" if not protocol["enabled"] else "UNKNOWN"
        else:
            result = results[result_index]
            result_index += 1
            ok = bool(result) if not isinstance(result, Exception) else False
            status = "OK" if ok else "FAILED"
            set_setting(f"protocol.{key}.last_checked_at", checked_at)
            if ok:
                set_setting(f"protocol.{key}.last_ok_at", checked_at)
                set_setting(f"protocol.{key}.failed_since", "")
            elif not get_setting(f"protocol.{key}.failed_since"):
                set_setting(f"protocol.{key}.failed_since", checked_at)
        set_setting(f"protocol.{key}.status", status)
        statuses.append(
            {
                **protocol,
                "status": status,
                "last_checked_at": get_setting(f"protocol.{key}.last_checked_at"),
                "last_ok_at": get_setting(f"protocol.{key}.last_ok_at"),
                "failed_since": get_setting(f"protocol.{key}.failed_since"),
            }
        )
    return statuses
