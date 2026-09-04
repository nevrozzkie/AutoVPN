from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import settings
from app.db import (
    create_client,
    get_db,
    get_vpn_state,
    init_db,
    set_client_enabled,
    set_setting,
    update_client_name,
)
from app.router_credentials import (
    authenticate_router_credential,
    create_router,
    get_router,
    issue_router_credential,
    list_routers,
    list_router_credentials,
    revoke_router_credential,
    rotate_router_credential,
    set_router_enabled,
    update_router_label,
)
from app.router_credentials_cli import main as credentials_cli_main
from app.vpn_state import complete_install_operation, prepare_install_operation


@pytest.fixture
def router_api_enabled() -> None:
    original = settings.enable_router_api
    object.__setattr__(settings, "enable_router_api", True)
    try:
        yield
    finally:
        object.__setattr__(settings, "enable_router_api", original)


def _apply_current_snapshot() -> int:
    prepared = prepare_install_operation("203.0.113.10")
    complete_install_operation(prepared.operation_id, "test apply")
    return prepared.revision


def _fixture_client(name: str = "Router One") -> dict[str, object]:
    client = create_client(name)
    with get_db() as db:
        db.execute(
            """
            UPDATE clients
            SET token = ?, vless_uuid = ?, hysteria_password = ?,
                amnezia_private_key = ?, amnezia_public_key = ?,
                amnezia_preshared_key = ?, amnezia_ipv4 = ?
            WHERE id = ?
            """,
            (
                f"legacy-client-token-{client['id']}",
                f"00000000-0000-0000-0000-{int(client['id']):012d}",
                f"unused-client-hysteria-{client['id']}",
                f"awg-private-{client['id']}",
                f"awg-public-{client['id']}",
                f"awg-preshared-{client['id']}",
                f"10.66.66.{int(client['id']) + 1}",
                client["id"],
            ),
        )
    return {**client, "name": name}


def _setup_applied_router() -> tuple[TestClient, dict[str, object], str, int]:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    set_setting("vless.reality_public_key", "reality-public")
    set_setting("vless.reality_short_id", "0011223344556677")
    set_setting("hysteria.password", "shared-hysteria")
    set_setting("hysteria.obfs_password", "hysteria-obfs")
    set_setting("amnezia.server_public_key", "awg-server-public")
    client = _fixture_client()
    revision = _apply_current_snapshot()
    issued = issue_router_credential(
        int(client["id"]), ["snapshot:read", "apply:write"]
    )
    return TestClient(main.app), client, issued.token, revision


def _authorization(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_router_credentials_migration_constraints_and_no_vpn_revision_change() -> None:
    init_db()
    client = _fixture_client()
    before = get_vpn_state()["desired_revision"]

    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])
    listed = list_router_credentials()
    rotated = rotate_router_credential(issued.credential_id)
    revoke_router_credential(issued.credential_id)
    with pytest.raises(ValueError, match="Revoked"):
        rotate_router_credential(issued.credential_id)

    assert get_vpn_state()["desired_revision"] == before
    assert issued.token.startswith(f"avrt_{issued.credential_id}.")
    assert len(issued.token.rsplit(".", 1)[1]) >= 43
    assert rotated != issued.token
    assert listed == [
        {
            "credential_id": issued.credential_id,
            "router_id": issued.router_id,
            "client_id": client["id"],
            "label": "",
            "scopes": ["snapshot:read"],
            "enabled": True,
            "created_at": issued.created_at,
            "updated_at": issued.created_at,
            "last_used_at": None,
            "expires_at": None,
            "revoked_at": None,
        }
    ]
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM router_credentials WHERE credential_id = ?",
            (issued.credential_id,),
        ).fetchone()
    assert issued.token not in json.dumps(row)
    assert issued.token.rsplit(".", 1)[1] not in json.dumps(row)
    assert row["secret_digest"] == hashlib.sha256(
        rotated.rsplit(".", 1)[1].encode("ascii")
    ).hexdigest()
    assert len(row["secret_digest"]) == 64


