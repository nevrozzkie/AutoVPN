from __future__ import annotations

from urllib.parse import quote, urlencode

from app.hysteria_auth import hysteria_auth
from app.profile_names import profile_name
from app.vpn_config import CapturedVpnConfig, capture_vpn_config


def render_subscription(config: CapturedVpnConfig, client: dict) -> str:
    links: list[str] = []
    vless_query = urlencode(
        {
            "type": "tcp",
            "security": "reality",
            "pbk": config.vless.public_key,
            "fp": config.vless.fingerprint,
            "sni": config.vless.server_name,
            "sid": config.vless.short_id,
            "spx": config.vless.spider_x,
            "flow": "xtls-rprx-vision",
        }
    )
    if config.vless.protocol.enabled:
        vless_name = quote(profile_name(client, "vless"))
        links.append(
            f"vless://{client['vless_uuid']}@{config.current_ip}:{config.vless.protocol.port}"
            f"?{vless_query}#{vless_name}"
        )
    if config.hysteria.protocol.enabled:
        hysteria_name = quote(profile_name(client, "hysteria"))
        hysteria_password = quote(hysteria_auth(client, config), safe="")
        hysteria_obfs_password = config.hysteria.obfs_password
        hysteria_query = {
            "insecure": "1",
            "sni": config.vless.server_name,
        }
        if hysteria_obfs_password:
            hysteria_query["obfs"] = "salamander"
            hysteria_query["obfs-password"] = hysteria_obfs_password
        links.append(
            f"hy2://{hysteria_password}@{config.current_ip}:{config.hysteria.protocol.port}/"
            f"?{urlencode(hysteria_query)}#{hysteria_name}"
        )
    return "\n".join(links) + ("\n" if links else "")


def build_subscription(client: dict, current_ip: str) -> str:
    config = capture_vpn_config()
    return render_subscription(
        CapturedVpnConfig(
            current_ip=current_ip,
            config_updated_at=config.config_updated_at,
            vless=config.vless,
            hysteria=config.hysteria,
            amnezia=config.amnezia,
            clients=config.clients,
        ),
        client,
    )


def render_sing_box_subscription(config: CapturedVpnConfig, client: dict) -> dict:
    outbounds = []
    proxy_outbounds = []
    if config.vless.protocol.enabled:
        proxy_outbounds.append("vless-reality")
        outbounds.append(
            {
                "type": "vless",
                "tag": "vless-reality",
                "server": config.current_ip,
                "server_port": config.vless.protocol.port,
                "uuid": client["vless_uuid"],
                "flow": "xtls-rprx-vision",
                "tls": {
                    "enabled": True,
                    "server_name": config.vless.server_name,
                    "utls": {
                        "enabled": True,
                        "fingerprint": config.vless.fingerprint,
                    },
                    "reality": {
                        "enabled": True,
                        "public_key": config.vless.public_key,
                        "short_id": config.vless.short_id,
                    },
                },
            }
        )
    if config.hysteria.protocol.enabled:
        proxy_outbounds.append("hysteria2")
        hysteria2_outbound = {
            "type": "hysteria2",
            "tag": "hysteria2",
            "server": config.current_ip,
            "server_port": config.hysteria.protocol.port,
            "password": hysteria_auth(client, config),
            "tls": {
                "enabled": True,
                "server_name": config.vless.server_name,
                "insecure": True,
            },
        }
        hysteria_obfs_password = config.hysteria.obfs_password
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


def build_sing_box_subscription(client: dict, current_ip: str) -> dict:
    config = capture_vpn_config()
    return render_sing_box_subscription(
        CapturedVpnConfig(
            current_ip=current_ip,
            config_updated_at=config.config_updated_at,
            vless=config.vless,
            hysteria=config.hysteria,
            amnezia=config.amnezia,
            clients=config.clients,
        ),
        client,
    )
