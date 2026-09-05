from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from app.db import (
    _mark_vpn_config_updated,
    ensure_router_amnezia_peer,
    get_db,
    now_iso,
)


ROUTER_TOKEN_PREFIX = "avrt_"
ROUTER_TOKEN_MAX_LENGTH = 256
KNOWN_SCOPES = frozenset({"apply:write", "snapshot:read"})
_TOKEN_PATTERN = re.compile(
    r"\Aavrt_([A-Za-z0-9_-]{8,64})\.([A-Za-z0-9_-]{43,128})\Z"
)
_ROUTER_ID_PATTERN = re.compile(r"\A[A-Za-z0-9_-]{8,64}\Z")
_FAKE_DIGEST = hashlib.sha256(b"invalid-router-credential").hexdigest()


class RouterCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedRouterCredential:
    credential_id: str
    router_id: str
    client_id: int
    scopes: tuple[str, ...]
    label: str
    token: str
    created_at: str


@dataclass(frozen=True)
class AuthenticatedRouterCredential:
    credential_id: str
    router_id: str
    client_id: int
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class RouterAuthenticationFailure:
    code: str


def _normalize_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted({scope.strip() for scope in scopes if scope.strip()}))
    unknown = set(normalized) - KNOWN_SCOPES
    if unknown:
        raise RouterCredentialError("Unknown router credential scope")
    if not normalized:
        raise RouterCredentialError("At least one router credential scope is required")
    return normalized


def _encode_scopes(scopes: tuple[str, ...]) -> str:
    return " ".join(scopes)


def _decode_scopes(value: str) -> tuple[str, ...]:
    return tuple(item for item in value.split(" ") if item)


def _digest_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def _normalized_label(label: str) -> str:
    normalized = label.strip()
    if len(normalized) > 100 or any(ord(character) < 32 for character in normalized):
        raise RouterCredentialError("Router credential label is invalid")
    return normalized


def _validated_router_id(router_id: str) -> str:
    if _ROUTER_ID_PATTERN.fullmatch(router_id) is None:
        raise RouterCredentialError("Router does not exist")
    return router_id


