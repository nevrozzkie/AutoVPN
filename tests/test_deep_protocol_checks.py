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
