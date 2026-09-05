from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.amnezia import render_amnezia_server_config
from app.db import create_client, get_db, init_db, set_setting
from app.router_credentials import (
    issue_router_credential, list_routers, rotate_router_credential,
    revoke_router_credential, set_router_enabled,
)
from app.vpn_config import (
    canonical_vpn_config_json, capture_vpn_config, captured_vpn_config_from_json,
)
from app.vpn_state import complete_install_operation, prepare_install_operation


PATH = "/api/v2/router/amnezia/vpn-zapret"
DUAL_PATH = "/api/v2/router/snapshot/dual"


def apply_current() -> int:
    prepared = prepare_install_operation("203.0.113.10")
    complete_install_operation(prepared.operation_id, "fixture apply without remote calls")
    return prepared.revision


def setup(scopes: list[str] | None = None):
    init_db()
    set_setting("config.enable_router_api", "1")
    set_setting("config.amnezia_enabled", "1")
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Family router")
    credential = issue_router_credential(client["id"], scopes or ["snapshot:read"])
    return TestClient(app), client, credential


def headers(token: str):
    return {"Authorization": "Bearer " + token}


def apply_result_body(snapshot: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "revision": snapshot["revision"],
        "snapshot_sha256": snapshot["snapshot_sha256"],
        "outcome": "APPLIED",
        "active_profile": "vless-reality",
        "capabilities": {
            "vless": True, "hysteria2": True, "amneziawg": True,
            "zapret": True, "policy_routing": True,
        },
        "diagnostics": {"firmware": "OpenWrt 25.12", "checks": ["dual-snapshot"]},
    }


def test_auxiliary_key_is_applied_router_scoped_and_absent_from_ordinary_links():
    http, client, credential = setup()
    paths = [f"/sub/{client['token']}", f"/sing-box/{client['token']}",
             f"/amnezia/{client['token']}", f"/amnezia-key/{client['token']}"]
    ordinary = {path: http.get(path).content for path in paths}
    revision = apply_current()
    response = http.get(PATH, headers=headers(credential.token))
    assert response.status_code == 200
    payload = response.json()
    assert payload["router_id"] == credential.router_id
    assert payload["revision"] == revision
    assert payload["lane"] == "vpn_zapret"
    assert payload["enabled"] is True
    profile = payload["profile"]
    assert profile["interface"]["private_key"] != client["amnezia_private_key"]
    assert profile["interface"]["address"] != client["amnezia_ipv4"] + "/32"
    assert "legacy_amnezia_vpn_import_key" not in profile
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["vary"] == "Authorization"
    for path in paths:
        result = http.get(path)
        assert result.status_code == 200
        assert result.content == ordinary[path]
        assert profile["interface"]["private_key"].encode() not in result.content
    original_snapshot = http.get("/api/v2/router/snapshot", headers=headers(credential.token)).json()
    assert original_snapshot["schema_version"] == 3
    assert original_snapshot["protocols"]["amneziawg"]["profile"]["interface"]["private_key"] == client["amnezia_private_key"]
    assert original_snapshot["snapshot_sha256"] == payload["snapshot_sha256"]
    assert profile["interface"]["private_key"] not in json.dumps(list_routers())
    assert http.get(PATH, headers={**headers(credential.token), "If-None-Match": response.headers["etag"]}).status_code == 304


def test_dual_snapshot_keeps_v3_contract_and_embeds_applied_auxiliary_peer():
    http, client, credential = setup()
    apply_current()
    v3 = http.get("/api/v2/router/snapshot", headers=headers(credential.token)).json()
    dual = http.get(DUAL_PATH, headers=headers(credential.token))
    assert dual.status_code == 200
    payload = dual.json()
    assert v3["schema_version"] == 3
    assert payload["schema_version"] == 4
    assert payload["protocols"]["amneziawg"]["profile"]["interface"]["private_key"] == client["amnezia_private_key"]
    profile = payload["protocols"]["amneziawg_aux"]["profile"]
    assert payload["protocols"]["amneziawg_aux"]["enabled"] is True
    assert profile["interface"]["private_key"] != client["amnezia_private_key"]
    assert "legacy_amnezia_vpn_import_key" not in profile
    assert http.get(DUAL_PATH, headers={**headers(credential.token), "If-None-Match": dual.headers["etag"]}).status_code == 304


def test_dual_snapshot_without_an_applied_auxiliary_peer_is_disabled_and_null():
    init_db()
    set_setting("config.enable_router_api", "1")
    set_setting("config.amnezia_enabled", "1")
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Router added after apply")
    apply_current()
    credential = issue_router_credential(client["id"], ["snapshot:read"])
    response = TestClient(app).get(DUAL_PATH, headers=headers(credential.token))
    assert response.status_code == 200
    assert response.json()["protocols"]["amneziawg_aux"] == {"enabled": False, "profile": None}


