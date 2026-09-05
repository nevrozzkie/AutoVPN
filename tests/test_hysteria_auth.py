import json
from urllib.parse import quote

import pytest

from app.db import create_client, delete_client, init_db, set_client_enabled, set_setting
from app.eu_install import build_eu_install_script
from app.remote_apply import _hysteria_config
from app.subscriptions import build_sing_box_subscription, build_subscription
from app.vpn_config import capture_vpn_config


def _server_config(renderer: str) -> str:
    if renderer == "install":
        return build_eu_install_script()
    return _hysteria_config(capture_vpn_config(), "/cert.pem", "/key.pem")


def test_each_client_gets_distinct_hysteria_auth_in_all_formats() -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    first = create_client("Alice")
    second = create_client("Bob")

    first_auth = f"client-{first['id']}:{first['hysteria_password']}"
    second_auth = f"client-{second['id']}:{second['hysteria_password']}"

    first_text = build_subscription(first, "203.0.113.10")
    second_text = build_subscription(second, "203.0.113.10")
    first_json = build_sing_box_subscription(first, "203.0.113.10")
    second_json = build_sing_box_subscription(second, "203.0.113.10")

    assert f"hy2://{quote(first_auth, safe='')}@" in first_text
    assert f"hy2://{quote(second_auth, safe='')}@" in second_text
    assert quote(first_auth, safe="") not in second_text
    assert quote(second_auth, safe="") not in first_text
    assert next(item for item in first_json["outbounds"] if item["tag"] == "hysteria2")["password"] == first_auth
    assert next(item for item in second_json["outbounds"] if item["tag"] == "hysteria2")["password"] == second_auth


@pytest.mark.parametrize("renderer", ["install", "transactional"])
def test_hysteria_server_auth_excludes_disabled_and_deleted_clients(renderer: str) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    enabled = create_client("Enabled")
    disabled = create_client("Disabled")
    deleted = create_client("Deleted")
    set_client_enabled(int(disabled["id"]), False)
    delete_client(int(deleted["id"]))

    script = _server_config(renderer)

    users = json.loads(script.split("  userpass: ", 1)[1].splitlines()[0])
    assert users == {f"client-{enabled['id']}": enabled["hysteria_password"]}
    assert f"client-{disabled['id']}" not in script
    assert disabled["hysteria_password"] not in script
    assert f"client-{deleted['id']}" not in script
    assert deleted["hysteria_password"] not in script


@pytest.mark.parametrize("renderer", ["install", "transactional"])
def test_hysteria_server_auth_denies_when_no_clients_are_enabled(renderer: str) -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    disabled = create_client("Disabled")
    set_client_enabled(int(disabled["id"]), False)

    script = _server_config(renderer)

    assert "type: command" in script
    assert "command: /bin/false" in script
    assert "type: userpass" not in script
    assert f"client-{disabled['id']}" not in script


def test_new_hysteria_config_does_not_use_legacy_shared_auth() -> None:
    init_db()
    set_setting("config.hysteria_enabled", "1")
    set_setting("hysteria.password", "legacy-shared-password")
    client = create_client("Alice")

    script = build_eu_install_script()
    subscription = build_subscription(client, "203.0.113.10")

    assert "type: userpass" in script
    assert "type: password" not in script
    assert "legacy-shared-password" not in script
    assert "legacy-shared-password" not in subscription
