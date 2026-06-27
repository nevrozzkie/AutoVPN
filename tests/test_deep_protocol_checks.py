from app.deep_protocol_checks import parse_deep_check_output


def test_parse_deep_check_output() -> None:
    result = parse_deep_check_output(
        """
        VLESS_DEEP=VERIFIED
        HYSTERIA_DEEP=FAILED
        """
    )

    assert result["vless"].verified is True
    assert result["hysteria"].verified is False
