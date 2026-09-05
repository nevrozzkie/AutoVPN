from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, replace
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app.amnezia import render_amnezia_vpn_key
from app.db import get_db
from app.hysteria_auth import hysteria_auth
from app.runtime_config import router_api_enabled
from app.router_credentials import (
    AuthenticatedRouterCredential,
    RouterAuthenticationFailure,
    authenticate_router_credential,
)
from app.router_apply_results import (
    APPLY_RESULT_MAX_BODY_BYTES,
    RouterApplyConflictError,
    RouterApplyValidationError,
    StoredRouterApplyResult,
    find_router_apply_result_replay,
    record_router_apply_result,
    validate_idempotency_key,
    validate_router_apply_result,
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
    if not router_api_enabled():
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
            "router_forbidden": "Router is disabled",
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


def _hysteria_outbound(config: CapturedVpnConfig, client: VpnClient) -> dict[str, Any]:
    outbound: dict[str, Any] = {
        "type": "hysteria2",
        "tag": "hysteria2",
        "server": config.current_ip,
        "server_port": config.hysteria.protocol.port,
        "password": hysteria_auth(client.as_dict(), config),
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
    router_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": 3,
        "router_id": router_id,
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
                "outbound": _hysteria_outbound(config, client)
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


def build_router_dual_snapshot_response(
    snapshot: dict[str, Any],
    config: CapturedVpnConfig,
    client: VpnClient,
    published_at: str | None,
    router_id: str,
) -> dict[str, Any]:
    payload = build_router_snapshot_response(snapshot, config, client, published_at, router_id)
    payload["schema_version"] = 4
    peer = next(
        (item for item in config.router_amnezia_peers
         if item.router_id == router_id and item.client_id == client.id),
        None,
    )
    profile = None
    if peer is not None and config.amnezia.protocol.enabled:
        auxiliary = replace(
            client, amnezia_private_key=peer.private_key,
            amnezia_public_key=peer.public_key,
            amnezia_preshared_key=peer.preshared_key, amnezia_ipv4=peer.ipv4,
        )
        profile = _amnezia_profile(config, auxiliary)
        del profile["legacy_amnezia_vpn_import_key"]
    payload["protocols"]["amneziawg_aux"] = {"enabled": profile is not None, "profile": profile}
    return payload


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def response_etag(payload: dict[str, Any]) -> str:
    return f'"{hashlib.sha256(canonical_json_bytes(payload)).hexdigest()}"'


def _client_from_applied_snapshot(
    credential: AuthenticatedRouterCredential,
    config: CapturedVpnConfig,
) -> VpnClient:
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
    return client


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RouterApplyValidationError("JSON object contains a duplicate key")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> None:
    raise RouterApplyValidationError("JSON contains a non-finite number")


async def _read_apply_result_body(request: Request) -> bytes:
    content_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if content_type != "application/json":
        raise RouterApiError(415, "unsupported_media_type", "Content-Type must be application/json")
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            raise RouterApiError(400, "invalid_header", "Content-Length is invalid") from None
        if declared_length < 0:
            raise RouterApiError(400, "invalid_header", "Content-Length is invalid")
        if declared_length > APPLY_RESULT_MAX_BODY_BYTES:
            raise RouterApiError(413, "body_too_large", "Request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > APPLY_RESULT_MAX_BODY_BYTES:
            raise RouterApiError(413, "body_too_large", "Request body is too large")
    if not body:
        raise RouterApiError(400, "invalid_json", "Request body must contain JSON")
    return bytes(body)


def _parse_apply_result_json(body: bytes) -> Any:
    try:
        return json.loads(
            body,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, RouterApplyValidationError) as exc:
        if isinstance(exc, RouterApplyValidationError):
            message = str(exc)
        else:
            message = "Request body is not valid JSON"
        raise RouterApiError(400, "invalid_json", message) from None


def _apply_result_response(result: StoredRouterApplyResult) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "result_id": result.id,
        "idempotency_key": result.idempotency_key,
        "revision": result.revision,
        "snapshot_sha256": result.snapshot_sha256,
        "outcome": result.outcome,
        "active_profile": result.active_profile,
        "accepted_at": result.created_at,
        "replayed": result.replayed,
    }


@router.get("/snapshot")
def router_snapshot(request: Request) -> Response:
    credential = authorize_router(request, "snapshot:read")
    snapshot, config, published_at = _load_applied_snapshot()
    client = _client_from_applied_snapshot(credential, config)
    if not config.current_ip:
        raise RouterApiError(
            503,
            "snapshot_not_ready",
            "Applied VPN snapshot has no server endpoint",
            {"Retry-After": "30"},
        )
    payload = build_router_snapshot_response(
        snapshot, config, client, published_at, credential.router_id
    )
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


@router.get("/snapshot/dual")
def router_snapshot_dual(request: Request) -> Response:
    credential = authorize_router(request, "snapshot:read")
    snapshot, config, published_at = _load_applied_snapshot()
    client = _client_from_applied_snapshot(credential, config)
    if not config.current_ip:
        raise RouterApiError(
            503, "snapshot_not_ready", "Applied VPN snapshot has no server endpoint",
            {"Retry-After": "30"},
        )
    payload = build_router_dual_snapshot_response(
        snapshot, config, client, published_at, credential.router_id
    )
    etag = response_etag(payload)
    conditional = request.headers.get("if-none-match", "")
    if len(conditional) > MAX_CONDITIONAL_HEADER:
        raise RouterApiError(400, "invalid_header", "Conditional header is too long")
    headers = {**API_RESPONSE_HEADERS, "ETag": etag}
    if conditional == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=canonical_json_bytes(payload), media_type="application/json", headers=headers)


@router.get("/amnezia/vpn-zapret")
def router_auxiliary_amnezia(request: Request) -> Response:
    """Return only this router's second peer from the immutable applied revision.

    Kept separate from snapshot v3 so existing strict OpenWrt clients, ETags and
    apply-results keep their contract. This endpoint never allocates live keys.
    """
    credential = authorize_router(request, "snapshot:read")
    snapshot, config, _ = _load_applied_snapshot()
    client = _client_from_applied_snapshot(credential, config)
    peer = next(
        (peer for peer in config.router_amnezia_peers
         if peer.router_id == credential.router_id and peer.client_id == credential.client_id),
        None,
    )
    if peer is None:
        raise RouterApiError(
            409, "router_peer_not_applied",
            "The router VPN+zapret peer is not present in the applied snapshot",
        )
    if not config.current_ip:
        raise RouterApiError(503, "snapshot_not_ready", "Applied VPN snapshot has no server endpoint")
    profile = None
    if config.amnezia.protocol.enabled:
        auxiliary_client = replace(
            client, amnezia_private_key=peer.private_key,
            amnezia_public_key=peer.public_key, amnezia_preshared_key=peer.preshared_key,
            amnezia_ipv4=peer.ipv4,
        )
        profile = _amnezia_profile(config, auxiliary_client)
        # No second import link is needed; the controller consumes typed fields.
        del profile["legacy_amnezia_vpn_import_key"]
    payload = {
        "schema_version": 1,
        "router_id": credential.router_id,
        "lane": "vpn_zapret",
        "revision": int(snapshot["revision"]),
        "snapshot_sha256": str(snapshot["payload_sha256"]),
        "enabled": config.amnezia.protocol.enabled,
        "profile": profile,
    }
    etag = response_etag(payload)
    conditional = request.headers.get("if-none-match", "")
    if len(conditional) > MAX_CONDITIONAL_HEADER:
        raise RouterApiError(400, "invalid_header", "Conditional header is too long")
    headers = {**API_RESPONSE_HEADERS, "ETag": etag}
    if conditional == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=canonical_json_bytes(payload), media_type="application/json", headers=headers)


