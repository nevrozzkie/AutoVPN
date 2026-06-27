from app.aeza import normalize_ipv4, normalize_ipv4_list, normalize_ipv4_price


def test_normalize_ipv4_list_from_data_payload() -> None:
    result = normalize_ipv4_list(
        {
            "data": [
                {"id": 12, "ip": "203.0.113.10", "is_main": True},
                {"key": "abc", "address": "203.0.113.11"},
            ]
        }
    )

    assert result == [
        {
            "id": "12",
            "ip": "203.0.113.10",
            "is_main": True,
            "raw": {"id": 12, "ip": "203.0.113.10", "is_main": True},
        },
        {
            "id": "abc",
            "ip": "203.0.113.11",
            "is_main": False,
            "raw": {"key": "abc", "address": "203.0.113.11"},
        },
    ]


def test_normalize_ipv4_from_single_result() -> None:
    assert normalize_ipv4({"result": {"uuid": "ip-id", "ipv4": "203.0.113.20"}}) == {
        "id": "ip-id",
        "ip": "203.0.113.20",
        "is_main": False,
        "raw": {"uuid": "ip-id", "ipv4": "203.0.113.20"},
    }


def test_normalize_ipv4_price_from_har_shape() -> None:
    payload = {
        "termLimits": {"default": 16, "hour": 3},
        "price": 2,
        "protectedPrice": 9,
    }

    assert normalize_ipv4_price(payload) == {
        "price": 2,
        "protected_price": 9,
        "term_limits": {"default": 16, "hour": 3},
        "raw": payload,
    }