def test_router_has_stable_identity_multiple_credentials_and_last_seen() -> None:
    init_db()
    client = _fixture_client()
    foreign_client = _fixture_client("Foreign client")
    primary = issue_router_credential(
        int(client["id"]), ["snapshot:read"], label="Dorm router"
    )
    second = issue_router_credential(
        int(client["id"]), ["apply:write"], router_id=primary.router_id
    )

    assert primary.router_id == second.router_id
    assert rotate_router_credential(primary.credential_id).startswith(
        f"avrt_{primary.credential_id}."
    )
    with pytest.raises(ValueError, match="does not belong"):
        issue_router_credential(
            int(foreign_client["id"]), ["snapshot:read"], router_id=primary.router_id
        )

    router = get_router(primary.router_id)
    assert router is not None
    assert router["client_id"] == client["id"]
    assert router["label"] == "Dorm router"
    assert router["last_seen_at"] is None
    listed = list_routers()
    assert len(listed) == 1
    assert {credential["credential_id"] for credential in listed[0]["credentials"]} == {
        primary.credential_id,
        second.credential_id,
    }
    assert listed[0]["latest_apply_result"] is None

    authenticated = authenticate_router_credential(second.token, "apply:write")
    assert authenticated.router_id == primary.router_id  # type: ignore[union-attr]
    assert authenticated.client_id == client["id"]  # type: ignore[union-attr]
    assert get_router(primary.router_id)["last_seen_at"] is not None  # type: ignore[index]

    set_router_enabled(primary.router_id, False)
    assert authenticate_router_credential(second.token, "apply:write").code == "router_forbidden"  # type: ignore[union-attr]
    set_router_enabled(primary.router_id, True)
    assert authenticate_router_credential(second.token, "apply:write").router_id == primary.router_id  # type: ignore[union-attr]


def test_create_router_is_client_bound() -> None:
    init_db()
    client = _fixture_client()
    created = create_router("Parents", int(client["id"]))

    assert created["label"] == "Parents"
    assert created["client_id"] == client["id"]
    assert list_routers()[0]["credentials"] == []
    with pytest.raises(ValueError, match="already belongs"):
        create_router("Duplicate", int(client["id"]))
    with pytest.raises(ValueError, match="already belongs"):
        issue_router_credential(int(client["id"]), ["snapshot:read"])


def test_router_label_update_preserves_identity_and_validates_name() -> None:
    init_db()
    client = _fixture_client()
    issued = issue_router_credential(
        int(client["id"]), ["snapshot:read"], label="Before"
    )
    before = get_router(issued.router_id)
    assert before is not None

    update_router_label(issued.router_id, "After")

    after = get_router(issued.router_id)
    assert after is not None
    assert after["router_id"] == issued.router_id
    assert after["label"] == "After"
    assert after["updated_at"] >= before["updated_at"]
    assert list_routers()[0]["credentials"][0]["credential_id"] == issued.credential_id
    with pytest.raises(ValueError, match="label is invalid"):
        update_router_label(issued.router_id, "invalid\x00name")


def test_router_credential_auth_rotate_revoke_expiry_and_scope() -> None:
    init_db()
    client = _fixture_client()
    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])

    assert authenticate_router_credential(issued.token, "snapshot:read").client_id == client["id"]  # type: ignore[union-attr]
    assert authenticate_router_credential(issued.token + "x", "snapshot:read").code == "invalid_token"  # type: ignore[union-attr]
    assert authenticate_router_credential(issued.token, "apply:write").code == "insufficient_scope"  # type: ignore[union-attr]

    rotated = rotate_router_credential(issued.credential_id)
    assert authenticate_router_credential(issued.token, "snapshot:read").code == "invalid_token"  # type: ignore[union-attr]
    assert authenticate_router_credential(rotated, "snapshot:read").client_id == client["id"]  # type: ignore[union-attr]
    revoke_router_credential(issued.credential_id)
    assert authenticate_router_credential(rotated, "snapshot:read").code == "credential_forbidden"  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="future"):
        issue_router_credential(
            int(client["id"]),
            ["snapshot:read"],
            expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            router_id=issued.router_id,
        )

    expires_later = issue_router_credential(
        int(client["id"]),
        ["snapshot:read"],
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        router_id=issued.router_id,
    )
    with get_db() as db:
        db.execute(
            "UPDATE router_credentials SET expires_at = ? WHERE credential_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expires_later.credential_id),
        )
    assert authenticate_router_credential(expires_later.token, "snapshot:read").code == "invalid_token"  # type: ignore[union-attr]


