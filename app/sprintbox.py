"""SprintBOX's documented form API (https://api.sprintbox.ru/).

Documented task IDs are distinct from box IDs. Never retry a mutation automatically.
"""
from __future__ import annotations

import ipaddress
from typing import Any

import httpx


class SprintboxApiError(RuntimeError):
    def __init__(self, code: str, description: str) -> None:
        self.code = code
        super().__init__(f"SprintBOX API {code}: {description}")


class SprintboxTaskIdMissing(RuntimeError):
    """The API accepted a mutation but supplied no usable task ID."""


class SprintboxClient:
    def __init__(self, login: str, key: str, timeout: float = 30.0) -> None:
        self.login = login
        self.key = key
        self.timeout = timeout

    async def _request(self, path: str, **params: str) -> Any:
        if not self.login or not self.key:
            raise RuntimeError("Configure SPRINTBOX_LOGIN and SPRINTBOX_KEY")
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
            response = await client.post(
                f"https://api.from.sh/sprintbox/{path}",
                headers={"AUTH-LOGIN": self.login, "AUTH-KEY": self.key},
                data=params,
            )
        # Never expose raw responses (box/show includes VNC credentials).
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(f"SprintBOX returned an invalid response (HTTP {response.status_code})") from None
        if not isinstance(payload, dict):
            raise RuntimeError("SprintBOX returned an invalid response")
        error = payload.get("error")
        if isinstance(error, dict) and error:
            description = str(error.get("description") or "Request rejected")
            for secret in (self.key, self.login):
                description = description.replace(secret, "[REDACTED]")
            raise SprintboxApiError(str(error.get("code", "unknown")), description[:500])
        if error or not response.is_success or "data" not in payload:
            raise RuntimeError(f"SprintBOX request failed (HTTP {response.status_code})")
        return payload["data"]

    async def list_boxes(self) -> dict[str, dict[str, Any]]:
        data = await self._request("boxes/list")
        # Empty collections may be encoded as [] by the provider's form API.
        if data == []:
            return {}
        if not isinstance(data, dict) or any(not isinstance(v, dict) for v in data.values()):
            raise RuntimeError("SprintBOX returned an invalid box list")
        return data

    async def get_box(self, name: str) -> dict[str, Any]:
        data = await self._request("box/show", box_name=name)
        if not isinstance(data, dict) or data.get("name") != name:
            raise RuntimeError("SprintBOX returned a different or invalid box")
        return data

    @staticmethod
    def task_id(data: Any) -> str:
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise SprintboxTaskIdMissing("SprintBOX did not return an asynchronous task ID")
        return data["id"]

    async def create_box(self, service: str, distributive: str, pubkey: str, description: str) -> str:
        return self.task_id(await self._request(
            "box/add", service=service, distributive=distributive, arch="x64",
            pubkey=pubkey, box_desc=description,
        ))

    async def delete_box(self, name: str) -> str:
        return self.task_id(await self._request("box/delete", box_name=name))

    async def task_status(self, task_id: str) -> str:
        data = await self._request("status/show", id=task_id)
        if not isinstance(data, dict) or data.get("id") != task_id:
            raise RuntimeError("SprintBOX returned an invalid task status")
        status = data.get("status")
        if not isinstance(status, str) or not status.strip():
            raise RuntimeError("SprintBOX returned an invalid task status")
        # The live API can report intermediate states absent from its docs.
        # They never mean success: keep polling within the caller's deadline.
        return status if status in {"started", "finished", "error"} else "pending"

    async def get_service(self, name: str) -> dict[str, Any]:
        box = await self.get_box(name)
        ips = box_ipv4(box)
        return {"ip": ips[0] if ips else "", "provider_status": str(box.get("state_description") or "")}

    async def reboot_service(self, name: str) -> str:
        return self.task_id(await self._request("box/restart", box_name=name))


def box_ipv4(box: dict[str, Any]) -> list[str]:
    values = box.get("ipv4")
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        try:
            address = ipaddress.IPv4Address(value)
        except (ValueError, TypeError, ipaddress.AddressValueError):
            continue
        result.append(str(address))
    return result
