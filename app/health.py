from __future__ import annotations

import asyncio


async def tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False
    except TimeoutError:
        return False


async def check_vpn_health(
    host: str,
    *,
    ssh_port: int,
    vless_port: int,
    hysteria_port: int,
) -> dict[str, bool]:
    ssh, vless = await asyncio.gather(
        tcp_check(host, ssh_port),
        tcp_check(host, vless_port),
    )
    hysteria = bool(hysteria_port)
    return {
        "ssh": ssh,
        "vless": vless,
        "hysteria_udp_configured": hysteria,
        "ok": ssh and vless and hysteria,
    }
