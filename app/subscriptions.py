from __future__ import annotations

from urllib.parse import quote, urlencode

from app.config import settings
from app.db import get_setting


def build_subscription(client: dict, current_ip: str) -> str:
    safe_name = quote(client["name"])
    hysteria_username = quote(f"client{client['id']}")
    hysteria_password = quote(client["hysteria_password"])
    vless_query = urlencode(
        {
            "type": "tcp",
            "security": "reality",
            "pbk": get_setting("vless.reality_public_key"),
            "fp": settings.vless_reality_fingerprint,
            "sni": settings.vless_reality_server_name,
            "sid": get_setting("vless.reality_short_id"),
            "spx": settings.vless_reality_spider_x,
            "flow": "xtls-rprx-vision",
        }
    )
    vless = (
        f"vless://{client['vless_uuid']}@{current_ip}:{settings.vless_port}"
        f"?{vless_query}#{safe_name}-vless"
    )
    hysteria = (
        f"hysteria2://{hysteria_username}:{hysteria_password}@{current_ip}:{settings.hysteria_port}"
        f"?insecure=1&sni=autovpn-eu#{safe_name}-hysteria"
    )
    return f"{vless}\n{hysteria}\n"
