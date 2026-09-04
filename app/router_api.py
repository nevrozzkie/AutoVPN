from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.amnezia import render_amnezia_vpn_key
from app.config import settings
from app.db import get_db
from app.router_credentials import (
    AuthenticatedRouterCredential,
    RouterAuthenticationFailure,
    authenticate_router_credential,
)
from app.vpn_config import CapturedVpnConfig, VpnClient, captured_vpn_config_from_json


router = APIRouter(prefix="/api/v2/router", tags=["router-api-v2"])
MAX_AUTHORIZATION_HEADER = 512
MAX_CONDITIONAL_HEADER = 256
API_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "Vary": "Authorization",
}


@dataclass
class RouterApiError(Exception):
    status_code: int
    code: str
    message: str
    headers: dict[str, str] | None = None
    extra: dict[str, Any] | None = None


def router_api_error_response(_: Request, exc: RouterApiError) -> JSONResponse:
    error: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.extra:
        error.update(exc.extra)
    headers = {**API_RESPONSE_HEADERS, **(exc.headers or {})}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": error},
        headers=headers,
    )


def _require_enabled() -> None:
    if not settings.enable_router_api:
        raise RouterApiError(404, "not_found", "Resource not found")


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    if not authorization or len(authorization) > MAX_AUTHORIZATION_HEADER:
        raise RouterApiError(
            401,
            "invalid_token",
            "Bearer authentication required",
            {"WWW-Authenticate": 'Bearer realm="router-api"'},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token or " " in token:
        raise RouterApiError(
            401,
            "invalid_token",
            "Bearer authentication required",
            {"WWW-Authenticate": 'Bearer realm="router-api"'},
        )
    return token


def authorize_router(request: Request, required_scope: str) -> AuthenticatedRouterCredential:
    _require_enabled()
    result = authenticate_router_credential(_bearer_token(request), required_scope)
    if isinstance(result, RouterAuthenticationFailure):
        if result.code == "invalid_token":
            raise RouterApiError(
                401,
                "invalid_token",
                "Bearer authentication required",
                {"WWW-Authenticate": 'Bearer realm="router-api"'},
            )
        messages = {
            "credential_forbidden": "Router credential is disabled or revoked",
            "client_forbidden": "Router credential client is disabled",
            "insufficient_scope": "Router credential lacks the required scope",
        }
        raise RouterApiError(403, result.code, messages[result.code])
    return result


def _load_applied_snapshot() -> tuple[dict[str, Any], CapturedVpnConfig, str | None]:
    with get_db() as db:
        db.execute("BEGIN")
        state = db.execute(
            "SELECT applied_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone()
        applied_revision = state["applied_revision"] if state else None
        if applied_revision is None:
            raise RouterApiError(
                503,
                "snapshot_not_ready",
                "No applied VPN snapshot is available",
                {"Retry-After": "30"},
            )
        snapshot = db.execute(
            """
            SELECT revision, payload_json, payload_sha256, prepared_at, applied_at
            FROM vpn_snapshots
            WHERE revision = ? AND lifecycle = 'APPLIED'
            """,
            (applied_revision,),
        ).fetchone()
        if snapshot is None:
            raise RouterApiError(
                503,
                "snapshot_not_ready",
                "No applied VPN snapshot is available",
                {"Retry-After": "30"},
            )
        publication = db.execute(
            """
            SELECT published_at
            FROM ip_change_operations
            WHERE published_revision = ? AND published_at IS NOT NULL
            ORDER BY id DESC LIMIT 1
            """,
            (applied_revision,),
        ).fetchone()
    payload_json = str(snapshot["payload_json"])
    payload_sha256 = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(payload_sha256, str(snapshot["payload_sha256"])):
        raise RouterApiError(
            503,
            "snapshot_not_ready",
            "Applied VPN snapshot is unavailable",
            {"Retry-After": "30"},
        )
    try:
        config = captured_vpn_config_from_json(payload_json)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RouterApiError(
            503,
            "snapshot_not_ready",
            "Applied VPN snapshot is unavailable",
            {"Retry-After": "30"},
        ) from exc
    published_at = publication["published_at"] if publication else None
    return snapshot, config, published_at


def _vless_outbound(config: CapturedVpnConfig, client: VpnClient) -> dict[str, Any]:
    return {
        "type": "vless",
        "tag": "vless-reality",
        "server": config.current_ip,
        "server_port": config.vless.protocol.port,
        "uuid": client.vless_uuid,
        "flow": "xtls-rprx-vision",
        "tls": {
            "enabled": True,
            "server_name": config.vless.server_name,
            "utls": {
                "enabled": True,
                "fingerprint": config.vless.fingerprint,
            },
            "reality": {
                "enabled": True,
                "public_key": config.vless.public_key,
                "short_id": config.vless.short_id,
            },
        },
    }


def _hysteria_outbound(config: CapturedVpnConfig) -> dict[str, Any]:
    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "tag": "hysteria2",
        "server": config.current_ip,
        "server_port": config.hysteria.protocol.port,
        "password": config.hysteria.password,
        "tls": {
            "enabled": True,
            "server_name": config.vless.server_name,
            "insecure": True,
        },
    }
    if config.hysteria.obfs_password:
        outbound["obfs"] = {
            "type": "salamander",
            "password": config.hysteria.obfs_password,
        }
    return outbound


def _amnezia_profile(config: CapturedVpnConfig, client: VpnClient) -> dict[str, Any]:
    obfuscation = config.amnezia.obfuscation
    return {
        "protocol_version": 1,
        "capabilities": {
            "awg_obfuscation_v1": True,
            "awg2_i_fields": False,
            "obfuscation_fields": [
                "Jc",
                "Jmin",
                "Jmax",
                "S1",
                "S2",
                "H1",
                "H2",
                "H3",
                "H4",
            ],
        },
        "interface": {
            "private_key": client.amnezia_private_key,
            "address": f"{client.amnezia_ipv4}/32",
            "dns_servers": [
                item.strip() for item in config.amnezia.dns.split(",") if item.strip()
            ],
        },
        "peer": {
            "public_key": config.amnezia.server_public_key,
            "preshared_key": client.amnezia_preshared_key,
            "endpoint": {
                "host": config.current_ip,
                "port": config.amnezia.protocol.port,
            },
            "persistent_keepalive": 25,
        },
        "obfuscation": {
            "Jc": obfuscation.jc,
            "Jmin": obfuscation.jmin,
            "Jmax": obfuscation.jmax,
            "S1": obfuscation.s1,
            "S2": obfuscation.s2,
            "H1": obfuscation.h1,
            "H2": obfuscation.h2,
            "H3": obfuscation.h3,
            "H4": obfuscation.h4,
        },
        "route_allowed_ips": ["0.0.0.0/0", "::/0"],
        "install_routes": False,
        "legacy_amnezia_vpn_import_key": render_amnezia_vpn_key(config, client),
    }


def build_router_snapshot_response(
    snapshot: dict[str, Any],
    config: CapturedVpnConfig,
    client: VpnClient,
    published_at: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "revision": int(snapshot["revision"]),
        "snapshot_sha256": snapshot["payload_sha256"],
        "applied_at": snapshot["applied_at"],
        "published_at": published_at,
        "client": {"id": client.id, "name": client.name},
        "server": {"endpoint": config.current_ip},
        "protocols": {
            "vless": {
                "enabled": config.vless.protocol.enabled,
                "outbound": _vless_outbound(config, client)
                if config.vless.protocol.enabled
                else None,
            },
            "hysteria2": {
                "enabled": config.hysteria.protocol.enabled,
                "outbound": _hysteria_outbound(config)
                if config.hysteria.protocol.enabled
                else None,
            },
            "amneziawg": {
                "enabled": config.amnezia.protocol.enabled,
                "profile": _amnezia_profile(config, client)
                if config.amnezia.protocol.enabled
                else None,
            },
        },
    }


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def response_etag(payload: dict[str, Any]) -> str:
    return f'"{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"'


@router.get("/snapshot")
def router_snapshot(request: Request) -> Response:
    credential = authorize_router(request, "snapshot:read")
    snapshot, config, published_at = _load_applied_snapshot()
    client = next(
        (candidate for candidate in config.enabled_clients if candidate.id == credential.client_id),
        None,
    )
    if client is None:
        raise RouterApiError(
            409,
            "client_not_applied",
            "Router credential client is not present in the applied snapshot",
        )
    if not config.current_ip:
        raise RouterApiError(
            503,
            "snapshot_not_ready",
            "Applied VPN snapshot has no server endpoint",
            {"Retry-After": "30"},
        )
    payload = build_router_snapshot_response(snapshot, config, client, published_at)
    etag = response_etag(payload)
    if_none_match = request.headers.get("if-none-match", "")
    if len(if_none_match) > MAX_CONDITIONAL_HEADER:
        raise RouterApiError(400, "invalid_header", "Conditional header is too long")
    headers = {**API_RESPONSE_HEADERS, "ETag": etag}
    if if_none_match == etag:
        return Response(status_code=304, headers=headers)
    return Response(
        content=canonical_json_bytes(payload),
        media_type="application/json",
        headers=headers,
    )
