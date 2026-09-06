from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.db import get_db, now_iso


APPLY_RESULT_SCHEMA_VERSION = 1
APPLY_RESULT_MAX_BODY_BYTES = 16 * 1024
APPLY_RESULT_MAX_DIAGNOSTICS_BYTES = 8 * 1024
APPLY_RESULT_OUTCOMES = frozenset({"APPLIED", "DEGRADED", "FAILED"})
CAPABILITY_NAMES = (
    "vless",
    "hysteria2",
    "amneziawg",
    "zapret",
    "policy_routing",
)
_IDEMPOTENCY_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z")
_SHA256_PATTERN = re.compile(r"\A[0-9a-f]{64}\Z")
_ACTIVE_PROFILE_PATTERN = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
_UUID_PATTERN = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)
_SECRET_VALUE_PATTERN = re.compile(r"\A[A-Za-z0-9_+/=-]{32,256}\Z")
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "privatekey",
    "private_key",
    "preshared",
    "secret",
    "token",
)


class RouterApplyValidationError(ValueError):
    pass


class RouterApplyConflictError(RuntimeError):
    def __init__(self, code: str, current_applied_revision: int | None):
        super().__init__(code)
        self.code = code
        self.current_applied_revision = current_applied_revision


@dataclass(frozen=True)
class ValidatedRouterApplyResult:
    revision: int
    snapshot_sha256: str
    outcome: str
    active_profile: str | None
    capabilities: dict[str, bool]
    diagnostics: dict[str, Any]
    canonical_body: bytes
    body_sha256: str


@dataclass(frozen=True)
class StoredRouterApplyResult:
    id: int
    credential_id: str
    idempotency_key: str
    revision: int
    snapshot_sha256: str
    snapshot_etag: str
    outcome: str
    active_profile: str | None
    created_at: str
    replayed: bool


def validate_idempotency_key(value: str) -> str:
    if not _IDEMPOTENCY_PATTERN.fullmatch(value):
        raise RouterApplyValidationError("Invalid idempotency key")
    return value


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character).startswith("C") for character in value)


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    if any(
        marker in lowered
        for marker in (
            "-----begin ",
            "authorization:",
            "bearer ",
            "password=",
            "private key",
            "presharedkey",
            "avrt_",
            "vless://",
            "hy2://",
            "vpn://",
        )
    ):
        return True
    return bool(_UUID_PATTERN.fullmatch(value) or _SECRET_VALUE_PATTERN.fullmatch(value))


