from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

import app.migrations as migrations
from app.db import (
    create_client,
    delete_client,
    ensure_router_amnezia_peer,
    get_db,
    get_vpn_state,
    init_db,
    list_router_amnezia_public_keys,
    set_client_enabled,
)
from app.router_credentials import (
    create_router,
    delete_router,
    issue_router_credential,
    revoke_router_credential,
    rotate_router_credential,
    set_router_enabled,
)


def _peer(router_id: str) -> dict[str, object] | None:
    with get_db() as db:
        return db.execute(
            """
            SELECT router_id, private_key, public_key, preshared_key, ipv4
            FROM router_amnezia_peers WHERE router_id = ?
            """,
            (router_id,),
        ).fetchone()


def test_router_peer_schema_and_creation_are_atomic_and_idempotent() -> None:
    init_db()
    client = create_client("Router owner")
    before = int(get_vpn_state()["desired_revision"])

    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])
    created = _peer(issued.router_id)

    assert created is not None
    assert set(created) == {
        "router_id",
        "private_key",
        "public_key",
        "preshared_key",
        "ipv4",
    }
    assert created["router_id"] == issued.router_id
    assert created["ipv4"] != client["amnezia_ipv4"]
    assert created["public_key"] != client["amnezia_public_key"]
    assert int(get_vpn_state()["desired_revision"]) == before + 1

    second = issue_router_credential(
        int(client["id"]), ["apply:write"], router_id=issued.router_id
    )
    assert second.router_id == issued.router_id
    assert _peer(issued.router_id) == created
    assert int(get_vpn_state()["desired_revision"]) == before + 1

    with get_db() as db:
        columns = [
            row["name"]
            for row in db.execute("PRAGMA table_info(router_amnezia_peers)")
        ]
        foreign_keys = db.execute(
            "PRAGMA foreign_key_list(router_amnezia_peers)"
        ).fetchall()
    assert columns == [
        "router_id",
        "private_key",
        "public_key",
        "preshared_key",
        "ipv4",
    ]
    assert any(
        row["table"] == "routers"
        and row["from"] == "router_id"
        and row["to"] == "router_id"
        and row["on_delete"] == "CASCADE"
        for row in foreign_keys
    )


def test_address_allocator_reserves_auxiliary_peer_for_disabled_deleted_owner() -> None:
    init_db()
    first = create_client("First")
    issued = issue_router_credential(int(first["id"]), ["snapshot:read"])
    peer = _peer(issued.router_id)
    assert peer is not None

    set_router_enabled(issued.router_id, False)
    delete_client(int(first["id"]))
    second = create_client("Second")

    assert len(
        {
            str(first["amnezia_ipv4"]),
            str(peer["ipv4"]),
            str(second["amnezia_ipv4"]),
        }
    ) == 3
    assert list_router_amnezia_public_keys() == []


def test_public_peer_listing_exposes_only_active_public_identity() -> None:
    init_db()
    client = create_client("Listed")
    router = create_router("Listed router", int(client["id"]))
    peer = _peer(str(router["router_id"]))
    assert peer is not None

    listed = list_router_amnezia_public_keys()
    assert listed == [{"client_id": client["id"], "public_key": peer["public_key"]}]
    assert "private_key" not in listed[0]
    assert "preshared_key" not in listed[0]
    assert "ipv4" not in listed[0]

    set_client_enabled(int(client["id"]), False)
    assert list_router_amnezia_public_keys() == []
    set_client_enabled(int(client["id"]), True)
    assert list_router_amnezia_public_keys() == listed
    set_router_enabled(str(router["router_id"]), False)
    assert list_router_amnezia_public_keys() == []


def test_router_lifecycle_revisions_do_not_rotate_auxiliary_peer() -> None:
    init_db()
    client = create_client("Lifecycle")
    issued = issue_router_credential(int(client["id"]), ["snapshot:read"])
    peer = _peer(issued.router_id)
    assert peer is not None
    created_revision = int(get_vpn_state()["desired_revision"])

    rotate_router_credential(issued.credential_id)
    revoke_router_credential(issued.credential_id)
    assert _peer(issued.router_id) == peer
    assert int(get_vpn_state()["desired_revision"]) == created_revision

    set_router_enabled(issued.router_id, False)
    assert int(get_vpn_state()["desired_revision"]) == created_revision + 1
    assert _peer(issued.router_id) == peer
    set_router_enabled(issued.router_id, False)
    assert int(get_vpn_state()["desired_revision"]) == created_revision + 1
    set_router_enabled(issued.router_id, True)
    assert int(get_vpn_state()["desired_revision"]) == created_revision + 2
    assert _peer(issued.router_id) == peer

    delete_router(issued.router_id)
    assert int(get_vpn_state()["desired_revision"]) == created_revision + 3
    assert _peer(issued.router_id) is None


