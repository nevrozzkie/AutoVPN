from __future__ import annotations


def profile_name(client: dict, protocol: str) -> str:
    return f"[AutoVPN] {client['name']} - {protocol}"
