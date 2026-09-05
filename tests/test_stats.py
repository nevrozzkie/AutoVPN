from __future__ import annotations

import pytest

import app.stats as stats
from app.db import (
    create_client,
    get_client_by_id,
    get_client_stats,
    init_db,
    upsert_client_stats,
)
from app.eu_install import xray_client_email
from app.stats import (
    format_bytes,
    parse_amnezia_dump_output,
    parse_xray_stats_output,
    refresh_client_stats,
)


def test_parse_xray_stats_output() -> None:
    output = '''
stat: <
  name: "user>>>Alice-1>>>traffic>>>uplink"
  value: 123
>
stat: <
  name: "user>>>Alice-1>>>traffic>>>downlink"
  value: 456
>
'''

    assert parse_xray_stats_output(output) == {
        "Alice-1": {
            "uplink": 123,
            "downlink": 456,
        }
    }


def test_format_bytes() -> None:
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(1024 * 1024) == "1.0 MB"


def test_parse_amnezia_dump_output() -> None:
    output = """server-private-key\tserver-public-key\t51820\toff
client-public-key-1\tpsk\t198.51.100.10:45000\t10.66.66.2/32\t1710000000\t1234\t5678\toff
client-public-key-2\tpsk\t(none)\t10.66.66.3/32\t0\t0\t0\toff
"""

    assert parse_amnezia_dump_output(output) == {
        "client-public-key-1": {
            "rx": 1234,
            "tx": 5678,
            "latest_handshake": 1710000000,
        },
        "client-public-key-2": {
            "rx": 0,
            "tx": 0,
            "latest_handshake": 0,
        },
    }


def _existing_stats(client_id: int) -> None:
    upsert_client_stats(
        client_id,
        vless_uplink=100,
        vless_downlink=200,
        amnezia_rx=300,
        amnezia_tx=400,
        amnezia_latest_handshake=500,
        last_seen_at="2026-01-01T00:00:00+00:00",
        raw="old",
    )


@pytest.mark.anyio
@pytest.mark.parametrize("failed_source", ["xray", "amnezia"])
async def test_partial_stats_failure_preserves_failed_source_and_updates_success(
    monkeypatch: pytest.MonkeyPatch,
    failed_source: str,
) -> None:
    init_db()
    client = create_client("Alice")
    _existing_stats(int(client["id"]))
    timeouts: list[float | None] = []

    async def fake_remote(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        timeouts.append(timeout)
        if command == stats.XRAY_STATS_COMMAND:
            if failed_source == "xray":
                return 1, "xray failed"
            email = xray_client_email(client)
            return 0, (
                f'name: "user>>>{email}>>>traffic>>>uplink"\nvalue: 150\n'
                f'name: "user>>>{email}>>>traffic>>>downlink"\nvalue: 250\n'
            )
        if failed_source == "amnezia":
            return 1, "awg failed"
        return 0, (
            f"{client['amnezia_public_key']}\tpsk\tendpoint\t10.66.66.2/32"
            "\t600\t350\t450\toff\n"
        )

    monkeypatch.setattr(stats, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(stats, "run_remote_command", fake_remote)

    result = await refresh_client_stats()
    stored = get_client_stats(int(client["id"]))

    assert stored is not None
    if failed_source == "xray":
        assert (stored["vless_uplink"], stored["vless_downlink"]) == (100, 200)
        assert (stored["amnezia_rx"], stored["amnezia_tx"]) == (350, 450)
    else:
        assert (stored["vless_uplink"], stored["vless_downlink"]) == (150, 250)
        assert (stored["amnezia_rx"], stored["amnezia_tx"]) == (300, 400)
    assert stored["last_seen_at"] != "2026-01-01T00:00:00+00:00"
    assert len(result["errors"]) == 1
    assert len(timeouts) == 2 and all(timeout and timeout > 0 for timeout in timeouts)


@pytest.mark.anyio
async def test_both_stats_fail_without_erasing_existing_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    client = create_client("Alice")
    _existing_stats(int(client["id"]))
    before = get_client_stats(int(client["id"]))

    async def fail_remote(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 1, "failed"

    monkeypatch.setattr(stats, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(stats, "run_remote_command", fail_remote)

    with pytest.raises(RuntimeError, match="Stats collection failed"):
        await refresh_client_stats()

    assert get_client_stats(int(client["id"])) == before


@pytest.mark.anyio
async def test_failed_source_does_not_advance_last_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    client = create_client("Alice")
    _existing_stats(int(client["id"]))

    async def fake_remote(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        if command == stats.XRAY_STATS_COMMAND:
            return 1, "failed"
        return 0, (
            f"{client['amnezia_public_key']}\tpsk\tendpoint\t10.66.66.2/32"
            "\t500\t300\t400\toff\n"
        )

    monkeypatch.setattr(stats, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(stats, "run_remote_command", fake_remote)

    await refresh_client_stats()

    assert get_client_stats(int(client["id"]))["last_seen_at"] == (
        "2026-01-01T00:00:00+00:00"
    )


@pytest.mark.anyio
async def test_router_peer_stats_are_aggregated_into_the_ordinary_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    client = create_client("Router owner")
    client_id = int(client["id"])
    ordinary_public_key = str(client["amnezia_public_key"])
    auxiliary_public_key = "router-auxiliary-public-key"
    original_token = str(client["token"])

    async def fake_remote(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        if command == stats.XRAY_STATS_COMMAND:
            return 0, ""
        return 0, (
            f"{ordinary_public_key}\tpsk\tendpoint\t10.66.66.2/32"
            "\t600\t100\t200\toff\n"
            f"{auxiliary_public_key}\tpsk\tendpoint\t10.66.66.3/32"
            "\t700\t300\t400\toff\n"
        )

    monkeypatch.setattr(stats, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(stats, "run_remote_command", fake_remote)
    monkeypatch.setattr(
        stats,
        "list_router_amnezia_public_keys",
        lambda: [{"client_id": client_id, "public_key": auxiliary_public_key}],
    )

    await refresh_client_stats()

    stored = get_client_stats(client_id)
    assert stored is not None
    assert (stored["amnezia_rx"], stored["amnezia_tx"]) == (400, 600)
    assert stored["amnezia_latest_handshake"] == 700
    raw = str(stored["raw"])
    assert auxiliary_public_key not in raw
    current = get_client_by_id(client_id)
    assert current is not None
    assert current["amnezia_public_key"] == ordinary_public_key
    assert current["token"] == original_token


def test_amnezia_aggregation_deduplicates_keys_and_uses_latest_handshake() -> None:
    parsed = {
        "ordinary": {"rx": 10, "tx": 20, "latest_handshake": 100},
        "router": {"rx": 30, "tx": 40, "latest_handshake": 200},
    }

    assert stats._aggregate_amnezia_stats(
        ["ordinary", "router", "router", "missing"], parsed
    ) == {"rx": 40, "tx": 60, "latest_handshake": 200}
