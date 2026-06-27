from __future__ import annotations

import os
from dataclasses import dataclass


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _get_csv(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    database_path: str = os.getenv("DATABASE_PATH", "./data/autovpn.sqlite3")

    admin_username: str = os.getenv("ADMIN_USERNAME", "admin")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")

    aeza_api_base: str = os.getenv("AEZA_API_BASE", "https://my.aeza.net")
    aeza_token: str = os.getenv("AEZA_TOKEN", "")
    aeza_service_id: str = os.getenv("AEZA_SERVICE_ID", "")
    aeza_ipv4_payment_method: str = os.getenv("AEZA_IPV4_PAYMENT_METHOD", "balance")
    aeza_ipv4_domain: str = os.getenv("AEZA_IPV4_DOMAIN", "")
    aeza_ipv4_after_purchase_delay_seconds: int = _get_int(
        "AEZA_IPV4_AFTER_PURCHASE_DELAY_SECONDS",
        300,
    )

    eu_ssh_host: str = os.getenv("EU_SSH_HOST", "")
    eu_ssh_user: str = os.getenv("EU_SSH_USER", "root")
    eu_ssh_port: int = _get_int("EU_SSH_PORT", 22)
    eu_ssh_key_path: str = os.getenv("EU_SSH_KEY_PATH", "")
    eu_ssh_password: str = os.getenv("EU_SSH_PASSWORD", "")
    ssh_connect_timeout_seconds: int = _get_int("SSH_CONNECT_TIMEOUT_SECONDS", 15)

    vless_port: int = _get_int("VLESS_PORT", 8443)
    vless_reality_target: str = os.getenv("VLESS_REALITY_TARGET", "ok.ru:443")
    vless_reality_server_names: list[str] = None  # type: ignore[assignment]
    vless_reality_server_name: str = os.getenv("VLESS_REALITY_SERVER_NAME", "ok.ru")
    vless_reality_fingerprint: str = os.getenv("VLESS_REALITY_FINGERPRINT", "firefox")
    vless_reality_spider_x: str = os.getenv("VLESS_REALITY_SPIDER_X", "/")
    hysteria_port: int = _get_int("HYSTERIA_PORT", 443)
    amnezia_port: int = _get_int("AMNEZIA_PORT", 51820)
    amnezia_network_prefix: str = os.getenv("AMNEZIA_NETWORK_PREFIX", "10.66.66")
    amnezia_dns: str = os.getenv("AMNEZIA_DNS", "1.1.1.1, 8.8.8.8")
    ssh_port: int = _get_int("SSH_PORT", 22)

    ip_appear_timeout_seconds: int = _get_int("IP_APPEAR_TIMEOUT_SECONDS", 300)
    ip_appear_interval_seconds: int = _get_int("IP_APPEAR_INTERVAL_SECONDS", 5)
    reboot_wait_seconds: int = _get_int("REBOOT_WAIT_SECONDS", 30)
    healthcheck_timeout_seconds: int = _get_int("HEALTHCHECK_TIMEOUT_SECONDS", 300)
    healthcheck_interval_seconds: int = _get_int("HEALTHCHECK_INTERVAL_SECONDS", 10)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "vless_reality_server_names",
            _get_csv("VLESS_REALITY_SERVER_NAMES", "ok.ru,www.ok.ru"),
        )


settings = Settings()