def test_router_credential_cli_prints_secret_once_and_list_is_sanitized(
    capsys: pytest.CaptureFixture[str],
) -> None:
    init_db()
    client = _fixture_client()

    assert credentials_cli_main(
        [
            "issue",
            "--client-id",
            str(client["id"]),
            "--scope",
            "snapshot:read",
            "--label",
            "Hall router",
        ]
    ) == 0
    issue_output = capsys.readouterr().out
    issue_metadata = json.loads(issue_output.splitlines()[0])
    token = next(line.removeprefix("token=") for line in issue_output.splitlines() if line.startswith("token="))
    assert issue_metadata["router_id"]
    assert issue_output.count(token) == 1

    assert credentials_cli_main(["list"]) == 0
    list_output = capsys.readouterr().out
    assert "Hall router" in list_output
    assert token not in list_output
    assert token.rsplit(".", 1)[1] not in list_output
    assert "secret_digest" not in list_output


def test_router_snapshot_feature_off_is_404() -> None:
    init_db()
    set_setting("config.enable_router_api", "0")
    response = TestClient(main.app).get("/api/v2/router/snapshot")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Resource not found"}
    }


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Basic wrong"])
def test_router_snapshot_rejects_missing_or_wrong_bearer(
    router_api_enabled: None,
    authorization: str | None,
) -> None:
    init_db()
    headers = {"Authorization": authorization} if authorization else {}
    response = TestClient(main.app).get("/api/v2/router/snapshot", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == 'Bearer realm="router-api"'
    assert response.json()["error"]["code"] == "invalid_token"
    assert "wrong" not in response.text


def test_router_snapshot_scope_revoked_and_disabled_client_are_403(
    router_api_enabled: None,
) -> None:
    init_db()
    client = _fixture_client()
    read = issue_router_credential(int(client["id"]), ["snapshot:read"])
    write_only = issue_router_credential(
        int(client["id"]), ["apply:write"], router_id=read.router_id
    )
    http = TestClient(main.app)

    assert http.get("/api/v2/router/snapshot", headers=_authorization(write_only.token)).status_code == 403
    revoke_router_credential(read.credential_id)
    assert http.get("/api/v2/router/snapshot", headers=_authorization(read.token)).status_code == 403
    replacement = issue_router_credential(
        int(client["id"]), ["snapshot:read"], router_id=read.router_id
    )
    set_client_enabled(int(client["id"]), False)
    assert http.get("/api/v2/router/snapshot", headers=_authorization(replacement.token)).status_code == 403


def test_router_snapshot_requires_an_applied_snapshot(
    router_api_enabled: None,
) -> None:
    init_db()
    client = _fixture_client()
    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])

    response = TestClient(main.app).get(
        "/api/v2/router/snapshot", headers=_authorization(issued.token)
    )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "30"
    assert response.json()["error"]["code"] == "snapshot_not_ready"


