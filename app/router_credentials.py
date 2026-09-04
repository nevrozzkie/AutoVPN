from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable

from app.db import get_db, now_iso


ROUTER_TOKEN_PREFIX = "avrt_"
ROUTER_TOKEN_MAX_LENGTH = 256
KNOWN_SCOPES = frozenset({"apply:write", "snapshot:read"})
_TOKEN_PATTERN = re.compile(
    r"\Aavrt_([A-Za-z0-9_-]{8,64})\.([A-Za-z0-9_-]{43,128})\Z"
)
_FAKE_DIGEST = hashlib.sha256(b"invalid-router-credential").hexdigest()


class RouterCredentialError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedRouterCredential:
    credential_id: str
    client_id: int
    scopes: tuple[str, ...]
    label: str
    token: str
    created_at: str


@dataclass(frozen=True)
class AuthenticatedRouterCredential:
    credential_id: str
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


def issue_router_credential(
    client_id: int,
    scopes: Iterable[str],
    *,
    expires_at: str | None = None,
    label: str = "",
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
        db.execute(
            """
            INSERT INTO router_credentials(
                credential_id, client_id, label, secret_digest, scopes, enabled,
                created_at, updated_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                credential_id,
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
                   updated_at, last_used_at, expires_at, revoked_at
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
                SELECT credential.*, client.enabled AS client_enabled,
                       client.deleted_at AS client_deleted_at
                FROM router_credentials AS credential
                JOIN clients AS client ON client.id = credential.client_id
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
        scopes = _decode_scopes(row["scopes"])
        if required_scope not in scopes:
            return RouterAuthenticationFailure("insufficient_scope")
        timestamp = now_iso()
        db.execute(
            "UPDATE router_credentials SET last_used_at = ? WHERE credential_id = ?",
            (timestamp, credential_id),
        )
    return AuthenticatedRouterCredential(
        credential_id=credential_id,
        client_id=int(row["client_id"]),
        scopes=scopes,
    )
