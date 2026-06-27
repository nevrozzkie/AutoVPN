from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    latency_ms: int | None = None


async def tcp_check(host: str, port: int, timeout: float = 5.0) -> bool:
    result = await tcp_probe(host, port, timeout=timeout)
    return result.ok


async def tcp_probe(host: str, port: int, timeout: float = 5.0) -> ProbeResult:
    started_at = asyncio.get_running_loop().time()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
        writer.close()
        await writer.wait_closed()
        return ProbeResult(True, _elapsed_ms(started_at))
    except OSError:
        return ProbeResult(False)
    except TimeoutError:
        return ProbeResult(False)


async def udp_probe(host: str, port: int, timeout: float = 1.0) -> ProbeResult:
    started_at = asyncio.get_running_loop().time()
    try:
        transport, _ = await asyncio.wait_for(
            asyncio.get_running_loop().create_datagram_endpoint(
                asyncio.DatagramProtocol,
                remote_addr=(host, port),
            ),
            timeout=timeout,
        )
        try:
            transport.sendto(b"\0")
            return ProbeResult(True, _elapsed_ms(started_at))
        finally:
            transport.close()
    except OSError:
        return ProbeResult(False)
    except TimeoutError:
        return ProbeResult(False)


def _elapsed_ms(started_at: float) -> int:
    elapsed = asyncio.get_running_loop().time() - started_at
    return max(1, round(elapsed * 1000))


async def check_vpn_health(
    host: str,
    *,
    ssh_port: int,
    vless_port: int,
    hysteria_port: int,
) -> dict[str, bool]:
    ssh, vless, hysteria_udp = await asyncio.gather(
        tcp_check(host, ssh_port),
        tcp_check(host, vless_port),
        udp_probe(host, hysteria_port),
    )
    hysteria_udp_packet_sent = hysteria_udp.ok
    return {
        "ssh": ssh,
        "vless_tcp_reachable": vless,
        "hysteria_udp_packet_sent": hysteria_udp_packet_sent,
        "ok": ssh and vless and hysteria_udp_packet_sent,
    }
