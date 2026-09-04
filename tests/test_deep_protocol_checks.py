import pytest

import app.deep_protocol_checks as deep_protocol_checks
import app.eu_install as eu_install
from app.deep_protocol_checks import parse_deep_check_output


def test_parse_deep_check_output_maps_all_protocols() -> None:
    result = parse_deep_check_output(
        """
        VLESS_DEEP=VERIFIED
        HYSTERIA_SERVICE=active
        HYSTERIA_DEEP=VERIFIED
        AMNEZIA_DEEP=VERIFIED
        """
    )

    assert result["vless"].verified is True
    assert result["hysteria_quic"].verified is True
    assert result["hysteria_salamander"].verified is True
    assert result["amnezia"].verified is True


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
