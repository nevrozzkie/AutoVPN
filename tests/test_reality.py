from app.reality import (
    generate_reality_private_key,
    generate_reality_public_key,
    generate_reality_short_id,
)


def test_reality_keys_are_raw_urlsafe_base64() -> None:
    private_key = generate_reality_private_key()
    public_key = generate_reality_public_key(private_key)

    assert len(private_key) == 43
    assert len(public_key) == 43
    assert "=" not in private_key
    assert "=" not in public_key


def test_reality_short_id_is_hex_16_chars() -> None:
    short_id = generate_reality_short_id()

    assert len(short_id) == 16
    int(short_id, 16)
