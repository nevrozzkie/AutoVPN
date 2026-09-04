from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import settings
from app.db import create_client, get_db, init_db, set_setting, update_client_name
from app.router_apply_results import (
    APPLY_RESULT_MAX_BODY_BYTES,
    RouterApplyValidationError,
    record_router_apply_result,
    validate_router_apply_result,
)
from app.router_credentials import issue_router_credential
from app.router_credentials_cli import main as router_cli_main
from app.vpn_state import complete_install_operation, prepare_install_operation


@pytest.fixture
def router_api_enabled() -> None:
    original = settings.enable_router_api
    object.__setattr__(settings, "enable_router_api", True)
    try:
        yield
    finally:
        object.__setattr__(settings, "enable_router_api", original)


def _applied_router() -> tuple[TestClient, dict[str, object], str, str, dict[str, object]]:
    init_db()
    set_setting("current_ip", "203.0.113.44")
    client = create_client("OpenWrt Cudy")
    prepared = prepare_install_operation("203.0.113.44")
    complete_install_operation(prepared.operation_id, "test apply")
    issued = issue_router_credential(
        int(client["id"]), ["snapshot:read", "apply:write"], label="Cudy WR3000S"
    )
    http = TestClient(main.app)
    snapshot = http.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {issued.token}"},
    )
    assert snapshot.status_code == 200
    return http, client, issued.token, snapshot.headers["etag"], snapshot.json()


def _result_body(snapshot: dict[str, object], **updates: object) -> dict[str, object]:
    body: dict[str, object] = {
        "schema_version": 1,
        "revision": snapshot["revision"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "outcome": "APPLIED",
        "active_profile": "vless-reality",
        "capabilities": {
            "vless": True,
            "hysteria2": True,
            "amneziawg": True,
            "zapret": False,
            "policy_routing": True,
        },
        "diagnostics": {
            "firmware": "OpenWrt 24.10",
            "checks": ["config-loaded", "route-installed"],
            "latency_ms": 18,
        },
    }
    body.update(updates)
    return body


def _headers(token: str, etag: str | None = None) -> dict[str, str]:
    result = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if etag is not None:
        result["If-Match"] = etag
    return result


def test_apply_result_replay_is_idempotent_and_conflicting_body_is_409(
    router_api_enabled: None,
) -> None:
    http, _, token, etag, snapshot = _applied_router()
    body = _result_body(snapshot)

    first = http.put(
        "/api/v2/router/apply-results/boot-001",
        headers=_headers(token, etag),
        json=body,
    )
    replay = http.put(
        "/api/v2/router/apply-results/boot-001",
        headers=_headers(token, etag),
        content=json.dumps(body, indent=2),
    )
    conflict = http.put(
        "/api/v2/router/apply-results/boot-001",
        headers=_headers(token, etag),
        json=_result_body(snapshot, outcome="DEGRADED"),
    )

    assert first.status_code == 200
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "replayed": True}
    assert conflict.status_code == 409
    assert conflict.json()["error"] == {
        "code": "idempotency_conflict",
        "message": "Idempotency key was already used for a different apply result",
        "current_applied_revision": snapshot["revision"],
    }
    with get_db() as db:
        assert db.execute("SELECT count(*) AS count FROM router_apply_results").fetchone()["count"] == 1


def test_apply_result_requires_current_etag_and_snapshot(
    router_api_enabled: None,
) -> None:
    http, _, token, etag, snapshot = _applied_router()
    body = _result_body(snapshot)

    missing = http.put(
        "/api/v2/router/apply-results/boot-002",
        headers=_headers(token),
        json=body,
    )
    mismatch = http.put(
        "/api/v2/router/apply-results/boot-002",
        headers=_headers(token, '"' + "0" * 64 + '"'),
        json=body,
    )
    stale = http.put(
        "/api/v2/router/apply-results/boot-002",
        headers=_headers(token, etag),
        json=_result_body(snapshot, revision=int(snapshot["revision"]) + 1),
    )
    unknown_hash = http.put(
        "/api/v2/router/apply-results/boot-003",
        headers=_headers(token, etag),
        json=_result_body(snapshot, snapshot_sha256="0" * 64),
    )

    assert missing.status_code == 428
    assert missing.json()["error"]["current_applied_revision"] == snapshot["revision"]
    assert mismatch.status_code == 412
    assert mismatch.json()["error"]["code"] == "etag_mismatch"
    for response in (stale, unknown_hash):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "snapshot_conflict"
        assert response.json()["error"]["current_applied_revision"] == snapshot["revision"]


def test_apply_result_feature_flag_and_scope_boundary(
    router_api_enabled: None,
) -> None:
    http, client, token, etag, snapshot = _applied_router()
    read_only = issue_router_credential(int(client["id"]), ["snapshot:read"])

    forbidden = http.put(
        "/api/v2/router/apply-results/write-scope-required",
        headers=_headers(read_only.token, etag),
        json=_result_body(snapshot),
    )
    object.__setattr__(settings, "enable_router_api", False)
    hidden = http.put(
        "/api/v2/router/apply-results/hidden",
        headers=_headers(token, etag),
        json=_result_body(snapshot),
    )

    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "insufficient_scope"
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "not_found"


