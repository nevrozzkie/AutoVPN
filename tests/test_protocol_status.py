import asyncio

from fastapi.testclient import TestClient

import app.main as main
from app.db import create_client
from app.db import get_setting, init_db, set_setting
from app.deep_protocol_checks import DeepCheckResult
from app.health import ProbeResult
from app.protocol_status import (
    get_client_protocol_statuses,
    get_server_access,
    refresh_protocol_statuses,
    refresh_transport_protocol_statuses,
)


def test_refresh_protocol_statuses_checks_udp_protocol_pings(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
        return ProbeResult(port == 443, 12 if port == 443 else None)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 56)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert get_server_access("203.0.113.10")["status"] == "DOWN"
    assert by_key["vless"]["status"] == "TCP_REACHABLE"
    assert by_key["vless"]["ping_ms"] == 12
    assert "hysteria" not in by_key
    assert by_key["amnezia"]["status"] == "UDP_PACKET_SENT"
    assert by_key["amnezia"]["ping_ms"] == 56


def test_refresh_protocol_statuses_marks_failed_udp_ping(monkeypatch) -> None:
    init_db()

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
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

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 34)

    async def fake_deep_checks(
        host: str, *, command_timeout: float | None = None
    ) -> dict[str, DeepCheckResult]:
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

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
        assert port == 22, "VLESS probe should not run when the port is empty"
        return ProbeResult(True, 10)

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


def test_refresh_protocol_statuses_splits_hysteria_into_two_rows(monkeypatch) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 56)

    async def fake_deep_checks(
        host: str, *, command_timeout: float | None = None
    ) -> dict[str, DeepCheckResult]:
        return {
            "hysteria_quic": DeepCheckResult(True),
            "hysteria_salamander": DeepCheckResult(True),
        }

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)
    monkeypatch.setattr("app.protocol_status.run_deep_protocol_checks", fake_deep_checks)

    try:
        statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
        by_key = {status["key"]: status for status in statuses}

        assert by_key["hysteria_quic"]["status"] == "SERVICE_ACTIVE"
        assert by_key["hysteria_quic"]["port"] == 8443
        assert by_key["hysteria_salamander"]["status"] == "VERIFIED"
        assert by_key["hysteria_salamander"]["port"] == 8443
    finally:
        set_setting("config.hysteria_enabled", "0")


def test_refresh_protocol_statuses_isolates_salamander_failure(monkeypatch) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")

    async def fake_tcp_probe(host: str, port: int, **kwargs) -> ProbeResult:
        return ProbeResult(True, 12)

    async def fake_udp_probe(host: str, port: int) -> ProbeResult:
        return ProbeResult(True, 56)

    async def fake_deep_checks(
        host: str, *, command_timeout: float | None = None
    ) -> dict[str, DeepCheckResult]:
        # Service is up (port/QUIC ok) but the obfuscated tunnel fails:
        # e.g. an obfs/auth password mismatch.
        return {
            "hysteria_quic": DeepCheckResult(True),
            "hysteria_salamander": DeepCheckResult(False, "obfs mismatch"),
        }

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)
    monkeypatch.setattr("app.protocol_status.run_deep_protocol_checks", fake_deep_checks)

    try:
        statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
        by_key = {status["key"]: status for status in statuses}

        assert by_key["hysteria_quic"]["status"] == "SERVICE_ACTIVE"
        assert by_key["hysteria_salamander"]["status"] == "FAILED"
    finally:
        set_setting("config.hysteria_enabled", "0")


def test_client_protocol_statuses_show_single_plain_hysteria_row(monkeypatch) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    set_setting("protocol.hysteria_quic.status", "SERVICE_ACTIVE")
    set_setting("protocol.hysteria_salamander.status", "VERIFIED")

    try:
        statuses = get_client_protocol_statuses()
        by_key = {status["key"]: status for status in statuses}

        assert "hysteria_quic" not in by_key
        assert by_key["hysteria_salamander"]["name"] == "Hysteria"
        assert by_key["hysteria_salamander"].get("note") == ""
    finally:
        set_setting("config.hysteria_enabled", "0")


