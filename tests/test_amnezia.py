from app.amnezia import (
    build_amnezia_client_config,
    build_amnezia_server_config,
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
