from __future__ import annotations

import asyncio
import json
from typing import Any

from app.aeza import AezaClient
from app.config import settings
from app.db import get_setting, mark_vpn_config_updated, now_iso, set_setting, update_operation
from app.eu_install import build_eu_install_script, run_remote_command
from app.health import check_vpn_health
from app.protocol_status import refresh_protocol_statuses
from app.runtime_config import (
    aeza_api_base,
    aeza_ipv4_after_purchase_delay_seconds,
    aeza_ipv4_domain,
    aeza_ipv4_payment_method,
    aeza_service_id,
    aeza_token,
    hysteria_port,
    vless_port,
)


class IpChangeError(RuntimeError):
    pass


def _find_main_ip(ipv4_list: list[dict[str, Any]], current_ip: str) -> dict[str, Any]:
    for item in ipv4_list:
        if item.get("is_main"):
            return item
    for item in ipv4_list:
        if current_ip and item.get("ip") == current_ip:
            return item
    raise IpChangeError("Cannot find current main IPv4")


def _find_new_ip(
    ipv4_list: list[dict[str, Any]],
    old_ip_id: str,
    old_ip: str,
    created_ip_id: str,
) -> dict[str, Any] | None:
    for item in ipv4_list:
        if created_ip_id and item.get("id") == created_ip_id:
            return item
    for item in ipv4_list:
        if item.get("id") != old_ip_id and item.get("ip") != old_ip:
            return item
    return None


async def run_ip_change(operation_id: int) -> None:
    client = AezaClient(aeza_api_base(), aeza_token())
    service_id = aeza_service_id()
    old_ip_id = ""
    old_ip = ""
    old_ip_deleted = False

    try:
        if not service_id:
            raise IpChangeError("AEZA_SERVICE_ID is not configured")

        await _step(operation_id, "get_ipv4_list")
        ipv4_list = await client.get_ipv4_list(service_id)

        await _step(operation_id, "find_current_main_ip")
        current_ip = get_setting("current_ip")
        main_ip = _find_main_ip(ipv4_list, current_ip)
        old_ip = main_ip["ip"]
        old_ip_id = main_ip["id"]
        update_operation(operation_id, old_ip=old_ip, old_ip_id=old_ip_id)

        await _step(operation_id, "create_new_ipv4")
        created_ip = await client.add_ipv4(
            service_id,
            payment_method=aeza_ipv4_payment_method(),
            domain=aeza_ipv4_domain(),
        )
        created_ip_id = created_ip.get("id", "")

        await _step(operation_id, "wait_after_ipv4_purchase")
        await asyncio.sleep(aeza_ipv4_after_purchase_delay_seconds())

        await _step(operation_id, "wait_new_ipv4")
        new_ip = await _wait_for_new_ip(client, service_id, old_ip_id, old_ip, created_ip_id)
        new_ip_id = new_ip["id"]
        new_ip_address = new_ip["ip"]
        update_operation(operation_id, new_ip=new_ip_address, new_ip_id=new_ip_id)

        await _step(operation_id, "make_new_ipv4_main")
        await client.make_main_ipv4(service_id, new_ip_id)

        await _step(operation_id, "wait_vps_health")
        health_result = await _wait_for_health(new_ip_address)
        update_operation(
            operation_id,
            current_step="server_reachable",
            server_reachable_at=now_iso(),
            healthcheck_result=json.dumps(health_result, ensure_ascii=False),
        )

        await _step(operation_id, "update_current_ip")
        set_setting("current_ip", new_ip_address)
        set_setting("last_healthcheck_status", "OK")

        await _step(operation_id, "sync_vpn_after_ip_change")
        await _sync_vpn_after_ip_change(new_ip_address)
        mark_vpn_config_updated()
        await _step(operation_id, "refresh_protocol_statuses")
        await refresh_protocol_statuses(new_ip_address)

        cleanup_warning = ""
        try:
            await _step(operation_id, "delete_old_ipv4")
            await client.delete_ipv4(service_id, old_ip_id)
            old_ip_deleted = True
        except Exception as exc:
            cleanup_warning = (
                f"IP was changed to {new_ip_address}, but old IPv4 cleanup failed: {exc}. "
                f"Old IP was preserved: {old_ip} ({old_ip_id})"
            )

        update_operation(
            operation_id,
            status="DONE",
            current_step="done",
            error_message=cleanup_warning,
        )
    except Exception as exc:
        message = str(exc)
        if old_ip_id and not old_ip_deleted:
            message = f"{message}. Old IP was preserved: {old_ip} ({old_ip_id})"
        update_operation(
            operation_id,
            status="FAILED",
            current_step="failed",
            error_message=message,
        )
        set_setting("last_healthcheck_status", "FAILED")
        raise


async def _step(operation_id: int, step: str) -> None:
    update_operation(operation_id, status="RUNNING", current_step=step)
    await asyncio.sleep(0)


async def _wait_for_new_ip(
    client: AezaClient,
    service_id: str,
    old_ip_id: str,
    old_ip: str,
    created_ip_id: str,
) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + settings.ip_appear_timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        ipv4_list = await client.get_ipv4_list(service_id)
        new_ip = _find_new_ip(ipv4_list, old_ip_id, old_ip, created_ip_id)
        if new_ip and new_ip.get("id") and new_ip.get("ip"):
            return new_ip
        await asyncio.sleep(settings.ip_appear_interval_seconds)
    raise IpChangeError("New IPv4 did not appear in Aeza list before timeout")


async def _wait_for_health(host: str) -> dict[str, bool]:
    deadline = asyncio.get_running_loop().time() + settings.healthcheck_timeout_seconds
    last_result: dict[str, bool] | None = None
    while asyncio.get_running_loop().time() < deadline:
        last_result = await check_vpn_health(
            host,
            ssh_port=settings.ssh_port,
            vless_port=vless_port(),
            hysteria_port=hysteria_port(),
        )
        if last_result["ok"]:
            return last_result
        await asyncio.sleep(settings.healthcheck_interval_seconds)
    raise IpChangeError(f"Healthcheck failed before timeout: {last_result}")


async def _sync_vpn_after_ip_change(host: str) -> None:
    exit_code, output = await run_remote_command(host, "bash -s", stdin_data=build_eu_install_script())
    if exit_code != 0:
        raise IpChangeError(
            f"VPN config sync failed after IP change with exit code {exit_code}: {output[-4000:]}"
        )


async def apply_manual_main_ip(new_ip: str) -> None:
    set_setting("current_ip", new_ip)
    await _sync_vpn_after_ip_change(new_ip)
    mark_vpn_config_updated()
    await refresh_protocol_statuses(new_ip)