def test_router_snapshot_is_exact_client_only_applied_schema_and_supports_304(
    router_api_enabled: None,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    set_setting("vless.reality_public_key", "reality-public")
    set_setting("vless.reality_short_id", "0011223344556677")
    set_setting("hysteria.password", "shared-hysteria")
    set_setting("hysteria.obfs_password", "hysteria-obfs")
    set_setting("amnezia.server_public_key", "awg-server-public")
    client = _fixture_client()
    foreign = _fixture_client("Foreign Client")
    foreign_secret = "foreign-vless-secret"
    with get_db() as db:
        db.execute(
            "UPDATE clients SET vless_uuid = ?, amnezia_private_key = ? WHERE id = ?",
            (foreign_secret, "foreign-awg-private", foreign["id"]),
        )
    revision = _apply_current_snapshot()
    token = issue_router_credential(
        int(client["id"]), ["snapshot:read", "apply:write"]
    ).token
    http = TestClient(main.app)
    before_desired_change = http.get(
        "/api/v2/router/snapshot", headers=_authorization(token)
    )
    update_client_name(int(client["id"]), "Desired New Name")

    response = http.get("/api/v2/router/snapshot", headers=_authorization(token))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["vary"] == "Authorization"
    payload = response.json()
    assert set(payload) == {
        "schema_version",
        "router_id",
        "revision",
        "snapshot_sha256",
        "applied_at",
        "published_at",
        "client",
        "server",
        "protocols",
    }
    assert payload["schema_version"] == 3
    assert payload["router_id"] == list_routers()[0]["router_id"]
    assert payload["revision"] == revision
    assert payload["client"] == {"id": client["id"], "name": "Router One"}
    assert payload["server"] == {"endpoint": "203.0.113.10"}
    assert payload["protocols"]["vless"]["outbound"]["type"] == "vless"
    assert payload["protocols"]["hysteria2"]["outbound"]["type"] == "hysteria2"
    awg = payload["protocols"]["amneziawg"]["profile"]
    assert awg["protocol_version"] == 1
    assert awg["route_allowed_ips"] == ["0.0.0.0/0", "::/0"]
    assert awg["install_routes"] is False
    assert awg["capabilities"]["awg2_i_fields"] is False
    assert set(awg["obfuscation"]) == {
        "Jc", "Jmin", "Jmax", "S1", "S2", "H1", "H2", "H3", "H4"
    }
    assert not ({"dns", "route", "inbounds", "tun"} & set(payload))
    assert foreign_secret not in response.text
    assert "foreign-awg-private" not in response.text
    assert f"legacy-client-token-{client['id']}" not in response.text
    assert token not in response.text
    assert response.content == before_desired_change.content
    assert response.headers["etag"] == before_desired_change.headers["etag"]

    cached = http.get(
        "/api/v2/router/snapshot",
        headers={**_authorization(token), "If-None-Match": response.headers["etag"]},
    )
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["etag"] == response.headers["etag"]


def test_snapshot_identity_and_etag_are_distinct_for_multiple_routers(
    router_api_enabled: None,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    first_client = _fixture_client("First router")
    second_client = _fixture_client("Second router")
    _apply_current_snapshot()
    first = issue_router_credential(int(first_client["id"]), ["snapshot:read"])
    second = issue_router_credential(int(second_client["id"]), ["snapshot:read"])
    http = TestClient(main.app)

    first_snapshot = http.get(
        "/api/v2/router/snapshot", headers=_authorization(first.token)
    )
    second_snapshot = http.get(
        "/api/v2/router/snapshot", headers=_authorization(second.token)
    )

    assert first_snapshot.status_code == 200
    assert second_snapshot.status_code == 200
    assert first_snapshot.json()["router_id"] == first.router_id
    assert second_snapshot.json()["router_id"] == second.router_id
    assert first_snapshot.headers["etag"] != second_snapshot.headers["etag"]


def test_client_enabled_now_but_absent_from_applied_snapshot_is_409(
    router_api_enabled: None,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = _fixture_client()
    set_client_enabled(int(client["id"]), False)
    _apply_current_snapshot()
    set_client_enabled(int(client["id"]), True)
    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])

    response = TestClient(main.app).get(
        "/api/v2/router/snapshot", headers=_authorization(issued.token)
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "client_not_applied"
