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


def build_sing_box_subscription(client: dict, current_ip: str) -> dict:
    return {
        "log": {
            "level": "info",
        },
        "dns": {
            "servers": [
                {
                    "tag": "cloudflare",
                    "address": "1.1.1.1",
                },
            ],
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "address": ["172.19.0.1/30"],
                "auto_route": True,
                "strict_route": True,
                "sniff": True,
            },
        ],
        "outbounds": [
            {
                "type": "selector",
                "tag": "proxy",
                "outbounds": ["vless-reality", "hysteria2"],
                "default": "vless-reality",
            },
            {
                "type": "vless",
                "tag": "vless-reality",
                "server": current_ip,
                "server_port": settings.vless_port,
                "uuid": client["vless_uuid"],
                "flow": "xtls-rprx-vision",
                "tls": {
                    "enabled": True,
                    "server_name": settings.vless_reality_server_name,
                    "utls": {
                        "enabled": True,
                        "fingerprint": settings.vless_reality_fingerprint,
                    },
                    "reality": {
                        "enabled": True,
                        "public_key": get_setting("vless.reality_public_key"),
                        "short_id": get_setting("vless.reality_short_id"),
                    },
                },
            },
            {
                "type": "hysteria2",
                "tag": "hysteria2",
                "server": current_ip,
                "server_port": settings.hysteria_port,
                "password": client["hysteria_password"],
                "tls": {
                    "enabled": True,
                    "server_name": "autovpn-eu",
                    "insecure": True,
                },
            },
            {
                "type": "direct",
                "tag": "direct",
            },
        ],
        "route": {
            "auto_detect_interface": True,
            "final": "proxy",
        },
    }
