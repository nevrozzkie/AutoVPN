import pytest

from app.aeza import (
    AezaClient,
    _response_text,
    aeza_api_key,
    normalize_ipv4,
    normalize_ipv4_list,
    normalize_ipv4_price,
)


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("abc123", "abc123"),
        (" Bearer abc123 ", "abc123"),
        ("Authorization: Bearer abc123", "abc123"),
        ("X-API-Key: abc123", "abc123"),
        ("token abc123", "abc123"),
    ],
)
def test_aeza_api_key_accepts_raw_token_or_header(token: str, expected: str) -> None:
    assert aeza_api_key(token) == expected


def test_aeza_client_headers_use_x_api_key() -> None:
    client = AezaClient("https://my.aeza.net", "Bearer abc123")

    assert client._headers()["X-API-Key"] == "abc123"
    assert "Authorization" not in client._headers()


class FakeResponse:
    def __init__(self, content: bytes, text: str) -> None:
        self.content = content
        self.text = text


def test_response_text_strips_response_body() -> None:
    assert _response_text(FakeResponse(b'{"message":"bad request"}', ' {"message":"bad request"} ')) == (
        '{"message":"bad request"}'
    )


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