def test_dual_snapshot_has_distinct_auxiliary_keys_for_each_router():
    http, _, first = setup()
    second_client = create_client("Second dual router")
    second = issue_router_credential(second_client["id"], ["snapshot:read"])
    apply_current()
    first_profile = http.get(DUAL_PATH, headers=headers(first.token)).json()["protocols"]["amneziawg_aux"]["profile"]
    second_profile = http.get(DUAL_PATH, headers=headers(second.token)).json()["protocols"]["amneziawg_aux"]["profile"]
    assert first_profile["interface"]["private_key"] != second_profile["interface"]["private_key"]
    assert first_profile["interface"]["address"] != second_profile["interface"]["address"]


def test_apply_result_accepts_and_persists_dual_or_legacy_snapshot_etag():
    http, _, credential = setup(["snapshot:read", "apply:write"])
    apply_current()
    dual = http.get(DUAL_PATH, headers=headers(credential.token))
    v3 = http.get("/api/v2/router/snapshot", headers=headers(credential.token))
    assert dual.status_code == v3.status_code == 200
    dual_etag = dual.headers["etag"]
    dual_body = apply_result_body(dual.json())
    request_headers = {**headers(credential.token), "Content-Type": "application/json", "If-Match": dual_etag}
    accepted = http.put("/api/v2/router/apply-results/dual-etag", headers=request_headers, json=dual_body)
    replay = http.put("/api/v2/router/apply-results/dual-etag", headers=request_headers, content=json.dumps(dual_body, indent=2))
    assert accepted.status_code == replay.status_code == 200
    assert accepted.headers["etag"] == dual_etag
    assert accepted.json()["replayed"] is False
    assert replay.json() == {**accepted.json(), "replayed": True}
    with get_db() as db:
        stored = db.execute("SELECT snapshot_etag FROM router_apply_results WHERE idempotency_key = ?", ("dual-etag",)).fetchone()
    assert stored["snapshot_etag"] == dual_etag

    v3_etag = v3.headers["etag"]
    legacy = http.put(
        "/api/v2/router/apply-results/legacy-etag",
        headers={**headers(credential.token), "Content-Type": "application/json", "If-Match": v3_etag},
        json=apply_result_body(v3.json()),
    )
    assert legacy.status_code == 200
    assert legacy.headers["etag"] == v3_etag


def test_api_never_publishes_a_peer_before_its_server_revision_is_applied():
    init_db()
    set_setting("config.enable_router_api", "1")
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Before router")
    apply_current()
    credential = issue_router_credential(client["id"], ["snapshot:read"])
    http = TestClient(app)
    pending = http.get(PATH, headers=headers(credential.token))
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "router_peer_not_applied"
    apply_current()
    assert http.get(PATH, headers=headers(credential.token)).status_code == 200


def test_auxiliary_api_auth_isolated_between_routers_and_rotation_preserves_peer():
    http, _, first = setup()
    second_client = create_client("Other router")
    second = issue_router_credential(second_client["id"], ["snapshot:read"])
    write_only = issue_router_credential(second_client["id"], ["apply:write"], router_id=second.router_id)
    apply_current()
    first_reply = http.get(PATH, headers=headers(first.token)).json()
    second_reply = http.get(PATH, headers=headers(second.token)).json()
    assert first_reply["profile"]["interface"] != second_reply["profile"]["interface"]
    assert http.get(PATH).status_code == 401
    assert http.get(PATH, headers=headers(write_only.token)).status_code == 403
    # A caller-supplied query parameter cannot select a foreign router.
    assert http.get(PATH + "?router_id=" + second.router_id, headers=headers(first.token)).json() == first_reply
    token = rotate_router_credential(first.credential_id)
    assert http.get(PATH, headers=headers(first.token)).status_code == 401
    assert http.get(PATH, headers=headers(token)).json() == first_reply
    revoke_router_credential(first.credential_id)
    assert http.get(PATH, headers=headers(token)).status_code == 403
    set_router_enabled(second.router_id, False)
    assert http.get(PATH, headers=headers(second.token)).status_code == 403


def test_auxiliary_response_and_server_renderer_use_the_same_immutable_capture():
    http, client, credential = setup()
    captured = capture_vpn_config()
    peer = captured.router_amnezia_peers[0]
    server = render_amnezia_server_config(captured)
    assert peer.public_key in server and peer.ipv4 + "/32" in server
    assert client["amnezia_public_key"] in server
    assert peer.private_key not in server
    encoded = canonical_vpn_config_json(captured)
    assert captured_vpn_config_from_json(encoded) == captured
    apply_current()
    before = http.get(PATH, headers=headers(credential.token))
    with get_db() as db:
        db.execute("UPDATE router_amnezia_peers SET ipv4 = '10.66.66.250' WHERE router_id = ?", (credential.router_id,))
    after = http.get(PATH, headers=headers(credential.token))
    assert after.content == before.content and after.headers["etag"] == before.headers["etag"]
    historical = json.loads(encoded)
    del historical["router_amnezia_peers"]
    assert captured_vpn_config_from_json(json.dumps(historical)).router_amnezia_peers == ()


def test_disabled_protocol_returns_no_auxiliary_private_key():
    http, _, credential = setup()
    set_setting("config.amnezia_enabled", "0")
    apply_current()
    response = http.get(PATH, headers=headers(credential.token))
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert response.json()["profile"] is None
