from __future__ import annotations

from urllib.parse import quote, urlencode

from app.config import settings
from app.db import get_setting
from app.profile_names import profile_name
from app.runtime_config import hysteria_port, vless_port


def hysteria_auth(_: dict | None = None) -> str:
    return get_setting("hysteria.password")


def build_subscription(client: dict, current_ip: str) -> str:
    links: list[str] = []
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
    current_vless_port = vless_port()
    if current_vless_port is not None:
        vless_name = quote(profile_name(client, "vless"))
        links.append(
            f"vless://{client['vless_uuid']}@{current_ip}:{current_vless_port}"
            f"?{vless_query}#{vless_name}"
        )
    current_hysteria_port = hysteria_port()
    if current_hysteria_port is not None:
        hysteria_name = quote(profile_name(client, "hysteria"))
        hysteria_password = quote(hysteria_auth(client))
        hysteria_obfs_password = get_setting("hysteria.obfs_password")
        hysteria_query = {
            "insecure": "1",
            "sni": settings.vless_reality_server_name,
        }
        if hysteria_obfs_password:
            hysteria_query["obfs"] = "salamander"
            hysteria_query["obfs-password"] = hysteria_obfs_password
        links.append(
            f"hysteria2://{hysteria_password}@{current_ip}:{current_hysteria_port}/"
            f"?{urlencode(hysteria_query)}#{hysteria_name}"
        )
    return "\n".join(links) + ("\n" if links else "")


def build_sing_box_subscription(client: dict, current_ip: str) -> dict:
    outbounds = []
    proxy_outbounds = []
    current_vless_port = vless_port()
    if current_vless_port is not None:
        proxy_outbounds.append("vless-reality")
        outbounds.append(
            {
                "type": "vless",
                "tag": "vless-reality",
                "server": current_ip,
                "server_port": current_vless_port,
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
            }
        )
    current_hysteria_port = hysteria_port()
    if current_hysteria_port is not None:
        proxy_outbounds.append("hysteria2")
        hysteria2_outbound = {
            "type": "hysteria2",
            "tag": "hysteria2",
            "server": current_ip,
            "server_port": current_hysteria_port,
            "password": hysteria_auth(client),
            "tls": {
                "enabled": True,
                "server_name": settings.vless_reality_server_name,
                "insecure": True,
            },
        }
        hysteria_obfs_password = get_setting("hysteria.obfs_password")
        if hysteria_obfs_password:
            hysteria2_outbound["obfs"] = {
                "type": "salamander",
                "password": hysteria_obfs_password,
            }
        outbounds.append(hysteria2_outbound)
    if not proxy_outbounds:
        proxy_outbounds.append("direct")
    outbounds.insert(
        0,
        {
            "type": "selector",
            "tag": "proxy",
            "outbounds": proxy_outbounds,
            "default": proxy_outbounds[0],
        },
    )
    outbounds.append(
        {
            "type": "direct",
            "tag": "direct",
        }
    )
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
        "outbounds": outbounds,
        "route": {
            "auto_detect_interface": True,
            "final": "proxy",
        },
    }
