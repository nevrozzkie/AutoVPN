from __future__ import annotations

import base64
import secrets

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519


def _raw_urlsafe_base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_reality_private_key() -> str:
    private_bytes = bytearray(secrets.token_bytes(32))
    private_bytes[0] &= 248
    private_bytes[31] &= 127
    private_bytes[31] |= 64
    return _raw_urlsafe_base64(bytes(private_bytes))


def generate_reality_public_key(private_key: str) -> str:
    padding = "=" * (-len(private_key) % 4)
    private_bytes = base64.urlsafe_b64decode(private_key + padding)
    private = x25519.X25519PrivateKey.from_private_bytes(private_bytes)
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _raw_urlsafe_base64(public_bytes)


def generate_reality_short_id() -> str:
    return secrets.token_hex(8)
