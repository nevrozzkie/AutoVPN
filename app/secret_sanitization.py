from __future__ import annotations

import re


_ROUTER_TOKEN = re.compile(r"avrt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{20,128}")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [^-\r\n]{0,64}PRIVATE KEY-----.*?"
    r"-----END [^-\r\n]{0,64}PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_PRIVATE_KEY_MARKER = re.compile(
    r"-----BEGIN [^-\r\n]{0,64}PRIVATE KEY-----|"
    r"-----END [^-\r\n]{0,64}PRIVATE KEY-----",
    re.IGNORECASE,
)


def sanitize_error(value: object, *configured_secrets: str, limit: int = 4000) -> str:
    message = str(value or "")
    for secret in configured_secrets:
        if secret:
            message = message.replace(secret, "[redacted]")
    message = _PRIVATE_KEY_BLOCK.sub("[redacted-private-key]", message)
    message = _PRIVATE_KEY_MARKER.sub("[redacted-private-key]", message)
    message = _ROUTER_TOKEN.sub("[redacted-router-token]", message)
    message = re.sub(
        r"(?i)(password|passwd|authorization)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    return " ".join(message.split())[:limit]