def _validate_diagnostic_value(
    value: Any,
    *,
    depth: int,
    budget: list[int],
) -> Any:
    if depth > 5:
        raise RouterApplyValidationError("Diagnostics nesting is too deep")
    budget[0] += 1
    if budget[0] > 128:
        raise RouterApplyValidationError("Diagnostics contain too many values")
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise RouterApplyValidationError("Diagnostics contain a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > 500:
            raise RouterApplyValidationError("Diagnostic string is too long")
        if _has_control_character(value):
            raise RouterApplyValidationError("Diagnostics contain control characters")
        if "<" in value or ">" in value:
            raise RouterApplyValidationError("Diagnostics cannot contain HTML markup")
        if _looks_sensitive(value):
            raise RouterApplyValidationError("Diagnostics may contain secret material")
        return value
    if isinstance(value, list):
        if len(value) > 32:
            raise RouterApplyValidationError("Diagnostic list is too long")
        return [
            _validate_diagnostic_value(item, depth=depth + 1, budget=budget)
            for item in value
        ]
    if isinstance(value, dict):
        if len(value) > 32:
            raise RouterApplyValidationError("Diagnostic object has too many keys")
        validated: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise RouterApplyValidationError("Diagnostic key is invalid")
            if _has_control_character(key) or "<" in key or ">" in key:
                raise RouterApplyValidationError("Diagnostic key is invalid")
            normalized_key = key.lower().replace("-", "_")
            compact_key = normalized_key.replace("_", "")
            if any(
                part in normalized_key or part.replace("_", "") in compact_key
                for part in _FORBIDDEN_KEY_PARTS
            ):
                raise RouterApplyValidationError("Diagnostic key may contain secret material")
            if (
                normalized_key in {"code", "stage"}
                and isinstance(child, str)
                and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", child)
            ):
                validated[key] = child
            else:
                validated[key] = _validate_diagnostic_value(
                    child, depth=depth + 1, budget=budget
                )
        return validated
    raise RouterApplyValidationError("Diagnostics contain an unsupported value")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def validate_router_apply_result(payload: Any) -> ValidatedRouterApplyResult:
    if not isinstance(payload, dict):
        raise RouterApplyValidationError("Request body must be a JSON object")
    expected_keys = {
        "schema_version",
        "revision",
        "snapshot_sha256",
        "outcome",
        "active_profile",
        "capabilities",
        "diagnostics",
    }
    if set(payload) != expected_keys:
        raise RouterApplyValidationError("Request body has an invalid schema")
    if payload["schema_version"] != APPLY_RESULT_SCHEMA_VERSION:
        raise RouterApplyValidationError("Unsupported apply result schema version")
    revision = payload["revision"]
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise RouterApplyValidationError("revision must be a non-negative integer")
    snapshot_sha256 = payload["snapshot_sha256"]
    if not isinstance(snapshot_sha256, str) or not _SHA256_PATTERN.fullmatch(
        snapshot_sha256
    ):
        raise RouterApplyValidationError("snapshot_sha256 must be lowercase SHA-256")
    outcome = payload["outcome"]
    if not isinstance(outcome, str) or outcome not in APPLY_RESULT_OUTCOMES:
        raise RouterApplyValidationError("Invalid apply outcome")
    active_profile = payload["active_profile"]
    if active_profile is not None and (
        not isinstance(active_profile, str)
        or not _ACTIVE_PROFILE_PATTERN.fullmatch(active_profile)
    ):
        raise RouterApplyValidationError("active_profile is invalid")
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, dict) or tuple(sorted(capabilities)) != tuple(
        sorted(CAPABILITY_NAMES)
    ):
        raise RouterApplyValidationError("capabilities has an invalid schema")
    if any(type(capabilities[name]) is not bool for name in CAPABILITY_NAMES):
        raise RouterApplyValidationError("Capability values must be booleans")
    normalized_capabilities = {name: capabilities[name] for name in CAPABILITY_NAMES}
    diagnostics = payload["diagnostics"]
    if not isinstance(diagnostics, dict):
        raise RouterApplyValidationError("diagnostics must be a JSON object")
    normalized_diagnostics = _validate_diagnostic_value(
        diagnostics, depth=0, budget=[0]
    )
    diagnostics_json = _canonical_json(normalized_diagnostics)
    if len(diagnostics_json) > APPLY_RESULT_MAX_DIAGNOSTICS_BYTES:
        raise RouterApplyValidationError("Diagnostics are too large")
    normalized = {
        "schema_version": APPLY_RESULT_SCHEMA_VERSION,
        "revision": revision,
        "snapshot_sha256": snapshot_sha256,
        "outcome": outcome,
        "active_profile": active_profile,
        "capabilities": normalized_capabilities,
        "diagnostics": normalized_diagnostics,
    }
    canonical_body = _canonical_json(normalized)
    return ValidatedRouterApplyResult(
        revision=revision,
        snapshot_sha256=snapshot_sha256,
        outcome=outcome,
        active_profile=active_profile,
        capabilities=normalized_capabilities,
        diagnostics=normalized_diagnostics,
        canonical_body=canonical_body,
        body_sha256=hashlib.sha256(canonical_body).hexdigest(),
    )


def _stored_result(row: dict[str, Any], *, replayed: bool) -> StoredRouterApplyResult:
    return StoredRouterApplyResult(
        id=int(row["id"]),
        credential_id=str(row["credential_id"]),
        idempotency_key=str(row["idempotency_key"]),
        revision=int(row["revision"]),
        snapshot_sha256=str(row["snapshot_sha256"]),
        snapshot_etag=str(row["snapshot_etag"]),
        outcome=str(row["outcome"]),
        active_profile=row["active_profile"],
        created_at=str(row["created_at"]),
        replayed=replayed,
    )