def test_public_transport_refresh_is_bounded_and_preserves_deep_state(
    monkeypatch,
) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    set_setting("protocol.vless.status", "VERIFIED")
    set_setting("protocol.vless.last_checked_at", "deep-vless-time")
    set_setting("protocol.hysteria_salamander.status", "VERIFIED")
    set_setting("protocol.hysteria_salamander.last_checked_at", "deep-hysteria-time")
    calls: list[tuple[str, int, float]] = []

    async def fake_tcp_probe(host: str, port: int, timeout: float) -> ProbeResult:
        calls.append(("tcp", port, timeout))
        return ProbeResult(True, 10)

    async def fake_udp_probe(host: str, port: int, timeout: float) -> ProbeResult:
        calls.append(("udp", port, timeout))
        return ProbeResult(True, 20)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)
    monkeypatch.setattr(
        "app.protocol_status.run_deep_protocol_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deep SSH check must not run")
        ),
    )

    try:
        statuses = asyncio.run(
            refresh_transport_protocol_statuses(
                "203.0.113.10", probe_timeout=0.25
            )
        )
        by_key = {status["key"]: status for status in statuses}

        assert set(by_key) == {"vless", "hysteria_quic", "amnezia"}
        assert by_key["vless"]["status"] == "TCP_REACHABLE"
        assert by_key["hysteria_quic"]["status"] == "UDP_PACKET_SENT"
        assert by_key["amnezia"]["status"] == "UDP_PACKET_SENT"
        assert all(timeout == 0.25 for _, _, timeout in calls)
        assert get_setting("protocol.vless.status") == "VERIFIED"
        assert get_setting("protocol.vless.last_checked_at") == "deep-vless-time"
        assert get_setting("protocol.hysteria_salamander.status") == "VERIFIED"
        assert (
            get_setting("protocol.hysteria_salamander.last_checked_at")
            == "deep-hysteria-time"
        )
        assert get_setting("protocol.amnezia.transport_status") == "UDP_PACKET_SENT"
    finally:
        set_setting("config.hysteria_enabled", "0")


def test_public_client_refresh_preserves_deep_results_and_shows_compact_status(
    monkeypatch,
) -> None:
    init_db()
    client = create_client("Public probe")
    set_setting("current_ip", "203.0.113.10")
    set_setting("protocol.vless.status", "VERIFIED")
    set_setting("protocol.vless.last_checked_at", "2026-01-01T00:00:00+00:00")

    async def fake_tcp_probe(host: str, port: int, timeout: float) -> ProbeResult:
        return ProbeResult(True, 11)

    async def fake_udp_probe(host: str, port: int, timeout: float) -> ProbeResult:
        return ProbeResult(True, 12)

    monkeypatch.setattr("app.protocol_status.tcp_probe", fake_tcp_probe)
    monkeypatch.setattr("app.protocol_status.udp_probe", fake_udp_probe)
    monkeypatch.setattr(
        "app.protocol_status.run_deep_protocol_checks",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("public refresh called SSH/deep check")
        ),
    )
    http = TestClient(main.app)

    refreshed = http.post(
        f"/client/{client['token']}/protocols/refresh",
        follow_redirects=False,
    )
    page = http.get(f"/client/{client['token']}")

    assert refreshed.status_code == 303
    assert refreshed.headers["location"] == f"/client/{client['token']}"
    assert get_setting("protocol.vless.status") == "VERIFIED"
    assert get_setting("protocol.vless.last_checked_at") == (
        "2026-01-01T00:00:00+00:00"
    )
    assert get_setting("protocol.vless.transport_status") == "TCP_REACHABLE"
    assert page.status_code == 200
    assert "Последняя проверка:" in page.text
    assert "Доступ к серверу" in page.text and "SSH, порт 22" in page.text
    assert get_server_access("203.0.113.10")["status"] == "UP"
    assert "Транспорт:" not in page.text
    assert "handshake" not in page.text



def test_compact_status_does_not_call_peer_configuration_a_working_vpn():
    init_db()
    client = create_client("Compact status")
    set_setting("current_ip", "203.0.113.10")
    for protocol in ("vless", "hysteria", "amnezia"):
        set_setting(f"config.{protocol}_enabled", "1")
    for key, value in {
        "vless.status": "VERIFIED",
        "vless.last_checked_at": "2020-11-15T02:45:00+03:00",
        "vless.transport_last_checked_at": "2020-11-15T00:10:00+00:00",
        "hysteria_salamander.status": "UNKNOWN",
        "hysteria_salamander.last_checked_at": "2020-11-15T00:30:00+00:00",
        "amnezia.status": "VERIFIED",
    }.items():
        set_setting(f"protocol.{key}", value)
    page = TestClient(main.app).get(f"/client/{client['token']}")
    status = page.text.split('<section id="vpn-status">', 1)[1].split('</section>', 1)[0]
    assert status.count('class="ok"') == 1
    assert status.count('>Не проверено<') == 2
    assert status.count('Последняя проверка:') == 1
    assert '15.11.2020 03:30:00 МСК' in status
    assert 'IP:' in status and 'Конфиг обновлён:' in status
    assert 'Транспорт:' not in status and 'handshake' not in status
    # Reachability alone must not upgrade the visible result to OK.
    set_setting('protocol.vless.status', 'TCP_REACHABLE')
    page = TestClient(main.app).get(f"/client/{client['token']}")
    status = page.text.split('<section id="vpn-status">', 1)[1].split('</section>', 1)[0]
    assert 'class="ok"' not in status



def test_recent_timestamp_labels_and_absolute_fallback():
    from datetime import datetime, UTC, timedelta
    now = datetime.now(UTC)
    assert main.format_recent_msk((now-timedelta(hours=2)).isoformat()) == '2 часа назад'
    assert main.format_recent_msk((now-timedelta(minutes=5)).isoformat()) == '5 минут назад'
    old = (now-timedelta(hours=25)).isoformat()
    assert main.format_recent_msk(old) == main.format_msk(old)