def test_init_backfills_existing_routers_once_outside_migration_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_migrations = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations[:8])
    init_db()
    client = create_client("Existing")
    with get_db() as db:
        db.execute(
            """
            INSERT INTO routers(
                router_id, client_id, label, enabled, created_at, updated_at
            ) VALUES ('existing-router', ?, 'Existing', 1, '2026-01-01', '2026-01-01')
            """,
            (client["id"],),
        )
    before = int(get_vpn_state()["desired_revision"])
    monkeypatch.setattr(migrations, "MIGRATIONS", current_migrations)

    init_db()
    created = _peer("existing-router")
    assert created is not None
    assert int(get_vpn_state()["desired_revision"]) == before + 1

    init_db()
    assert _peer("existing-router") == created
    assert int(get_vpn_state()["desired_revision"]) == before + 1


def test_ensure_peer_rejects_unknown_router_without_revision_change() -> None:
    init_db()
    before = int(get_vpn_state()["desired_revision"])
    with pytest.raises(LookupError, match="Router does not exist"):
        with get_db() as db:
            ensure_router_amnezia_peer(db, "missing-router")
    assert int(get_vpn_state()["desired_revision"]) == before


def test_auxiliary_constraints_reject_duplicate_public_key_and_address() -> None:
    init_db()
    first = create_client("First")
    second = create_client("Second")
    first_router = create_router("First router", int(first["id"]))
    second_router = create_router("Second router", int(second["id"]))
    first_peer = _peer(str(first_router["router_id"]))
    second_peer = _peer(str(second_router["router_id"]))
    assert first_peer is not None and second_peer is not None

    with get_db() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE router_amnezia_peers SET public_key = ? WHERE router_id = ?",
            (first_peer["public_key"], second_router["router_id"]),
        )
    with get_db() as db, pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "UPDATE router_amnezia_peers SET ipv4 = ? WHERE router_id = ?",
            (first_peer["ipv4"], second_router["router_id"]),
        )


def test_concurrent_router_allocations_have_distinct_client_and_peer_identities() -> None:
    init_db()
    clients = [create_client(f"Concurrent {index}") for index in range(4)]
    before = int(get_vpn_state()["desired_revision"])

    def issue(client: dict[str, object]) -> object:
        return issue_router_credential(int(client["id"]), ["snapshot:read"])

    with ThreadPoolExecutor(max_workers=4) as executor:
        credentials = list(executor.map(issue, clients))

    with get_db() as db:
        peers = db.execute(
            "SELECT router_id, public_key, ipv4 FROM router_amnezia_peers"
        ).fetchall()
        client_addresses = {
            str(row["amnezia_ipv4"])
            for row in db.execute("SELECT amnezia_ipv4 FROM clients")
        }
    assert {peer["router_id"] for peer in peers} == {
        credential.router_id for credential in credentials
    }
    assert len({peer["public_key"] for peer in peers}) == 4
    assert len({peer["ipv4"] for peer in peers}) == 4
    assert not client_addresses.intersection({str(peer["ipv4"]) for peer in peers})
    assert int(get_vpn_state()["desired_revision"]) == before + 4


def test_pool_exhaustion_rolls_back_router_peer_credential_and_revision() -> None:
    init_db()
    client = create_client("No address left")
    prefix = str(client["amnezia_ipv4"]).rsplit(".", 1)[0]
    assert client["amnezia_ipv4"] == f"{prefix}.2"
    with get_db() as db:
        db.executemany(
            """
            INSERT INTO clients(
                name, token, enabled, vless_uuid, hysteria_password, created_at,
                amnezia_private_key, amnezia_public_key,
                amnezia_preshared_key, amnezia_ipv4
            ) VALUES (?, ?, 1, ?, ?, '2026-01-01', ?, ?, ?, ?)
            """,
            [
                (
                    f"Reserved {octet}",
                    f"reserved-token-{octet}",
                    f"reserved-uuid-{octet}",
                    f"reserved-hysteria-{octet}",
                    f"reserved-private-{octet}",
                    f"reserved-public-{octet}",
                    f"reserved-preshared-{octet}",
                    f"{prefix}.{octet}",
                )
                for octet in range(3, 255)
            ],
        )
    before = int(get_vpn_state()["desired_revision"])

    with pytest.raises(RuntimeError, match="address pool .* is exhausted"):
        issue_router_credential(int(client["id"]), ["snapshot:read"])

    assert int(get_vpn_state()["desired_revision"]) == before
    with get_db() as db:
        assert db.execute("SELECT COUNT(*) AS count FROM routers").fetchone()["count"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS count FROM router_amnezia_peers"
        ).fetchone()["count"] == 0
        assert db.execute(
            "SELECT COUNT(*) AS count FROM router_credentials"
        ).fetchone()["count"] == 0
