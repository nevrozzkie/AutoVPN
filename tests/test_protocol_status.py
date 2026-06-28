import asyncio
import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import init_db, set_setting
from app.deep_protocol_checks import DeepCheckResult
from app.health import ProbeResult
from app.protocol_status import refresh_protocol_statuses


def test_refresh_protocol_statuses_checks_udp_protocol_pings(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(port == 443, 12 if port == 443 else None)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 56)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert by_key["vless"]["status"] == "TCP_REACHABLE"
    assert by_key["vless"]["ping_ms"] == 12
    assert "hysteria" not in by_key
    assert by_key["amnezia"]["status"] == "UDP_PACKET_SENT"
    assert by_key["amnezia"]["ping_ms"] == 56


def test_refresh_protocol_statuses_marks_failed_udp_ping(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(port != 443, 56 if port != 443 else None)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert "hysteria" not in by_key
    assert by_key["amnezia"]["status"] == "UDP_PACKET_SENT"
    assert by_key["amnezia"]["ping_ms"] == 56


def test_refresh_protocol_statuses_prefers_deep_check_result(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 34)

    async def fake_deep_checks(host: str) -> dict[str, DeepCheckResult]:
        return {
            "vless": DeepCheckResult(True),
            "amnezia": DeepCheckResult(True),
        }

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)
    monkeypatch.setattr("app.protocol_status.run_deep_protocol_checks", fake_deep_checks)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert by_key["vless"]["status"] == "VERIFIED"
    assert "hysteria" not in by_key
    assert by_key["amnezia"]["status"] == "VERIFIED"


def test_refresh_protocol_statuses_marks_disabled_protocol_hidden(monkeypatch) -> None:
    init_db()
    set_setting("config.vless_enabled", "0")

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        raise AssertionError("VLESS probe should not run when the port is empty")

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 34)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    try:
        statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
        by_key = {status["key"]: status for status in statuses}

        assert "vless" not in by_key
    finally:
        set_setting("config.vless_enabled", "1")


def test_refresh_protocol_statuses_shows_enabled_hysteria_placeholder(monkeypatch) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")

    async def fake_tcp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 56)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    try:
        statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
        by_key = {status["key"]: status for status in statuses}

        assert by_key["hysteria"]["status"] == "PLACEHOLDER"
        assert by_key["hysteria"]["port"] == 8443
        assert by_key["hysteria"]["ping_ms"] is None
    finally:
        set_setting("config.hysteria_enabled", "0")