def test_apply_result_body_limit_is_enforced_before_json_parsing(
    router_api_enabled: None,
) -> None:
    http, _, token, etag, _ = _applied_router()
    response = http.put(
        "/api/v2/router/apply-results/too-large",
        headers=_headers(token, etag),
        content=b"{" + b"x" * APPLY_RESULT_MAX_BODY_BYTES,
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"password": "redacted"},
        {"detail": "avrt_credential.secret-material-that-must-not-be-stored"},
        {"detail": "line one\nline two"},
        {"detail": "<script>alert(1)</script>"},
        {"detail": "00000000-1111-2222-3333-444444444444"},
    ],
)
def test_apply_result_rejects_secret_control_and_html_diagnostics(
    router_api_enabled: None,
    diagnostics: dict[str, object],
) -> None:
    http, _, token, etag, snapshot = _applied_router()
    response = http.put(
        "/api/v2/router/apply-results/unsafe-diagnostics",
        headers=_headers(token, etag),
        json=_result_body(snapshot, diagnostics=diagnostics),
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_apply_result"
    assert "avrt_" not in response.text
    assert "script" not in response.text
    with get_db() as db:
        assert db.execute("SELECT count(*) AS count FROM router_apply_results").fetchone()["count"] == 0


def test_capabilities_schema_is_exact_and_snapshot_etag_does_not_change(
    router_api_enabled: None,
) -> None:
    http, _, token, etag, snapshot = _applied_router()
    invalid = _result_body(snapshot)
    invalid["capabilities"] = {"vless": True}

    rejected = http.put(
        "/api/v2/router/apply-results/capabilities-invalid",
        headers=_headers(token, etag),
        json=invalid,
    )
    accepted = http.put(
        "/api/v2/router/apply-results/capabilities-valid",
        headers=_headers(token, etag),
        json=_result_body(snapshot),
    )
    after = http.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert rejected.status_code == 400
    assert accepted.status_code == 200
    assert after.content == json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    assert after.headers["etag"] == etag


def test_apply_result_concurrent_replay_creates_one_row(
    router_api_enabled: None,
) -> None:
    _, client, _, _, snapshot = _applied_router()
    credential = issue_router_credential(int(client["id"]), ["apply:write"])
    validated = validate_router_apply_result(_result_body(snapshot))
    barrier = threading.Barrier(2)

    def submit() -> bool:
        barrier.wait()
        return record_router_apply_result(
            credential.credential_id, "concurrent-001", validated, '"' + "a" * 64 + '"'
        ).replayed

    with ThreadPoolExecutor(max_workers=2) as executor:
        replayed = list(executor.map(lambda _: submit(), range(2)))

    assert sorted(replayed) == [False, True]
    with get_db() as db:
        assert db.execute("SELECT count(*) AS count FROM router_apply_results").fetchone()["count"] == 1


def test_apply_result_cli_summary_and_show_are_read_only_and_sanitized(
    router_api_enabled: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    http, _, token, etag, snapshot = _applied_router()
    response = http.put(
        "/api/v2/router/apply-results/cli-visible",
        headers=_headers(token, etag),
        json=_result_body(snapshot),
    )
    result_id = response.json()["result_id"]

    assert router_cli_main(["results"]) == 0
    summary = capsys.readouterr().out
    assert "cli-visible" in summary
    assert token not in summary
    assert "diagnostics_json" not in summary
    assert router_cli_main(["result", str(result_id)]) == 0
    detail = capsys.readouterr().out
    assert "OpenWrt 24.10" in detail
    assert token not in detail
    assert "secret_digest" not in detail


def test_apply_result_without_applied_snapshot_is_503(
    router_api_enabled: None,
) -> None:
    init_db()
    client = create_client("Not applied")
    issued = issue_router_credential(int(client["id"]), ["apply:write"])
    response = TestClient(main.app).put(
        "/api/v2/router/apply-results/not-ready",
        headers=_headers(issued.token, '"' + "0" * 64 + '"'),
        json=_result_body({"revision": 0, "snapshot_sha256": "0" * 64}),
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == "snapshot_not_ready"


def test_exact_replay_with_original_etag_survives_applied_revision_advance(
    router_api_enabled: None,
) -> None:
    http, client, token, original_etag, original_snapshot = _applied_router()
    original_body = _result_body(original_snapshot)
    first = http.put(
        "/api/v2/router/apply-results/durable-replay",
        headers=_headers(token, original_etag),
        json=original_body,
    )
    assert first.status_code == 200

    update_client_name(int(client["id"]), "OpenWrt Cudy updated")
    newer = prepare_install_operation("203.0.113.44")
    complete_install_operation(newer.operation_id, "newer test apply")
    current = http.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert current.status_code == 200
    assert current.headers["etag"] != original_etag

    replay = http.put(
        "/api/v2/router/apply-results/durable-replay",
        headers=_headers(token, original_etag),
        json=original_body,
    )
    new_stale_key = http.put(
        "/api/v2/router/apply-results/new-stale-key",
        headers=_headers(token, original_etag),
        json=original_body,
    )
    changed_replay = http.put(
        "/api/v2/router/apply-results/durable-replay",
        headers=_headers(token, original_etag),
        json=_result_body(original_snapshot, outcome="FAILED"),
    )

    assert replay.status_code == 200
    assert replay.json() == {**first.json(), "replayed": True}
    assert replay.headers["etag"] == original_etag
    assert new_stale_key.status_code == 412
    assert changed_replay.status_code == 409
    assert changed_replay.json()["error"]["current_applied_revision"] == newer.revision


def test_apply_result_feature_off_is_404() -> None:
    init_db()
    set_setting("config.enable_router_api", "0")
    response = TestClient(main.app).put(
        "/api/v2/router/apply-results/off",
        headers={"Content-Type": "application/json"},
        content=b"{}",
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_apply_result_validator_rejects_duplicate_or_extra_schema_fields() -> None:
    with pytest.raises(RouterApplyValidationError, match="invalid schema"):
        validate_router_apply_result({"schema_version": 1, "extra": True})
