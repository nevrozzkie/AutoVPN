from __future__ import annotations

from typing import Any


def _response_text(response: Any) -> str:
    if not response.content:
        return ""
    try:
        return response.text.strip()
    except UnicodeDecodeError:
        return response.content.decode("utf-8", errors="replace").strip()


def aeza_api_key(token: str) -> str:
    value = token.strip()
    header_name = value.split(":", 1)[0].strip().lower()
    if header_name in {"authorization", "x-api-key"}:
        value = value.split(":", 1)[1].strip()
    scheme = value.split(" ", 1)[0].lower()
    if scheme in {"bearer", "basic", "token"}:
        return value.split(" ", 1)[1].strip()
    return value


class AezaClient:
    def __init__(self, api_base: str, token: str, timeout: float = 30.0) -> None:
        self.api_base = api_base.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self, *, has_json_body: bool = False) -> dict[str, str]:
        headers = {
            "X-API-Key": aeza_api_key(self.token),
            "Accept": "application/json",
        }
        if has_json_body:
            headers["Content-Type"] = "application/json"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> Any:
        import httpx

        if not self.token:
            raise RuntimeError("AEZA_TOKEN is not configured")
        url = f"{self.api_base}{path}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                url,
                headers=self._headers(has_json_body=json is not None),
                json=json,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    raise RuntimeError(
                        "Aeza rejected AEZA_TOKEN with 401 Unauthorized. "
                        "Update AEZA_TOKEN on Setup; you can paste either the raw token or the full X-API-Key header."
                    ) from None
                detail = _response_text(exc.response)
                message = f"Aeza API returned {exc.response.status_code} {exc.response.reason_phrase}"
                if detail:
                    message = f"{message}: {detail}"
                raise RuntimeError(message) from None
            if response.content:
                return response.json()
            return None

    async def get_ipv4_list(self, service_id: str) -> list[dict[str, Any]]:
        data = await self._request("GET", f"/api/v2/services/{service_id}/networks/ipv4")
        return normalize_ipv4_list(data)

    async def get_ipv4_price(self, service_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/api/v2/services/{service_id}/networks/ipv4/price")
        return normalize_ipv4_price(data)

    async def get_service(self, service_id: str) -> dict[str, Any]:
        data = await self._request("GET", f"/api/v2/services/{service_id}")
        return normalize_service(data)

    async def add_ipv4(
        self,
        service_id: str,
        *,
        payment_method: str = "balance",
        domain: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"method": payment_method}
        if domain:
            payload["domain"] = domain
        data = await self._request(
            "POST",
            f"/api/v2/services/{service_id}/networks/ipv4",
            json=payload,
        )
        return normalize_ipv4(data)

    async def make_main_ipv4(self, service_id: str, ipv4_id: str) -> Any:
        return await self._request(
            "POST",
            f"/api/v2/services/{service_id}/networks/ipv4/{ipv4_id}/make-main",
        )

    async def delete_ipv4(self, service_id: str, ipv4_id: str) -> Any:
        return await self._request(
            "DELETE",
            f"/api/v2/services/{service_id}/networks/ipv4",
            json={"key": int(ipv4_id) if ipv4_id.isdecimal() else ipv4_id},
        )

    async def reboot_service(self, service_id: str) -> Any:
        return await self._request("POST", f"/api/v2/services/{service_id}/reboot")


def _unwrap_payload(data: Any) -> Any:
    if isinstance(data, dict):
        for key in ("data", "result", "items", "list", "ipv4"):
            if key in data:
                return data[key]
    return data


def normalize_ipv4_list(data: Any) -> list[dict[str, Any]]:
    payload = _unwrap_payload(data)
    if isinstance(payload, list):
        return [normalize_ipv4(item) for item in payload]
    if isinstance(payload, dict):
        return [normalize_ipv4(item) for item in payload.values()]
    return []


def normalize_ipv4(data: Any) -> dict[str, Any]:
    payload = _unwrap_payload(data)
    if not isinstance(payload, dict):
        return {"id": "", "ip": "", "is_main": False, "raw": data}

    ip = (
        payload.get("ip")
        or payload.get("address")
        or payload.get("ipv4")
        or payload.get("value")
        or ""
    )
    ipv4_id = (
        payload.get("id")
        or payload.get("key")
        or payload.get("uuid")
        or payload.get("network_id")
        or ip
        or ""
    )
    is_main = bool(
        payload.get("is_main")
        or payload.get("main")
        or payload.get("isMain")
        or payload.get("primary")
    )
    return {
        "id": str(ipv4_id),
        "ip": str(ip),
        "is_main": is_main,
        "raw": payload,
    }


def normalize_service(data: Any) -> dict[str, Any]:
    payload = _unwrap_payload(data)
    if not isinstance(payload, dict):
        return {"ip": "", "provider_status": "", "raw": data}
    provider_status = ""
    for key in (
        "provider_status",
        "providerStatus",
        "status",
        "state",
        "server_status",
        "serverStatus",
        "power_status",
        "powerStatus",
    ):
        candidate = payload.get(key)
        if isinstance(candidate, (str, int, float)) and not isinstance(candidate, bool):
            provider_status = str(candidate)
            break
    return {
        "ip": str(payload.get("ip") or payload.get("address") or payload.get("ipv4") or ""),
        "provider_status": provider_status,
        "raw": payload,
    }


def normalize_ipv4_price(data: Any) -> dict[str, Any]:
    payload = _unwrap_payload(data)
    if not isinstance(payload, dict):
        return {"price": None, "protected_price": None, "term_limits": {}, "raw": data}
    return {
        "price": payload.get("price"),
        "protected_price": payload.get("protectedPrice") or payload.get("protected_price"),
        "term_limits": payload.get("termLimits") or payload.get("term_limits") or {},
        "raw": payload,
    }
