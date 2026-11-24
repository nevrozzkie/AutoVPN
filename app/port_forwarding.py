from __future__ import annotations

import ipaddress
import json
import sqlite3
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

from app.db import _set_setting, _setting, get_db, now_iso
from app.operation_coordinator import acquire_vps_lease, release_vps_lease
from app.runtime_config import eu_ssh_password
from app.secret_sanitization import sanitize_error
from app.vpn_config import CapturedVpnConfig, capture_vpn_config

CONFIG_KEY = "port_forwarding.config"
RESULT_KEY = "port_forwarding.result"
LEASE_OWNER = "PORT_FORWARDING"
REMOTE_SCRIPT = Path(__file__).with_name("port_forwarding_remote.py")
Port = Annotated[int, Field(ge=1, le=65535)]


class Rule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool
    protocol: Literal["UDP", "TCP", "UDP + TCP"]
    external: Port
    internal: Port
    client_id: PositiveInt | None
    device_id: PositiveInt | None


class Group(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
    enabled: bool
    rules: list[Rule] = Field(max_length=32)


class ForwardingConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: bool = False
    groups: list[Group] = Field(default_factory=list, max_length=32)


class ForwardingEdit(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    revision: int = Field(ge=0)
    config: ForwardingConfig


class PortForwardingError(ValueError):
    pass


def _load(db: sqlite3.Connection) -> ForwardingEdit:
    raw = _setting(db, CONFIG_KEY)
    return ForwardingEdit.model_validate_json(raw) if raw else ForwardingEdit(revision=0, config=ForwardingConfig())


def read_result(db: sqlite3.Connection, revision: int, ip: str) -> dict:
    raw = _setting(db, RESULT_KEY)
    if raw:
        result = json.loads(raw)
        if result["revision"] == revision and result["ip"] == ip:
            return result
    return {}


def write_result(db: sqlite3.Connection, result: dict) -> None:
    _set_setting(db, RESULT_KEY, json.dumps(result, ensure_ascii=False))


def _users(config: CapturedVpnConfig) -> list[dict]:
    devices = {}
    for device in config.amnezia_devices:
        devices.setdefault(device.client_id, []).append({
            "id": device.id, "name": device.name, "ip": device.ipv4,
        })
    return [
        {"id": client.id, "name": client.name, "devices": [
            {"id": None, "name": "Главный", "ip": client.amnezia_ipv4},
            *devices.get(client.id, []),
        ]}
        for client in config.enabled_clients
    ]


def view() -> dict:
    with get_db() as db:
        db.execute("BEGIN")
        saved = _load(db)
        vpn = capture_vpn_config(db)
        result = read_result(db, saved.revision, vpn.current_ip)
    return {**saved.model_dump(), "ip": vpn.current_ip, "users": _users(vpn), "result": result}


def _active(config: ForwardingConfig):
    if config.enabled:
        for group_index, group in enumerate(config.groups):
            if group.enabled:
                for rule_index, rule in enumerate(group.rules):
                    if rule.enabled:
                        yield f"{group_index}.{rule_index}", group.name, rule


def _ports(vpn: CapturedVpnConfig) -> dict[tuple[str, int], str]:
    return {
        (protocol, config.port): name
        for protocol, config, name in (
            ("TCP", vpn.vless.protocol, "VLESS"),
            ("UDP", vpn.hysteria.protocol, "Hysteria"),
            ("UDP", vpn.amnezia.protocol, "AmneziaWG"),
        )
        if config.enabled
    }


def _validate_ports(config: ForwardingConfig, vpn: CapturedVpnConfig) -> None:
    occupied = _ports(vpn)
    count = 0
    for _, group, rule in _active(config):
        for protocol in rule.protocol.split(" + "):
            count += 1
            if count > 64:
                raise PortForwardingError("Одновременно можно включить не больше 64 пробросов TCP/UDP.")
            endpoint = (protocol, rule.external)
            if endpoint in occupied:
                raise PortForwardingError(
                    f"«{group}»: {protocol} {rule.external} уже используется ({occupied[endpoint]})."
                )
            occupied[endpoint] = group


def _manifest(config: ForwardingConfig, vpn: CapturedVpnConfig) -> tuple[dict, dict[str, str]]:
    clients = {client.id: client for client in vpn.enabled_clients}
    devices = {(device.client_id, device.id): device for device in vpn.amnezia_devices}
    rules = []
    errors = {}
    occupied = _ports(vpn)
    for key, group, rule in _active(config):
        label = f"«{group}», {rule.protocol} {rule.external}"
        client = clients.get(rule.client_id)
        device = devices.get((rule.client_id, rule.device_id))
        protocols = rule.protocol.split(" + ")
        conflict = next((occupied[(protocol, rule.external)] for protocol in protocols
                         if (protocol, rule.external) in occupied), None)
        if not vpn.amnezia.protocol.enabled:
            errors[key] = f"{label}: AmneziaWG выключен."
        elif not client or (rule.device_id is not None and not device):
            errors[key] = f"{label}: устройство отсутствует или выключено."
        elif conflict:
            errors[key] = f"{label}: внешний порт уже используется ({conflict})."
        else:
            address = device.ipv4 if device else client.amnezia_ipv4
            public_key = device.public_key if device else client.amnezia_public_key
            try:
                address = str(ipaddress.IPv4Address(address))
            except ipaddress.AddressValueError:
                errors[key] = f"{label}: у устройства нет туннельного IPv4."
                continue
            if not public_key:
                errors[key] = f"{label}: у устройства нет публичного ключа AmneziaWG."
                continue
            for protocol in protocols:
                occupied[(protocol, rule.external)] = group
                rules.append({
                    "id": key, "label": label, "protocol": protocol.lower(),
                    "external": rule.external, "internal": rule.internal,
                    "client_ip": address, "peer": public_key,
                })
    return {"public_ip": vpn.current_ip, "server_key": vpn.amnezia.server_public_key, "rules": rules}, errors


def _script(manifest: dict) -> str:
    # JSON escapes line breaks in user-controlled labels. Neither heredoc is evaluated by the shell.
    return f"""set -eu
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cat >"$work/remote.py" <<'AUTOVPN_PORT_FORWARDING_PY'
{REMOTE_SCRIPT.read_text()}
AUTOVPN_PORT_FORWARDING_PY
cat >"$work/manifest.json" <<'AUTOVPN_PORT_FORWARDING_JSON'
{json.dumps(manifest, ensure_ascii=True)}
AUTOVPN_PORT_FORWARDING_JSON
python3 "$work/remote.py" apply "$work/manifest.json"
"""


async def reconcile(
    host: str, vpn: CapturedVpnConfig, *, strict: bool = False, new_box: bool = False,
) -> dict | None:
    from app.eu_install import run_remote_command

    with get_db() as db:
        saved = _load(db)
    if not saved.revision:
        return None
    result = {"revision": saved.revision, "ip": vpn.current_ip, "checked_at": now_iso(),
              "state": "ERROR", "error": "", "rules": {}}
    try:
        manifest, errors = _manifest(saved.config, vpn)
        result["rules"] = errors
        if strict and errors:
            raise PortForwardingError(next(iter(errors.values())))
        if manifest["rules"] or not new_box:
            try:
                ipaddress.IPv4Address(vpn.current_ip)
            except ipaddress.AddressValueError:
                raise PortForwardingError("Для VPN-бокса не настроен публичный IPv4.") from None
            if not host:
                raise PortForwardingError("SSH-адрес VPN-бокса не настроен.")
            code, output = await run_remote_command(host, "bash -s", _script(manifest), timeout=60)
            if code or "AUTOVPN_PORT_FORWARDING_OK" not in output.splitlines():
                raise PortForwardingError(output.strip() or f"Установка правил завершилась с кодом {code}.")
        result["state"] = "PARTIAL" if errors else "APPLIED"
    except Exception as exc:
        result["error"] = sanitize_error(exc, eu_ssh_password(), limit=1200)
        if strict:
            raise PortForwardingError(f"Проброс портов: {result['error']}") from exc

    return result


def record_current_result(result: dict | None) -> None:
    if result is None:
        return
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        if _load(db).revision == result["revision"] and _setting(db, "current_ip") == result["ip"]:
            write_result(db, result)


async def save_and_apply(edit: ForwardingEdit) -> dict:
    from app.eu_install import resolve_eu_host

    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = _load(db)
        if previous.revision != edit.revision:
            raise PortForwardingError("Настройки уже изменились. Обновите страницу и повторите.")
        vpn = capture_vpn_config(db)
        _validate_ports(edit.config, vpn)
        revision = previous.revision + 1
        acquire_vps_lease(db, LEASE_OWNER, revision)
        _set_setting(db, CONFIG_KEY, edit.model_copy(update={"revision": revision}).model_dump_json())
    try:
        result = await reconcile(resolve_eu_host(), vpn, new_box=not previous.revision)
        record_current_result(result)
    finally:
        with get_db() as db:
            release_vps_lease(db, LEASE_OWNER, revision)
    return view()
