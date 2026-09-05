from __future__ import annotations

import re

from fastapi.testclient import TestClient

import app.main as main
from app.db import create_client, init_db, set_setting
from app.router_credentials import list_routers
from app.security import hash_password


TOKEN_PATTERN = re.compile(r"avrt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{43,128}")


def _admin_client() -> TestClient:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", hash_password("admin-password"))
    return TestClient(main.app, base_url="http://panel.local")


def _post(client: TestClient, path: str, data: dict[str, str] | None = None):
    return client.post(
        path,
        data=data or {},
        auth=("admin", "admin-password"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )


def _token(response) -> str:
    match = TOKEN_PATTERN.search(response.text)
    assert match is not None
    return match.group(0)


def test_router_admin_requires_auth_and_rejects_cross_origin_creation() -> None:
    client = _admin_client()
    vpn_client = create_client("Dorm VPN identity")

    assert client.get("/admin/routers").status_code == 401
    blocked = client.post(
        "/admin/routers",
        data={"name": "Dorm", "client_id": str(vpn_client["id"])},
        auth=("admin", "admin-password"),
        headers={"Origin": "https://evil.example"},
    )

    assert blocked.status_code == 403
    assert list_routers() == []


def test_create_router_shows_token_once_and_tracks_device_independently() -> None:
    client = _admin_client()
    dorm_client = create_client("Dorm VPN identity")
    parents_client = create_client("Parents VPN identity")

    dorm_response = _post(
        client,
        "/admin/routers",
        {"name": "Dorm", "client_id": str(dorm_client["id"])},
    )
    parents_response = _post(
        client,
        "/admin/routers",
        {"name": "Parents", "client_id": str(parents_client["id"])},
    )

    assert dorm_response.status_code == 200
    assert dorm_response.headers["cache-control"] == "private, no-store"
    assert dorm_response.headers["referrer-policy"] == "no-referrer"
    assert dorm_response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"
    dorm_token = _token(dorm_response)
    parents_token = _token(parents_response)
    assert dorm_token != parents_token
    assert dorm_response.text.count(dorm_token) == 1

    routers = list_routers()
    assert {(router["label"], router["client_id"]) for router in routers} == {
        ("Dorm", dorm_client["id"]),
        ("Parents", parents_client["id"]),
    }
    page = client.get("/admin/routers", auth=("admin", "admin-password"))
    assert page.status_code == 200
    assert "Dorm" in page.text
    assert "Parents" in page.text
    assert dorm_token not in page.text
    assert parents_token not in page.text
    assert "secret_digest" not in page.text

    dorm_api = client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {dorm_token}"},
    )
    assert dorm_api.status_code == 503
    routers_by_label = {router["label"]: router for router in list_routers()}
    assert routers_by_label["Dorm"]["last_seen_at"] is not None
    assert routers_by_label["Parents"]["last_seen_at"] is None


def test_router_disable_rotate_revoke_and_additional_credential() -> None:
    client = _admin_client()
    vpn_client = create_client("Dorm VPN identity")
    created = _post(
        client,
        "/admin/routers",
        {"name": "Dorm", "client_id": str(vpn_client["id"])},
    )
    original_token = _token(created)
    router = list_routers()[0]
    router_id = router["router_id"]
    credential_id = router["credentials"][0]["credential_id"]

    assert _post(client, f"/admin/routers/{router_id}/disable").status_code == 303
    disabled = client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {original_token}"},
    )
    assert disabled.status_code == 403
    assert disabled.json()["error"]["code"] == "router_forbidden"
    assert _post(client, f"/admin/routers/{router_id}/enable").status_code == 303

    rotated = _post(client, f"/admin/router-credentials/{credential_id}/rotate")
    rotated_token = _token(rotated)
    assert rotated_token != original_token
    assert client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {original_token}"},
    ).status_code == 401
    assert client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {rotated_token}"},
    ).status_code == 503

    additional = _post(client, f"/admin/routers/{router_id}/credentials")
    additional_token = _token(additional)
    assert additional_token not in {original_token, rotated_token}
    assert len(list_routers()[0]["credentials"]) == 2

    revoked = _post(client, f"/admin/router-credentials/{credential_id}/revoke")
    assert revoked.status_code == 303
    forbidden = client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {rotated_token}"},
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "credential_forbidden"
    assert client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {additional_token}"},
    ).status_code == 503


def test_router_can_be_renamed_without_changing_identity() -> None:
    client = _admin_client()
    vpn_client = create_client("Dorm VPN identity")
    _post(
        client,
        "/admin/routers",
        {"name": "Dorm", "client_id": str(vpn_client["id"])},
    )
    router_id = list_routers()[0]["router_id"]

    response = _post(
        client,
        f"/admin/routers/{router_id}/rename",
        {"name": "Dormitory main"},
    )

    assert response.status_code == 303
    assert list_routers()[0]["router_id"] == router_id
    assert list_routers()[0]["label"] == "Dormitory main"

    empty = _post(
        client,
        f"/admin/routers/{router_id}/rename",
        {"name": "   "},
    )
    assert empty.status_code == 400
    assert list_routers()[0]["label"] == "Dormitory main"


def test_admin_requires_a_dedicated_client_and_delete_frees_it() -> None:
    client = _admin_client()
    vpn_client = create_client("Dedicated VPN identity")
    created = _post(
        client,
        "/admin/routers",
        {"name": "First", "client_id": str(vpn_client["id"])},
    )
    first_token = _token(created)
    first_router_id = list_routers()[0]["router_id"]

    duplicate = _post(
        client,
        "/admin/routers",
        {"name": "Second", "client_id": str(vpn_client["id"])},
    )

    assert duplicate.status_code == 400
    assert len(list_routers()) == 1
    deleted = _post(client, f"/admin/routers/{first_router_id}/delete")
    assert deleted.status_code == 303
    assert list_routers() == []
    assert client.get(
        "/api/v2/router/snapshot",
        headers={"Authorization": f"Bearer {first_token}"},
    ).status_code == 401
    replacement = _post(
        client,
        "/admin/routers",
        {"name": "Second", "client_id": str(vpn_client["id"])},
    )
    assert replacement.status_code == 200
