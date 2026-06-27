from app.deep_protocol_checks import parse_deep_check_output


def test_parse_deep_check_output() -> None:
    result = parse_deep_check_output(
        """
        VLESS_DEEP=VERIFIED
        HYSTERIA_DEEP=VERIFIED
        AMNEZIA_DEEP=VERIFIED
        """
    )

    assert result["vless"].verified is True
    assert "hysteria" not in result
    assert result["amnezia"].verified is True


def test_parse_deep_check_output_ignores_hysteria_failures() -> None:
    result = parse_deep_check_output("HYSTERIA_DEEP=FAILED")

    assert "hysteria" not in result
