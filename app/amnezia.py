from __future__ import annotations

import base64
import ipaddress
import json
import secrets
import zlib
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from app.config import settings
from app.profile_names import profile_name


AMNEZIA_SERVER_ADDRESS = "10.66.66.1/24"


def generate_private_key() -> str:
    private_bytes = bytearray(secrets.token_bytes(32))
    private_bytes[0] &= 248
    private_bytes[31] &= 127
    private_bytes[31] |= 64
    return base64.b64encode(private_bytes).decode("ascii")


def generate_public_key(private_key: str) -> str:
    private_bytes = base64.b64decode(private_key)
    private = x25519.X25519PrivateKey.from_private_bytes(private_bytes)
    public_bytes = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(public_bytes).decode("ascii")


def generate_preshared_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def generate_obfuscation_settings() -> dict[str, int]:
    return {
        "jc": secrets.randbelow(5) + 3,
        "jmin": secrets.randbelow(40) + 20,
        "jmax": secrets.randbelow(600) + 700,
        "s1": secrets.randbelow(100) + 30,
        "s2": secrets.randbelow(100) + 30,
        "h1": secrets.randbits(32),
        "h2": secrets.randbits(32),
        "h3": secrets.randbits(32),
        "h4": secrets.randbits(32),
    }


def client_amnezia_address(client_id: int) -> str:
    octet = client_id + 1
    if octet > 254:
        raise ValueError("AMNEZIA_NETWORK_PREFIX supports up to 253 clients in MVP")
    ipaddress.ip_address(f"{settings.amnezia_network_prefix}.{octet}")
    return f"{settings.amnezia_network_prefix}.{octet}"


def build_amnezia_client_config(
    client: dict[str, Any],
    *,
    current_ip: str,
    server_public_key: str,
    obfuscation: dict[str, int],
) -> str:
    address = client.get("amnezia_ipv4") or client_amnezia_address(int(client["id"]))
    return f"""[Interface]
PrivateKey = {client["amnezia_private_key"]}
Address = {address}/32
DNS = {settings.amnezia_dns}
Jc = {obfuscation["jc"]}
Jmin = {obfuscation["jmin"]}
Jmax = {obfuscation["jmax"]}
S1 = {obfuscation["s1"]}
S2 = {obfuscation["s2"]}
H1 = {obfuscation["h1"]}
H2 = {obfuscation["h2"]}
H3 = {obfuscation["h3"]}
H4 = {obfuscation["h4"]}

[Peer]
PublicKey = {server_public_key}
PresharedKey = {client["amnezia_preshared_key"]}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = {current_ip}:{settings.amnezia_port}
PersistentKeepalive = 25
"""


def build_amnezia_vpn_key(
    client: dict[str, Any],
    *,
    current_ip: str,
    server_public_key: str,
    obfuscation: dict[str, int],
) -> str:
    config = build_amnezia_client_config(
        client,
        current_ip=current_ip,
        server_public_key=server_public_key,
        obfuscation=obfuscation,
    )
    address = client.get("amnezia_ipv4") or client_amnezia_address(int(client["id"]))
    dns_values = [item.strip() for item in settings.amnezia_dns.split(",") if item.strip()]
    dns1 = dns_values[0] if dns_values else "1.1.1.1"
    dns2 = dns_values[1] if len(dns_values) > 1 else dns1
    awg = {
        "H1": str(obfuscation["h1"]),
        "H2": str(obfuscation["h2"]),
        "H3": str(obfuscation["h3"]),
        "H4": str(obfuscation["h4"]),
        "I1": "",
        "I2": "",
        "I3": "",
        "I4": "",
        "I5": "",
        "Jc": str(obfuscation["jc"]),
        "Jmax": str(obfuscation["jmax"]),
        "Jmin": str(obfuscation["jmin"]),
        "S1": str(obfuscation["s1"]),
        "S2": str(obfuscation["s2"]),
        "S3": "0",
        "S4": "0",
        "allowed_ips": ["0.0.0.0/0", "::/0"],
        "clientId": client["amnezia_public_key"],
        "client_ip": address,
        "client_priv_key": client["amnezia_private_key"],
        "client_pub_key": client["amnezia_public_key"],
        "config": config,
        "hostName": current_ip,
        "mtu": "1280",
        "persistent_keep_alive": "25",
        "port": settings.amnezia_port,
        "psk_key": client["amnezia_preshared_key"],
        "server_pub_key": server_public_key,
    }
    awg["last_config"] = json.dumps(awg, ensure_ascii=False, indent=4)
    payload = {
        "containers": [
            {
                "awg": {
                    **awg,
                    "port": str(settings.amnezia_port),
                    "protocol_version": "2",
                    "subnet_address": f"{settings.amnezia_network_prefix}.0",
                    "transport_proto": "udp",
                },
                "container": "amnezia-awg2",
            }
        ],
        "defaultContainer": "amnezia-awg2",
        "description": profile_name(client, "amnezia"),
        "dns1": dns1,
        "dns2": dns2,
        "hostName": current_ip,
    }
    raw = json.dumps(payload, ensure_ascii=False, indent=4).encode("utf-8")
    packed = len(raw).to_bytes(4, "big") + zlib.compress(raw)
    return "vpn://" + base64.urlsafe_b64encode(packed).decode("ascii").rstrip("=")


def build_amnezia_server_config(
    clients: list[dict[str, Any]],
    *,
    server_private_key: str,
    obfuscation: dict[str, int],
) -> str:
    peer_blocks = []
    for client in clients:
        address = client.get("amnezia_ipv4") or client_amnezia_address(int(client["id"]))
        peer_blocks.append(
            f"""[Peer]
# {client["name"]}
PublicKey = {client["amnezia_public_key"]}
PresharedKey = {client["amnezia_preshared_key"]}
AllowedIPs = {address}/32
"""
        )
    peers = "\n".join(peer_blocks)
    return f"""[Interface]
PrivateKey = {server_private_key}
Address = {AMNEZIA_SERVER_ADDRESS}
ListenPort = {settings.amnezia_port}
Jc = {obfuscation["jc"]}
Jmin = {obfuscation["jmin"]}
Jmax = {obfuscation["jmax"]}
S1 = {obfuscation["s1"]}
S2 = {obfuscation["s2"]}
H1 = {obfuscation["h1"]}
H2 = {obfuscation["h2"]}
H3 = {obfuscation["h3"]}
H4 = {obfuscation["h4"]}
PostUp = sysctl -w net.ipv4.ip_forward=1; iptables -t nat -A POSTROUTING -s {settings.amnezia_network_prefix}.0/24 -o $(ip route show default | awk '{{print $5; exit}}') -j MASQUERADE
PostDown = iptables -t nat -D POSTROUTING -s {settings.amnezia_network_prefix}.0/24 -o $(ip route show default | awk '{{print $5; exit}}') -j MASQUERADE

{peers}
"""
