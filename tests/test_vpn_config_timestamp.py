from datetime import datetime

from app.db import create_client, get_setting, init_db, update_client_name


def test_client_changes_mark_vpn_config_updated() -> None:
    init_db()

    client = create_client("Alice")
    created_timestamp = get_setting("vpn.config_updated_at")

    assert datetime.fromisoformat(created_timestamp)

    update_client_name(client["id"], "Bob")

    assert datetime.fromisoformat(get_setting("vpn.config_updated_at"))


def test_hysteria_server_password_is_initialized() -> None:
    init_db()

    assert get_setting("hysteria.password")
