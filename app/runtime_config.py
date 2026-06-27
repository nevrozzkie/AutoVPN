from __future__ import annotations

from app.config import settings
from app.db import get_setting


def config_value(key: str, default: str = "") -> str:
    value = get_setting(f"config.{key}", "")
    if value:
        return value
    fallback = getattr(settings, key, default)
    if isinstance(fallback, list):
        return ",".join(fallback)
    return str(fallback) if fallback is not None else default


def config_int(key: str, default: int) -> int:
    value = config_value(key, "")
    if value == "":
        return default
    return int(value)


def config_csv(key: str, default: list[str]) -> list[str]:
    value = config_value(key, "")
    if not value:
        return default
    return [item.strip() for item in value.split(",") if item.strip()]


def admin_username() -> str:
    return config_value("admin_username", "admin") or "admin"


def admin_password() -> str:
    return config_value("admin_password", "")


def aeza_token() -> str:
    return config_value("aeza_token", "")


def aeza_service_id() -> str:
    return config_value("aeza_service_id", "")


def aeza_api_base() -> str:
    return config_value("aeza_api_base", "https://my.aeza.net")


def aeza_ipv4_payment_method() -> str:
    return config_value("aeza_ipv4_payment_method", "balance")


def aeza_ipv4_domain() -> str:
    return config_value("aeza_ipv4_domain", "")


def aeza_ipv4_after_purchase_delay_seconds() -> int:
    return config_int("aeza_ipv4_after_purchase_delay_seconds", 300)


def eu_ssh_host() -> str:
    return config_value("eu_ssh_host", "")


def eu_ssh_user() -> str:
    return config_value("eu_ssh_user", "root") or "root"


def eu_ssh_port() -> int:
    return config_int("eu_ssh_port", 22)


def eu_ssh_key_path() -> str:
    return config_value("eu_ssh_key_path", "")


def eu_ssh_password() -> str:
    return config_value("eu_ssh_password", "")


def ssh_connect_timeout_seconds() -> int:
    return config_int("ssh_connect_timeout_seconds", 15)


def vless_port() -> int:
    return config_int("vless_port", settings.vless_port)


def hysteria_port() -> int:
    return config_int("hysteria_port", settings.hysteria_port)


def amnezia_port() -> int:
    return config_int("amnezia_port", settings.amnezia_port)


def setup_complete() -> bool:
    return bool(admin_password())
