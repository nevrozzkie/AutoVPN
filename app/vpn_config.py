from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from typing import Any

from app.config import settings


@dataclass(frozen=True)
class ProtocolConfig:
    enabled: bool
    port: int


@dataclass(frozen=True)
class VlessRealityConfig:
    protocol: ProtocolConfig
    private_key: str
    public_key: str
    short_id: str
    target: str
    server_names: tuple[str, ...]
    server_name: str
    fingerprint: str
    spider_x: str


@dataclass(frozen=True)
class HysteriaConfig:
    protocol: ProtocolConfig
    password: str
    obfs_password: str


@dataclass(frozen=True)
class AmneziaObfuscation:
    jc: int
    jmin: int
    jmax: int
    s1: int
    s2: int
    h1: int
    h2: int
    h3: int
    h4: int

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


@dataclass(frozen=True)
class AmneziaConfig:
    protocol: ProtocolConfig
    server_private_key: str
    server_public_key: str
    obfuscation: AmneziaObfuscation
    network_prefix: str
    dns: str


@dataclass(frozen=True)
class VpnClient:
    id: int
    name: str
    token: str
    enabled: bool
    vless_uuid: str
    hysteria_password: str
    amnezia_private_key: str
    amnezia_public_key: str
    amnezia_preshared_key: str
    amnezia_ipv4: str
    created_at: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapturedVpnConfig:
    current_ip: str
    config_updated_at: str
    vless: VlessRealityConfig
    hysteria: HysteriaConfig
    amnezia: AmneziaConfig
    clients: tuple[VpnClient, ...]

    @property
    def enabled_clients(self) -> tuple[VpnClient, ...]:
        return tuple(client for client in self.clients if client.enabled)

    def client_by_token(self, token: str) -> VpnClient | None:
        return next((client for client in self.clients if client.token == token), None)


def _configured_value(values: dict[str, str], key: str, default: object) -> str:
    value = values.get(f"config.{key}")
    if value:
        return value
    if isinstance(default, list):
        return ",".join(default)
    return str(default) if default is not None else ""


def _protocol(values: dict[str, str], name: str, default_port: int) -> ProtocolConfig:
    configured_enabled = values.get(f"config.{name}_enabled")
    if configured_enabled is not None:
        enabled = configured_enabled.strip().lower() in {"1", "true", "yes", "on"}
    else:
        enabled = values.get(f"config.{name}_port") != ""
    raw_port = values.get(f"config.{name}_port")
    port = default_port if raw_port in (None, "") else int(raw_port)
    return ProtocolConfig(enabled=enabled, port=port)


def _capture(connection: sqlite3.Connection) -> CapturedVpnConfig:
    setting_rows = connection.execute("SELECT key, value FROM settings").fetchall()
    values = {str(row["key"]): str(row["value"]) for row in setting_rows}
    client_rows = connection.execute("SELECT * FROM clients ORDER BY id DESC").fetchall()

    clients = tuple(
        VpnClient(
            id=int(row["id"]),
            name=str(row["name"]),
            token=str(row["token"]),
            enabled=bool(row["enabled"]),
            vless_uuid=str(row["vless_uuid"]),
            hysteria_password=str(row["hysteria_password"]),
            amnezia_private_key=str(row.get("amnezia_private_key") or ""),
            amnezia_public_key=str(row.get("amnezia_public_key") or ""),
            amnezia_preshared_key=str(row.get("amnezia_preshared_key") or ""),
            amnezia_ipv4=str(row.get("amnezia_ipv4") or ""),
            created_at=str(row["created_at"]),
        )
        for row in client_rows
    )
    server_names = tuple(
        item.strip()
        for item in _configured_value(
            values,
            "vless_reality_server_names",
            settings.vless_reality_server_names,
        ).split(",")
        if item.strip()
    )
    return CapturedVpnConfig(
        current_ip=values.get("current_ip", ""),
        config_updated_at=values.get("vpn.config_updated_at", ""),
        vless=VlessRealityConfig(
            protocol=_protocol(values, "vless", settings.vless_port),
            private_key=values.get("vless.reality_private_key", ""),
            public_key=values.get("vless.reality_public_key", ""),
            short_id=values.get("vless.reality_short_id", ""),
            target=_configured_value(
                values, "vless_reality_target", settings.vless_reality_target
            ),
            server_names=server_names,
            server_name=_configured_value(
                values,
                "vless_reality_server_name",
                settings.vless_reality_server_name,
            ),
            fingerprint=_configured_value(
                values,
                "vless_reality_fingerprint",
                settings.vless_reality_fingerprint,
            ),
            spider_x=_configured_value(
                values, "vless_reality_spider_x", settings.vless_reality_spider_x
            ),
        ),
        hysteria=HysteriaConfig(
            protocol=_protocol(values, "hysteria", settings.hysteria_port),
            password=values.get("hysteria.password", ""),
            obfs_password=values.get("hysteria.obfs_password", ""),
        ),
        amnezia=AmneziaConfig(
            protocol=_protocol(values, "amnezia", settings.amnezia_port),
            server_private_key=values.get("amnezia.server_private_key", ""),
            server_public_key=values.get("amnezia.server_public_key", ""),
            obfuscation=AmneziaObfuscation(
                jc=int(values.get("amnezia.jc", "5")),
                jmin=int(values.get("amnezia.jmin", "40")),
                jmax=int(values.get("amnezia.jmax", "1000")),
                s1=int(values.get("amnezia.s1", "64")),
                s2=int(values.get("amnezia.s2", "128")),
                h1=int(values.get("amnezia.h1", "1")),
                h2=int(values.get("amnezia.h2", "2")),
                h3=int(values.get("amnezia.h3", "3")),
                h4=int(values.get("amnezia.h4", "4")),
            ),
            network_prefix=_configured_value(
                values, "amnezia_network_prefix", settings.amnezia_network_prefix
            ),
            dns=_configured_value(values, "amnezia_dns", settings.amnezia_dns),
        ),
        clients=clients,
    )


def capture_vpn_config(
    connection: sqlite3.Connection | None = None,
) -> CapturedVpnConfig:
    """Read all VPN inputs from one SQLite snapshot.

    Callers that pass a connection own its surrounding transaction. The normal
    path opens an explicit read transaction before either source table is read.
    """
    if connection is not None:
        return _capture(connection)

    # Import lazily to keep db key-generation helpers independent of this model.
    from app.db import get_db

    with get_db() as database:
        database.execute("BEGIN")
        return _capture(database)
