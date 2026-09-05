from __future__ import annotations

import json

from app.vpn_config import CapturedVpnConfig


def hysteria_auth(client: dict, config: CapturedVpnConfig | None = None) -> str:
    """Match the authentication mode of the supplied deployment snapshot."""
    if config is not None and config.hysteria.auth_type == "password":
        return config.hysteria.password
    if config is not None and config.hysteria.auth_type != "userpass":
        raise ValueError("Unsupported Hysteria authentication mode")
    return f"client-{int(client['id'])}:{client['hysteria_password']}"


def render_hysteria_server_auth(config: CapturedVpnConfig) -> str:
    if config.hysteria.auth_type == "password":
        # Only historical deployment snapshots use the shared password.
        return f"auth:\n  type: password\n  password: {json.dumps(config.hysteria.password)}"
    if config.hysteria.auth_type != "userpass":
        raise ValueError("Unsupported Hysteria authentication mode")
    users = {
        f"client-{client.id}": client.hysteria_password
        for client in config.enabled_clients
    }
    if not users:
        # Hysteria rejects an empty userpass map. An always-failing command
        # keeps the listener healthy while denying every authentication attempt.
        return "auth:\n  type: command\n  command: /bin/false"
    # JSON flow mappings are valid YAML and safely escape credential contents.
    return f"auth:\n  type: userpass\n  userpass: {json.dumps(users, sort_keys=True)}"