def _validated_expiry(expires_at: str | None) -> str | None:
    if expires_at is None or not expires_at.strip():
        return None
    if len(expires_at) > 64:
        raise RouterCredentialError("expires_at must be an ISO 8601 timestamp")
    try:
        parsed = datetime.fromisoformat(expires_at.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise RouterCredentialError("expires_at must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise RouterCredentialError("expires_at must include a timezone")
    if parsed <= datetime.now(UTC):
        raise RouterCredentialError("expires_at must be in the future")
    return parsed.astimezone(UTC).isoformat(timespec="seconds")


def _new_token(credential_id: str) -> tuple[str, str]:
    # token_urlsafe(32) contains 256 random bits before URL-safe encoding.
    secret = secrets.token_urlsafe(32)
    return f"{ROUTER_TOKEN_PREFIX}{credential_id}.{secret}", _digest_secret(secret)


def create_router(name: str, client_id: int) -> dict[str, Any]:
    normalized_label = _normalized_label(name)
    router_id = secrets.token_urlsafe(12)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND deleted_at IS NULL",
            (client_id,),
        ).fetchone()
        if client is None:
            raise RouterCredentialError("Client does not exist")
        existing_router = db.execute(
            "SELECT 1 FROM routers WHERE client_id = ? LIMIT 1",
            (client_id,),
        ).fetchone()
        if existing_router is not None:
            raise RouterCredentialError("Client already belongs to another router")
        db.execute(
            """
            INSERT INTO routers(
                router_id, client_id, label, enabled, created_at, updated_at
            ) VALUES (?, ?, ?, 1, ?, ?)
            """,
            (router_id, client_id, normalized_label, timestamp, timestamp),
        )
        ensure_router_amnezia_peer(db, router_id)
    return {
        "router_id": router_id,
        "client_id": client_id,
        "label": normalized_label,
        "enabled": True,
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_seen_at": None,
    }


def get_router(router_id: str) -> dict[str, Any] | None:
    normalized_router_id = _validated_router_id(router_id)
    with get_db() as db:
        row = db.execute(
            """
            SELECT router_id, client_id, label, enabled, created_at, updated_at,
                   last_seen_at
            FROM routers WHERE router_id = ?
            """,
            (normalized_router_id,),
        ).fetchone()
    if row is None:
        return None
    return {**row, "enabled": bool(row["enabled"])}


def list_routers() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT router_id, client_id, label, enabled, created_at, updated_at,
                   last_seen_at
            FROM routers ORDER BY created_at, router_id
            """
        ).fetchall()
        credentials = db.execute(
            """
            SELECT credential_id, router_id, label, scopes, enabled, created_at,
                   updated_at, last_used_at, expires_at, revoked_at
            FROM router_credentials
            ORDER BY created_at, credential_id
            """
        ).fetchall()
        latest_results = db.execute(
            """
            SELECT result.router_id, result.id, result.credential_id, result.revision,
                   result.outcome, result.active_profile, result.created_at
            FROM (
                SELECT credential.router_id, apply_result.id,
                       apply_result.credential_id, apply_result.revision,
                       apply_result.outcome, apply_result.active_profile,
                       apply_result.created_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY credential.router_id
                           ORDER BY apply_result.id DESC
                       ) AS row_number
                FROM router_apply_results AS apply_result
                JOIN router_credentials AS credential
                    ON credential.credential_id = apply_result.credential_id
            ) AS result
            WHERE result.row_number = 1
            """
        ).fetchall()
    credentials_by_router: dict[str, list[dict[str, Any]]] = {}
    for credential in credentials:
        credentials_by_router.setdefault(str(credential["router_id"]), []).append(
            {
                **credential,
                "enabled": bool(credential["enabled"]),
                "scopes": list(_decode_scopes(credential["scopes"])),
            }
        )
    latest_by_router = {str(row["router_id"]): dict(row) for row in latest_results}
    return [
        {
            **row,
            "enabled": bool(row["enabled"]),
            "credentials": credentials_by_router.get(str(row["router_id"]), []),
            "latest_apply_result": latest_by_router.get(str(row["router_id"])),
        }
        for row in rows
    ]


def set_router_enabled(router_id: str, enabled: bool) -> None:
    normalized_router_id = _validated_router_id(router_id)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        router = db.execute(
            "SELECT enabled FROM routers WHERE router_id = ?",
            (normalized_router_id,),
        ).fetchone()
        if router is None:
            raise RouterCredentialError("Router does not exist")
        desired_value = int(enabled)
        if int(router["enabled"]) == desired_value:
            return
        db.execute(
            """
            UPDATE routers SET enabled = ?, updated_at = ? WHERE router_id = ?
            """,
            (desired_value, timestamp, normalized_router_id),
        )
        _mark_vpn_config_updated(db)


def update_router_label(router_id: str, name: str) -> None:
    normalized_router_id = _validated_router_id(router_id)
    normalized_label = _normalized_label(name)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute(
            """
            UPDATE routers SET label = ?, updated_at = ? WHERE router_id = ?
            """,
            (normalized_label, timestamp, normalized_router_id),
        )
        if updated.rowcount != 1:
            raise RouterCredentialError("Router does not exist")


def delete_router(router_id: str) -> None:
    normalized_router_id = _validated_router_id(router_id)
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        exists = db.execute(
            "SELECT 1 FROM routers WHERE router_id = ?",
            (normalized_router_id,),
        ).fetchone()
        if exists is None:
            raise RouterCredentialError("Router does not exist")
        _mark_vpn_config_updated(db)
        deleted = db.execute(
            "DELETE FROM routers WHERE router_id = ?",
            (normalized_router_id,),
        )
        if deleted.rowcount != 1:
            raise RouterCredentialError("Router does not exist")


def issue_router_credential(
    client_id: int,
    scopes: Iterable[str],
    *,
    expires_at: str | None = None,
    label: str = "",
    router_id: str | None = None,
) -> IssuedRouterCredential:
    normalized_scopes = _normalize_scopes(scopes)
    normalized_label = _normalized_label(label)
    normalized_expiry = _validated_expiry(expires_at)
    credential_id = secrets.token_urlsafe(12)
    token, secret_digest = _new_token(credential_id)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        client = db.execute(
            "SELECT id FROM clients WHERE id = ? AND deleted_at IS NULL",
            (client_id,),
        ).fetchone()
        if client is None:
            raise RouterCredentialError("Client does not exist")
        if router_id is None:
            existing_router = db.execute(
                "SELECT 1 FROM routers WHERE client_id = ? LIMIT 1",
                (client_id,),
            ).fetchone()
            if existing_router is not None:
                raise RouterCredentialError(
                    "Client already belongs to another router"
                )
            assigned_router_id = secrets.token_urlsafe(12)
            db.execute(
                """
                INSERT INTO routers(
                    router_id, client_id, label, enabled, created_at, updated_at
                ) VALUES (?, ?, ?, 1, ?, ?)
                """,
                (
                    assigned_router_id,
                    client_id,
                    normalized_label,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            assigned_router_id = _validated_router_id(router_id)
            router = db.execute(
                """
                SELECT router_id FROM routers
                WHERE router_id = ? AND client_id = ?
                """,
                (assigned_router_id, client_id),
            ).fetchone()
            if router is None:
                raise RouterCredentialError("Router does not belong to client")
        ensure_router_amnezia_peer(db, assigned_router_id)
        db.execute(
            """
            INSERT INTO router_credentials(
                credential_id, router_id, client_id, label, secret_digest, scopes, enabled,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                credential_id,
                assigned_router_id,
                client_id,
                normalized_label,
                secret_digest,
                _encode_scopes(normalized_scopes),
                timestamp,
                timestamp,
                normalized_expiry,
            ),
        )
    return IssuedRouterCredential(
        credential_id=credential_id,
        router_id=assigned_router_id,
        client_id=client_id,
        scopes=normalized_scopes,
        label=normalized_label,
        token=token,
        created_at=timestamp,
    )


