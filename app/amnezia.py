from __future__ import annotations

import base64
import ipaddress
import secrets
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import x25519

from app.config import settings


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
