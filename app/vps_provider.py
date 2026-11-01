from __future__ import annotations

import hashlib
import json
from typing import Any

from app.config import settings
from app.db import get_setting
from app.runtime_config import config_value
from app.sprintbox import SprintboxClient


def provider_name() -> str:
    return config_value("vps_provider", "aeza")


def sprintbox_config() -> dict[str, str]:
    config = {key: config_value(f"sprintbox_{key}") for key in ("login", "key", "box_name", "service", "distributive")}
    # Explicitly clearing the box selects first-time creation, even with an env fallback.
    configured = get_setting("config.sprintbox_box_name", None)
    if configured is not None:
        config["box_name"] = configured
    return config


def provider_available() -> bool:
    if provider_name() == "sprintbox":
        config = sprintbox_config()
        return bool(config["login"] and config["key"] and config["box_name"])
    return provider_name() == "aeza" and bool(config_value("aeza_token") and config_value("aeza_service_id"))


def ip_rotation_available() -> bool:
    if provider_name() == "sprintbox":
        config = sprintbox_config()
        return all(config[key] for key in ("login", "key", "service", "distributive"))
    return provider_available()


def first_sprintbox_box() -> bool:
    return provider_name() == "sprintbox" and not sprintbox_config()["box_name"]


def provider_service_id() -> str:
    return sprintbox_config()["box_name"] if provider_name() == "sprintbox" else config_value("aeza_service_id")


def sprintbox_client(timeout: float = 30.0) -> SprintboxClient:
    config = sprintbox_config()
    return SprintboxClient(config["login"], config["key"], timeout=timeout)


def bind_provider(db: Any, operation_id: int) -> None:
    """Bind non-secret parameters and credential fingerprint in the prepare transaction."""
    def value(key: str, default: str = "") -> str:
        row = db.execute("SELECT value FROM settings WHERE key = ?", (f"config.{key}",)).fetchone()
        if row and key == "sprintbox_box_name":
            return str(row["value"])
        return str(row["value"] if row and row["value"] else getattr(settings, key, default))

    provider = value("vps_provider", "aeza")
    if provider not in {"aeza", "sprintbox"}:
        raise ValueError("Unknown VPS provider; configure Setup before rotating IP")
    data = {"provider": provider}
    if provider == "sprintbox":
        data.update({key: value(f"sprintbox_{key}") for key in ("login", "box_name", "service", "distributive")})
        data["credential_hash"] = hashlib.sha256(value("sprintbox_key").encode()).hexdigest()
    db.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (
        f"ip_change.{operation_id}.provider", json.dumps(data),
    ))


def bound_provider(operation_id: int) -> dict[str, str]:
    from app.db import get_setting
    return json.loads(get_setting(f"ip_change.{operation_id}.provider", '{"provider":"aeza"}'))