def rotate_router_credential(credential_id: str) -> str:
    if not 8 <= len(credential_id) <= 64:
        raise RouterCredentialError("Router credential does not exist")
    token, secret_digest = _new_token(credential_id)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute(
            """
            SELECT enabled, revoked_at, expires_at
            FROM router_credentials WHERE credential_id = ?
            """,
            (credential_id,),
        ).fetchone()
        if existing is None:
            raise RouterCredentialError("Router credential does not exist")
        if not existing["enabled"] or existing["revoked_at"]:
            raise RouterCredentialError("Revoked router credential cannot be rotated")
        if existing["expires_at"]:
            try:
                expiry = datetime.fromisoformat(
                    str(existing["expires_at"]).replace("Z", "+00:00")
                )
            except ValueError as exc:
                raise RouterCredentialError(
                    "Expired router credential cannot be rotated"
                ) from exc
            if expiry <= datetime.now(UTC):
                raise RouterCredentialError("Expired router credential cannot be rotated")
        updated = db.execute(
            """
            UPDATE router_credentials
            SET secret_digest = ?, last_used_at = NULL, updated_at = ?
            WHERE credential_id = ?
            """,
            (secret_digest, timestamp, credential_id),
        )
        if updated.rowcount != 1:
            raise RouterCredentialError("Router credential does not exist")
    return token


def revoke_router_credential(credential_id: str) -> None:
    if not 8 <= len(credential_id) <= 64:
        raise RouterCredentialError("Router credential does not exist")
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        updated = db.execute(
            """
            UPDATE router_credentials
            SET enabled = 0, revoked_at = COALESCE(revoked_at, ?), updated_at = ?
            WHERE credential_id = ?
            """,
            (timestamp, timestamp, credential_id),
        )
        if updated.rowcount != 1:
            raise RouterCredentialError("Router credential does not exist")


def list_router_credentials() -> list[dict[str, Any]]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT credential_id, client_id, label, scopes, enabled, created_at,
                   router_id, updated_at, last_used_at, expires_at, revoked_at
            FROM router_credentials
            ORDER BY created_at, credential_id
            """
        ).fetchall()
    return [
        {
            **row,
            "enabled": bool(row["enabled"]),
            "scopes": list(_decode_scopes(row["scopes"])),
        }
        for row in rows
    ]


def authenticate_router_credential(
    token: str,
    required_scope: str,
) -> AuthenticatedRouterCredential | RouterAuthenticationFailure:
    if required_scope not in KNOWN_SCOPES:
        raise ValueError("Unknown required router API scope")
    match = _TOKEN_PATTERN.fullmatch(token) if len(token) <= ROUTER_TOKEN_MAX_LENGTH else None
    credential_id = match.group(1) if match else ""
    secret = match.group(2) if match else ""
    with get_db() as db:
        row = (
            db.execute(
                """
                SELECT credential.*, router.client_id AS router_client_id,
                       client.enabled AS client_enabled,
                       client.deleted_at AS client_deleted_at,
                       router.enabled AS router_enabled
                FROM router_credentials AS credential
                JOIN routers AS router ON router.router_id = credential.router_id
                JOIN clients AS client ON client.id = router.client_id
                WHERE credential.credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
            if credential_id
            else None
        )
        expected = row["secret_digest"] if row else _FAKE_DIGEST
        supplied = _digest_secret(secret)
        secret_matches = hmac.compare_digest(supplied, expected)
        if row is None or not secret_matches:
            return RouterAuthenticationFailure("invalid_token")
        if row["expires_at"]:
            try:
                expires_at = datetime.fromisoformat(
                    str(row["expires_at"]).replace("Z", "+00:00")
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= datetime.now(UTC):
                    return RouterAuthenticationFailure("invalid_token")
            except ValueError:
                return RouterAuthenticationFailure("invalid_token")
        if not row["enabled"] or row["revoked_at"]:
            return RouterAuthenticationFailure("credential_forbidden")
        if not row["client_enabled"] or row["client_deleted_at"]:
            return RouterAuthenticationFailure("client_forbidden")
        if not row["router_enabled"]:
            return RouterAuthenticationFailure("router_forbidden")
        scopes = _decode_scopes(row["scopes"])
        if required_scope not in scopes:
            return RouterAuthenticationFailure("insufficient_scope")
        timestamp = now_iso()
        db.execute(
            "UPDATE router_credentials SET last_used_at = ? WHERE credential_id = ?",
            (timestamp, credential_id),
        )
        db.execute(
            "UPDATE routers SET last_seen_at = ?, updated_at = ? WHERE router_id = ?",
            (timestamp, timestamp, row["router_id"]),
        )
    return AuthenticatedRouterCredential(
        credential_id=credential_id,
        router_id=str(row["router_id"]),
        client_id=int(row["router_client_id"]),
        scopes=scopes,
    )
