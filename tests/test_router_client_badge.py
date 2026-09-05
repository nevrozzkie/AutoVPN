from __future__ import annotations

from app import main


class _Request:
    base_url = "https://example.test/"


def test_client_list_marks_router_client_without_changing_actions(monkeypatch) -> None:
    client = {
        "id": 7, "name": "Room router", "enabled": True, "token": "token",
        "vless_uuid": "uuid", "vless_downlink": 0, "vless_uplink": 0,
        "amnezia_rx": 0, "amnezia_tx": 0, "stats_last_seen_at": None,
        "stats_updated_at": None, "amnezia_latest_handshake": None,
    }
    monkeypatch.setattr(main, "list_clients_with_stats", lambda: [client.copy()])
    monkeypatch.setattr(main, "list_routers", lambda: [{"client_id": 7, "router_id": "router-7", "label": "Общага"}])
    monkeypatch.setattr(main, "get_setting", lambda *_: "")
    captured = {}
    monkeypatch.setattr(main.templates, "TemplateResponse", lambda request, name, context: captured.update(context) or context)
    main.admin_clients(_Request(), "admin")
    assert captured["clients"][0]["router"]["label"] == "Общага"
    assert captured["clients"][0]["token"] == "token"


def test_client_list_leaves_non_router_unbadged(monkeypatch) -> None:
    client = {"id": 8, "name": "Phone", "enabled": True, "token": "token", "vless_uuid": "uuid", "vless_downlink": 0, "vless_uplink": 0, "amnezia_rx": 0, "amnezia_tx": 0, "stats_last_seen_at": None, "stats_updated_at": None, "amnezia_latest_handshake": None}
    monkeypatch.setattr(main, "list_clients_with_stats", lambda: [client.copy()])
    monkeypatch.setattr(main, "list_routers", lambda: [])
    monkeypatch.setattr(main, "get_setting", lambda *_: "")
    captured = {}
    monkeypatch.setattr(main.templates, "TemplateResponse", lambda request, name, context: captured.update(context) or context)
    main.admin_clients(_Request(), "admin")
    assert captured["clients"][0]["router"] is None


def test_router_badge_keeps_empty_label_and_escapes_label(monkeypatch) -> None:
    client = {"id": 9, "name": "Router", "enabled": True, "token": "token", "vless_uuid": "uuid", "vless_downlink": 0, "vless_uplink": 0, "amnezia_rx": 0, "amnezia_tx": 0, "stats_last_seen_at": None, "stats_updated_at": None, "amnezia_latest_handshake": None}
    monkeypatch.setattr(main, "list_clients_with_stats", lambda: [client.copy()])
    monkeypatch.setattr(main, "list_routers", lambda: [{"client_id": 9, "router_id": "router-9", "label": ""}])
    monkeypatch.setattr(main, "get_setting", lambda *_: "")
    captured = {}
    monkeypatch.setattr(main.templates, "TemplateResponse", lambda request, name, context: captured.update(context) or context)
    main.admin_clients(_Request(), "admin")
    assert captured["clients"][0]["router"]["router_id"] == "router-9"
