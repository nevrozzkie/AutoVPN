from dataclasses import replace

import pytest

import app.deep_protocol_checks as deep_protocol_checks
import app.eu_install as eu_install
from app.deep_protocol_checks import parse_deep_check_output
from app.db import create_client, init_db
from app.vpn_config import RouterAmneziaPeer, capture_vpn_config


def test_parse_deep_check_output_maps_all_protocols() -> None:
    result = parse_deep_check_output(
        """
        VLESS_DEEP=VERIFIED
        HYSTERIA_SERVICE=active
        HYSTERIA_DEEP=VERIFIED
        AMNEZIA_CONFIGURED=1
        """
    )

    assert result["vless"].verified is True
    assert result["hysteria_quic"].verified is True
    assert result["hysteria_salamander"].verified is True
    assert "amnezia" not in result


def test_amnezia_peer_presence_is_not_reported_as_verified() -> None:
    result = parse_deep_check_output(
        "AMNEZIA_CONFIGURED=1\nAMNEZIA_DEEP=VERIFIED"
    )

    assert "amnezia" not in result


@pytest.mark.anyio
async def test_bound_check_with_no_clients_never_falls_back_to_live_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    config = capture_vpn_config()
    monkeypatch.setattr(
        deep_protocol_checks,
        "_first_enabled_client",
        lambda: pytest.fail("live client lookup used"),
    )

    assert await deep_protocol_checks.run_deep_protocol_checks(
        "203.0.113.20", config=config, target_host="203.0.113.20"
    ) == {}


def test_captured_hysteria_sni_does_not_fall_back_to_live_settings() -> None:
    init_db()
    config = capture_vpn_config()
    config = replace(
        config,
        vless=replace(config.vless, server_name="bound-sni.example"),
    )

    script = deep_protocol_checks.build_deep_check_script(
        {
            "id": 1,
            "hysteria_password": "personal-secret",
            "vless_uuid": "00000000-0000-0000-0000-000000000001",
        },
        "203.0.113.20",
        config=config,
    )

    assert "sni: bound-sni.example" in script
    assert "auth: client-1:personal-secret" in script


def test_expected_amnezia_peers_include_captured_router_peer_without_private_key() -> None:
    init_db()
    client = create_client("Router owner")
    config = capture_vpn_config()
    auxiliary_public_key = "router-auxiliary-public-key"
    auxiliary_private_key = "router-auxiliary-private-secret"
    auxiliary_preshared_key = "router-auxiliary-preshared-secret"
    config = replace(
        config,
        router_amnezia_peers=(
            RouterAmneziaPeer(
                router_id="router-auxiliary",
                client_id=int(client["id"]),
                public_key=auxiliary_public_key,
                private_key=auxiliary_private_key,
                preshared_key=auxiliary_preshared_key,
                ipv4="10.66.66.3",
            ),
        ),
    )

    script = deep_protocol_checks.build_deep_check_script(
        client,
        "203.0.113.20",
        config=config,
    )

    assert str(client["amnezia_public_key"]) in script
    assert auxiliary_public_key in script
    assert auxiliary_private_key not in script
    assert auxiliary_preshared_key not in script


def test_live_expected_amnezia_peers_include_authorized_router_public_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    client = create_client("Router owner")
    auxiliary_public_key = "router-live-public-key"
    monkeypatch.setattr(
        deep_protocol_checks,
        "list_router_amnezia_public_keys",
        lambda: [{"client_id": client["id"], "public_key": auxiliary_public_key}],
    )

    script = deep_protocol_checks.build_deep_check_script(
        client,
        "203.0.113.20",
    )

    assert auxiliary_public_key in script


def test_parse_deep_check_output_splits_hysteria_layers() -> None:
    # Service running but the obfuscated tunnel failed (obfs/auth mismatch).
    result = parse_deep_check_output("HYSTERIA_SERVICE=active\nHYSTERIA_DEEP=FAILED")

    assert result["hysteria_quic"].verified is True
    assert result["hysteria_salamander"].verified is False


def test_parse_deep_check_output_handles_inactive_service() -> None:
    result = parse_deep_check_output("HYSTERIA_SERVICE=inactive")

    assert result["hysteria_quic"].verified is False
    assert "hysteria_salamander" not in result


@pytest.mark.anyio
async def test_deep_check_forwards_bounded_ssh_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_remote_command(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        captured.update(host=host, command=command, stdin_data=stdin_data, timeout=timeout)
        return 0, "VLESS_DEEP=VERIFIED"

    monkeypatch.setattr(deep_protocol_checks, "_first_enabled_client", lambda: {"id": 1})
    monkeypatch.setattr(
        deep_protocol_checks, "build_deep_check_script", lambda client, ip: "check-script"
    )
    monkeypatch.setattr(eu_install, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(eu_install, "run_remote_command", fake_remote_command)

    result = await deep_protocol_checks.run_deep_protocol_checks(
        "203.0.113.10", command_timeout=7.5
    )

    assert result["vless"].verified is True
    assert captured == {
        "host": "203.0.113.10",
        "command": "bash -s",
        "stdin_data": "check-script",
        "timeout": 7.5,
    }


@pytest.mark.anyio
async def test_deep_check_uses_default_bounded_ssh_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    captured: list[float | None] = []

    async def fake_remote_command(
        host: str,
        command: str,
        stdin_data: str = "",
        timeout: float | None = None,
    ) -> tuple[int, str]:
        captured.append(timeout)
        return 0, "VLESS_DEEP=VERIFIED"

    monkeypatch.setattr(deep_protocol_checks, "_first_enabled_client", lambda: {"id": 1})
    monkeypatch.setattr(
        deep_protocol_checks, "build_deep_check_script", lambda client, ip: "check-script"
    )
    monkeypatch.setattr(eu_install, "resolve_eu_host", lambda: "203.0.113.10")
    monkeypatch.setattr(eu_install, "run_remote_command", fake_remote_command)

    result = await deep_protocol_checks.run_deep_protocol_checks("203.0.113.10")

    assert result["vless"].verified is True
    assert captured and captured[0] is not None and captured[0] > 0