@router.put("/apply-results/{idempotency_key}")
async def router_apply_result(request: Request, idempotency_key: str) -> Response:
    credential = authorize_router(request, "apply:write")
    try:
        validated_key = validate_idempotency_key(idempotency_key)
    except RouterApplyValidationError as exc:
        raise RouterApiError(400, "invalid_idempotency_key", str(exc)) from None
    if_match = request.headers.get("if-match", "")
    if len(if_match) > MAX_CONDITIONAL_HEADER:
        raise RouterApiError(400, "invalid_header", "Conditional header is too long")
    if not if_match:
        snapshot, _, _ = _load_applied_snapshot()
        raise RouterApiError(
            428,
            "if_match_required",
            "If-Match for the applied router snapshot is required",
            extra={"current_applied_revision": int(snapshot["revision"])},
        )
    raw_body = await _read_apply_result_body(request)
    parsed = _parse_apply_result_json(raw_body)
    try:
        result = validate_router_apply_result(parsed)
    except RouterApplyValidationError as exc:
        raise RouterApiError(400, "invalid_apply_result", str(exc)) from None
    try:
        replay = find_router_apply_result_replay(
            credential.credential_id, validated_key, result, if_match
        )
    except RouterApplyConflictError as exc:
        snapshot, _, _ = _load_applied_snapshot()
        if exc.code == "etag_mismatch":
            raise RouterApiError(
                412,
                "etag_mismatch",
                "If-Match does not match the original idempotent result",
                extra={"current_applied_revision": int(snapshot["revision"])},
            ) from None
        raise RouterApiError(
            409,
            "idempotency_conflict",
            "Idempotency key was already used for a different apply result",
            extra={"current_applied_revision": int(snapshot["revision"])},
        ) from None
    if replay is not None:
        return Response(
            content=canonical_json_bytes(_apply_result_response(replay)),
            media_type="application/json",
            headers={**API_RESPONSE_HEADERS, "ETag": replay.snapshot_etag},
        )

    snapshot, config, published_at = _load_applied_snapshot()
    client = _client_from_applied_snapshot(credential, config)
    snapshot_payload = build_router_snapshot_response(
        snapshot, config, client, published_at, credential.router_id
    )
    dual_payload = build_router_dual_snapshot_response(
        snapshot, config, client, published_at, credential.router_id
    )
    accepted_etags = [response_etag(snapshot_payload), response_etag(dual_payload)]
    if not any(hmac.compare_digest(if_match, item) for item in accepted_etags):
        raise RouterApiError(
            412,
            "etag_mismatch",
            "If-Match does not match the applied router snapshot",
            extra={"current_applied_revision": int(snapshot["revision"])},
        )
    if (
        result.revision != int(snapshot["revision"])
        or result.snapshot_sha256 != snapshot["payload_sha256"]
    ):
        raise RouterApiError(
            409,
            "snapshot_conflict",
            "Apply result does not reference the current applied snapshot",
            extra={"current_applied_revision": int(snapshot["revision"])},
        )
    try:
        stored = record_router_apply_result(
            credential.credential_id, validated_key, result, if_match
        )
    except RouterApplyConflictError as exc:
        if exc.code == "snapshot_not_ready":
            raise RouterApiError(
                503,
                "snapshot_not_ready",
                "No applied VPN snapshot is available",
                {"Retry-After": "30"},
            ) from None
        if exc.code == "etag_mismatch":
            raise RouterApiError(
                412,
                "etag_mismatch",
                "If-Match does not match the original idempotent result",
                extra={"current_applied_revision": exc.current_applied_revision},
            ) from None
        message = (
            "Idempotency key was already used for a different apply result"
            if exc.code == "idempotency_conflict"
            else "Applied VPN snapshot changed while accepting the result"
        )
        raise RouterApiError(
            409,
            exc.code,
            message,
            extra={"current_applied_revision": exc.current_applied_revision},
        ) from None
    return Response(
        content=canonical_json_bytes(_apply_result_response(stored)),
        media_type="application/json",
        headers={**API_RESPONSE_HEADERS, "ETag": if_match},
    )
