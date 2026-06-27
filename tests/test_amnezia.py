import base64
import json
import zlib

from app.amnezia import (
    build_amnezia_client_config,
    build_amnezia_server_config,
    build_amnezia_vpn_key,
    generate_private_key,
    generate_public_key,
)


def test_generate_amnezia_keys_are_wireguard_base64() -> None:
    private_key = generate_private_key()
    public_key = generate_public_key(private_key)

    assert len(private_key) == 44
    assert len(public_key) == 44


def test_build_amnezia_client_config() -> None:
    client = {
        "id": 1,
        "name": "Alice",
        "amnezia_private_key": "client-private",
        "amnezia_preshared_key": "psk",
        "amnezia_ipv4": "10.66.66.2",
    }
    config = build_amnezia_client_config(
        client,
        current_ip="203.0.113.10",
        server_public_key="server-public",
        obfuscation={
            "jc": 4,
            "jmin": 30,
            "jmax": 900,
            "s1": 64,
            "s2": 128,
            "h1": 1,
            "h2": 2,
            "h3": 3,
            "h4": 4,
        },
    )

    assert "PrivateKey = client-private" in config
    assert "Endpoint = 203.0.113.10:51820" in config
    assert "PublicKey = server-public" in config
    assert "PresharedKey = psk" in config
    assert "Jc = 4" in config


def test_build_amnezia_server_config_contains_peer() -> None:
    config = build_amnezia_server_config(
        [
            {
                "id": 1,
                "name": "Alice",
                "amnezia_public_key": "client-public",
                "amnezia_preshared_key": "psk",
                "amnezia_ipv4": "10.66.66.2",
            }
        ],
        server_private_key="server-private",
        obfuscation={
            "jc": 5,
            "jmin": 40,
            "jmax": 1000,
            "s1": 64,
            "s2": 128,
            "h1": 1,
            "h2": 2,
            "h3": 3,
            "h4": 4,
        },
    )

    assert "PrivateKey = server-private" in config
    assert "ListenPort = 51820" in config
    assert "PublicKey = client-public" in config
    assert "AllowedIPs = 10.66.66.2/32" in config


def test_build_amnezia_vpn_key_is_vpn_url_with_compressed_json() -> None:
    key = build_amnezia_vpn_key(
        {
            "id": 1,
            "name": "Alice",
            "amnezia_private_key": "client-private",
            "amnezia_public_key": "client-public",
            "amnezia_preshared_key": "psk",
            "amnezia_ipv4": "10.66.66.2",
        },
        current_ip="203.0.113.10",
        server_public_key="server-public",
        obfuscation={
            "jc": 5,
            "jmin": 40,
            "jmax": 1000,
            "s1": 64,
            "s2": 128,
            "h1": 1,
            "h2": 2,
            "h3": 3,
            "h4": 4,
        },
    )

    assert key.startswith("vpn://")
    encoded = key.removeprefix("vpn://")
    packed = base64.urlsafe_b64decode(encoded + "=" * ((4 - len(encoded) % 4) % 4))
    raw_length = int.from_bytes(packed[:4], "big")
    raw = zlib.decompress(packed[4:])
    payload = json.loads(raw)

    assert raw_length == len(raw)
    assert payload["defaultContainer"] == "amnezia-awg2"
    assert payload["description"] == "[AutoVPN] Alice - amnezia"
    assert payload["hostName"] == "203.0.113.10"
    assert payload["containers"][0]["awg"]["client_priv_key"] == "client-private"
