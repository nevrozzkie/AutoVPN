"""Validated protocol tuning, sharing the normal desired/applied deployment path."""
from __future__ import annotations

import json
import re
import secrets

from app.amnezia import generate_obfuscation_settings
from app.db import get_db, _active_operation_owner, _mark_vpn_config_updated, _set_setting
from app.operation_coordinator import OperationBusyError
from app.runtime_config import eu_ssh_port
from app.vpn_config import capture_vpn_config

FINGERPRINTS = ("firefox", "chrome", "safari", "ios", "android", "edge")
PROTOCOLS = ("vless", "hysteria", "amnezia")
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,63}\Z")


def profile_values(config, protocol: str) -> dict[str, str]:
    values = {"port": str(getattr(config, protocol).protocol.port)}
    if protocol == "vless":
        values.update(target=config.vless.target, server_names=",".join(config.vless.server_names),
                      server_name=config.vless.server_name, fingerprint=config.vless.fingerprint,
                      short_id=config.vless.short_id, spider_x=config.vless.spider_x)
    elif protocol == "hysteria":
        values["obfs_password"] = config.hysteria.obfs_password
    else:
        values.update({key: str(value) for key, value in config.amnezia.obfuscation.as_dict().items()})
    return values


def _integer(values, key: str, minimum: int, maximum: int) -> int:
    raw = values.get(key, "")
    if not re.fullmatch(r"[0-9]{1,10}", raw):
        raise ValueError(f"{key}: введите целое число от {minimum} до {maximum}")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{key}: допустимо от {minimum} до {maximum}")
    return value


def _validate_port(protocol: str, values: dict[str, str], config) -> int:
    port = _integer(values, "port", 1, 65535)
    if port in {22, eu_ssh_port()} or any(port == getattr(config, name).protocol.port for name in PROTOCOLS if name != protocol):
        raise ValueError("Порт занят другим протоколом или SSH")
    return port


def _original_settings(protocol: str, values: dict[str, str]) -> dict[str, str]:
    result = {f"config.{protocol}_port": values["port"]}
    for key, value in values.items():
        if key == "port":
            continue
        if protocol == "vless":
            setting = "vless.reality_short_id" if key == "short_id" else f"config.vless_reality_{key}"
        else:
            setting = f"{protocol}.{key}"
        result[setting] = value
    return result


def validate_values(protocol: str, values: dict[str, str], config) -> dict[str, str]:
    port = _validate_port(protocol, values, config)
    result = {f"config.{protocol}_port": str(port)}
    if protocol == "vless":
        target = values.get("target", "")
        host, sep, target_port = target.rpartition(":")
        if not sep or not DOMAIN.fullmatch(host):
            raise ValueError("REALITY target: укажите домен:порт, например ok.ru:443")
        _integer({"port": target_port}, "port", 1, 65535)
        names = [name.strip() for name in values.get("server_names", "").split(",")]
        if not 1 <= len(names) <= 16 or any(not DOMAIN.fullmatch(name) for name in names):
            raise ValueError("serverNames: укажите до 16 доменов через запятую")
        if values.get("server_name") not in names:
            raise ValueError("SNI должен входить в serverNames и поддерживаться REALITY target")
        if values.get("fingerprint") not in FINGERPRINTS:
            raise ValueError("Выберите поддерживаемый TLS fingerprint")
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,8}", values.get("short_id", "")):
            raise ValueError("short ID: от 2 до 16 шестнадцатеричных символов, чётная длина")
        path = values.get("spider_x", "")
        if not path.startswith("/") or len(path) > 128 or not re.fullmatch(r"/[a-zA-Z0-9/_.~-]*", path):
            raise ValueError("spiderX: путь от /, до 128 символов (буквы, цифры, /_.~-)")
        for key in ("target", "server_name", "fingerprint", "spider_x"):
            result[f"config.vless_reality_{key}"] = values[key]
        result["config.vless_reality_server_names"] = ",".join(names)
        result["vless.reality_short_id"] = values["short_id"].lower()
    elif protocol == "hysteria":
        password = values.get("obfs_password", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,128}", password):
            raise ValueError("Salamander: 8–128 латинских букв, цифр, _ или -")
        result["hysteria.obfs_password"] = password
    else:
        bounds = {"jc": (1, 128), "jmin": (1, 1280), "jmax": (1, 1280),
                  "s1": (0, 1024), "s2": (0, 1024),
                  **{f"h{i}": (0, 4294967295) for i in range(1, 5)}}
        parsed = {key: _integer(values, key, *limits) for key, limits in bounds.items()}
        if parsed["jmin"] >= parsed["jmax"]:
            raise ValueError("Jmin должен быть меньше Jmax")
        if parsed["s1"] + 148 == parsed["s2"] + 92:
            raise ValueError("Размеры handshake-пакетов совпадают: измените S1 или S2")
        if len({parsed[f"h{i}"] for i in range(1, 5)}) != 4:
            raise ValueError("H1–H4 должны отличаться друг от друга")
        result.update({f"amnezia.{key}": str(value) for key, value in parsed.items()})
    return result


def update_tuning(protocol: str, action: str, submitted: dict[str, str]) -> None:
    if protocol not in PROTOCOLS or action not in {"save", "random", "defaults"}:
        raise ValueError("Неизвестное действие или протокол")
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        owner = _active_operation_owner(db)
        if owner:
            raise OperationBusyError(owner)
        config = capture_vpn_config(db)
        current = profile_values(config, protocol)
        default_key = f"vpn.tuning_defaults.{protocol}"
        row = db.execute("SELECT value FROM settings WHERE key=?", (default_key,)).fetchone()
        defaults = json.loads(row["value"]) if row else current
        values = dict(current)
        if action == "defaults":
            values = defaults
        elif action == "save":
            values = submitted
        else:
            used_ports = {getattr(config, name).protocol.port for name in PROTOCOLS} | {22, eu_ssh_port()}
            port = secrets.randbelow(65535 - 1024) + 1024
            while port in used_ports:
                port = secrets.randbelow(65535 - 1024) + 1024
            values["port"] = str(port)
            if protocol == "vless":
                values.update(short_id=secrets.token_hex(8), fingerprint=secrets.choice(FINGERPRINTS[:3]),
                              spider_x="/" + secrets.token_hex(6))
            elif protocol == "hysteria":
                values["obfs_password"] = secrets.token_urlsafe(24)
            else:
                values.update({key: str(value) for key, value in generate_obfuscation_settings().items()})
        if action == "defaults":
            _validate_port(protocol, values, config)
            # Restore the exact pre-existing profile, including legacy values
            # outside today's conservative editing limits. It came from our
            # captured configuration, never from the submitted form.
            updates = _original_settings(protocol, values)
        else:
            updates = validate_values(protocol, values, config)
        # Save the original working profile once; random never replaces defaults.
        if not row:
            _set_setting(db, default_key, json.dumps(current))
        changed = False
        for key, value in updates.items():
            old = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            changed = changed or old is None or old["value"] != value
            _set_setting(db, key, value)
        if changed:
            _mark_vpn_config_updated(db, public_change=True)
