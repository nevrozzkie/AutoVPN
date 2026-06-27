import asyncio
import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import get_setting, init_db
from app.health import ProbeResult
from app.protocol_status import refresh_protocol_statuses


def test_refresh_protocol_statuses_checks_udp_protocol_pings(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(port == 443, 12 if port == 443 else None)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 34 if port == 8443 else 56)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert by_key["vless"]["status"] == "OK"
    assert by_key["vless"]["ping_ms"] == 12
    assert by_key["hysteria"]["status"] == "OK"
    assert by_key["hysteria"]["ping_ms"] == 34
    assert by_key["amnezia"]["status"] == "OK"
    assert by_key["amnezia"]["ping_ms"] == 56
    assert get_setting("protocol.hysteria.failed_since") == ""
    assert get_setting("protocol.hysteria.ping_ms") == "34"


def test_refresh_protocol_statuses_marks_failed_udp_ping(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(port != 8443, 56 if port != 8443 else None)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert by_key["hysteria"]["status"] == "FAILED"
    assert by_key["hysteria"]["ping_ms"] is None
    assert by_key["amnezia"]["status"] == "OK"
    assert by_key["amnezia"]["ping_ms"] == 56
    assert get_setting("protocol.hysteria.failed_since")
