import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

import pytest

from app.db import create_operation, get_latest_operation, get_setting, init_db, set_setting
from app.ip_change import run_ip_change


class FakeAezaClient:
    reboot_called = False
    events: list[str] = []

    def __init__(self, api_base: str, token: str) -> None:
        self.api_base = api_base
        self.token = token

    async def get_ipv4_list(self, service_id: str) -> list[dict[str, object]]:
        return [
            {"id": "old-ip-id", "ip": "203.0.113.10", "is_main": True},
            {"id": "new-ip-id", "ip": "203.0.113.20", "is_main": False},
        ]

    async def add_ipv4(self, service_id: str, payment_method: str, domain: str) -> dict[str, object]:
        return {"id": "new-ip-id", "ip": "203.0.113.20"}

    async def make_main_ipv4(self, service_id: str, ipv4_id: str) -> dict[str, object]:
        self.__class__.events.append("make_main")
        return {}

    async def reboot_service(self, service_id: str) -> dict[str, object]:
        self.__class__.reboot_called = True
        raise AssertionError("reboot should not be required after IP change")

    async def delete_ipv4(self, service_id: str, ipv4_id: str) -> dict[str, object]:
        self.__class__.events.append("delete_old")
        return {}


class DeleteFailsAezaClient(FakeAezaClient):
    async def delete_ipv4(self, service_id: str, ipv4_id: str) -> dict[str, object]:
        raise RuntimeError("delete failed")


@pytest.mark.anyio
async def test_ip_change_skips_reboot_and_waits_two_minutes(monkeypatch) -> None:
    init_db()
    FakeAezaClient.events = []
    FakeAezaClient.reboot_called = False
    set_setting("config.aeza_token", "token")
    set_setting("config.aeza_service_id", "service-id")
    set_setting("config.aeza_ipv4_payment_method", "balance")
    set_setting("config.aeza_ipv4_domain", "auto-vpn")
    set_setting("current_ip", "203.0.113.10")
    operation_id = create_operation()
    sleep_calls: list[int] = []

    async def fake_sleep(seconds: int) -> None:
        sleep_calls.append(seconds)

    async def fake_wait_for_health(host: str) -> dict[str, bool]:
        return {"ok": True}

    async def fake_sync_vpn_after_ip_change(host: str) -> None:
        return None

    async def fake_refresh_protocol_statuses(host: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr("app.ip_change.AezaClient", FakeAezaClient)
    monkeypatch.setattr("app.ip_change.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.ip_change._wait_for_health", fake_wait_for_health)
    monkeypatch.setattr("app.ip_change._sync_vpn_after_ip_change", fake_sync_vpn_after_ip_change)
    monkeypatch.setattr("app.ip_change.refresh_protocol_statuses", fake_refresh_protocol_statuses)

    await run_ip_change(operation_id)

    assert FakeAezaClient.reboot_called is False
    assert FakeAezaClient.events == ["make_main", "delete_old"]
    assert 120 in sleep_calls
    assert 30 not in sleep_calls
    assert get_setting("current_ip") == "203.0.113.20"


@pytest.mark.anyio
async def test_ip_change_stays_done_when_old_ip_cleanup_fails(monkeypatch) -> None:
    init_db()
    set_setting("config.aeza_token", "token")
    set_setting("config.aeza_service_id", "service-id")
    set_setting("config.aeza_ipv4_payment_method", "balance")
    set_setting("config.aeza_ipv4_domain", "auto-vpn")
    set_setting("current_ip", "203.0.113.10")
    operation_id = create_operation()

    async def fake_sleep(seconds: int) -> None:
        return None

    async def fake_wait_for_health(host: str) -> dict[str, bool]:
        return {"ok": True}

    async def fake_sync_vpn_after_ip_change(host: str) -> None:
        return None

    async def fake_refresh_protocol_statuses(host: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr("app.ip_change.AezaClient", DeleteFailsAezaClient)
    monkeypatch.setattr("app.ip_change.asyncio.sleep", fake_sleep)
    monkeypatch.setattr("app.ip_change._wait_for_health", fake_wait_for_health)
    monkeypatch.setattr("app.ip_change._sync_vpn_after_ip_change", fake_sync_vpn_after_ip_change)
    monkeypatch.setattr("app.ip_change.refresh_protocol_statuses", fake_refresh_protocol_statuses)

    await run_ip_change(operation_id)

    operation = get_latest_operation()
    assert operation is not None
    assert operation["status"] == "DONE"
    assert operation["current_step"] == "done"
    assert "old IPv4 cleanup failed" in operation["error_message"]
    assert get_setting("current_ip") == "203.0.113.20"
