import asyncio

from app.health import ProbeResult, check_vpn_health


def test_vpn_health_names_probe_results_without_claiming_protocol_auth(monkeypatch) -> None:
    async def fake_tcp_check(host: str, port: int) -> bool:
        return port in (22, 443)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(port == 8443, 15 if port == 8443 else None)

    monkeypatch.setattr("app.health.tcp_check", fake_tcp_check)
    monkeypatch.setattr("app.health.udp_probe", fake_udp_probe)

    result = asyncio.run(
        check_vpn_health(
            "203.0.113.10",
            ssh_port=22,
            vless_port=443,
            hysteria_port=8443,
        )
    )

    assert result == {
        "ssh": True,
        "vless_tcp_reachable": True,
        "hysteria_udp_packet_sent": True,
        "ok": True,
    }