def record_router_apply_result(
    credential_id: str,
    idempotency_key: str,
    result: ValidatedRouterApplyResult,
    snapshot_etag: str,
) -> StoredRouterApplyResult:
    validate_idempotency_key(idempotency_key)
    timestamp = now_iso()
    with get_db() as db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute(
            "SELECT applied_revision FROM vpn_state WHERE singleton = 1"
        ).fetchone()
        current_revision = (
            int(state["applied_revision"])
            if state and state["applied_revision"] is not None
            else None
        )
        if current_revision is None:
            raise RouterApplyConflictError("snapshot_not_ready", None)
        snapshot = db.execute(
            """
            SELECT payload_sha256 FROM vpn_snapshots
            WHERE revision = ? AND lifecycle = 'APPLIED'
            """,
            (current_revision,),
        ).fetchone()
        if (
            snapshot is None
            or result.revision != current_revision
            or result.snapshot_sha256 != snapshot["payload_sha256"]
        ):
            raise RouterApplyConflictError("snapshot_conflict", current_revision)
        existing = db.execute(
            """
            SELECT * FROM router_apply_results
            WHERE credential_id = ? AND idempotency_key = ?
            """,
            (credential_id, idempotency_key),
        ).fetchone()
        if existing is not None:
            if existing["body_sha256"] != result.body_sha256:
                raise RouterApplyConflictError(
                    "idempotency_conflict", current_revision
                )
            if not hmac.compare_digest(existing["snapshot_etag"], snapshot_etag):
                raise RouterApplyConflictError("etag_mismatch", current_revision)
            return _stored_result(existing, replayed=True)
        try:
            cursor = db.execute(
                """
                INSERT INTO router_apply_results(
                    credential_id, idempotency_key, body_sha256, revision,
                    snapshot_sha256, snapshot_etag, outcome, active_profile, capabilities_json,
                    diagnostics_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    idempotency_key,
                    result.body_sha256,
                    result.revision,
                    result.snapshot_sha256,
                    snapshot_etag,
                    result.outcome,
                    result.active_profile,
                    _canonical_json(result.capabilities).decode("utf-8"),
                    _canonical_json(result.diagnostics).decode("utf-8"),
                    timestamp,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError:
            # BEGIN IMMEDIATE serializes normal writers. This fallback keeps the
            # uniqueness contract explicit for a concurrent insert race.
            existing = db.execute(
                """
                SELECT * FROM router_apply_results
                WHERE credential_id = ? AND idempotency_key = ?
                """,
                (credential_id, idempotency_key),
            ).fetchone()
            if existing and existing["body_sha256"] == result.body_sha256:
                if not hmac.compare_digest(existing["snapshot_etag"], snapshot_etag):
                    raise RouterApplyConflictError(
                        "etag_mismatch", current_revision
                    ) from None
                return _stored_result(existing, replayed=True)
            raise RouterApplyConflictError(
                "idempotency_conflict", current_revision
            ) from None
        row = db.execute(
            "SELECT * FROM router_apply_results WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        return _stored_result(row, replayed=False)


def list_router_apply_results(limit: int = 50) -> list[dict[str, Any]]:
    bounded_limit = min(max(int(limit), 1), 200)
    with get_db() as db:
        return db.execute(
            """
            SELECT id, credential_id, idempotency_key, revision, snapshot_sha256,
                   snapshot_etag, outcome, active_profile, created_at, updated_at
            FROM router_apply_results
            ORDER BY id DESC LIMIT ?
            """,
            (bounded_limit,),
        ).fetchall()


def get_router_apply_result(result_id: int) -> dict[str, Any] | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT id, credential_id, idempotency_key, revision, snapshot_sha256,
                   snapshot_etag, outcome, active_profile, capabilities_json, diagnostics_json,
                   created_at, updated_at
            FROM router_apply_results WHERE id = ?
            """,
            (result_id,),
        ).fetchone()
    if row is None:
        return None
    metadata = {
        key: value
        for key, value in row.items()
        if key not in {"capabilities_json", "diagnostics_json"}
    }
    return {
        **metadata,
        "capabilities": json.loads(row["capabilities_json"]),
        "diagnostics": json.loads(row["diagnostics_json"]),
    }


def find_router_apply_result_replay(
    credential_id: str,
    idempotency_key: str,
    result: ValidatedRouterApplyResult,
    snapshot_etag: str,
) -> StoredRouterApplyResult | None:
    with get_db() as db:
        row = db.execute(
            """
            SELECT * FROM router_apply_results
            WHERE credential_id = ? AND idempotency_key = ?
            """,
            (credential_id, idempotency_key),
        ).fetchone()
    if row is None:
        return None
    if row["body_sha256"] != result.body_sha256:
        raise RouterApplyConflictError("idempotency_conflict", None)
    if not hmac.compare_digest(row["snapshot_etag"], snapshot_etag):
        raise RouterApplyConflictError("etag_mismatch", None)
    return _stored_result(row, replayed=True)
