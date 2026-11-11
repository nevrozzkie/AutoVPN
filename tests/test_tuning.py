from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import main
from app.db import init_db, create_client, get_setting, get_vpn_state, set_setting
from app.eu_install import build_eu_deploy_script
from app.operation_coordinator import OperationBusyError
from app.subscriptions import render_subscription, render_sing_box_subscription
from app.tuning import profile_values, update_tuning, validate_values
from app.vpn_config import capture_vpn_config, canonical_vpn_config_json
from app.vpn_state import prepare_install_operation


@pytest.fixture
def configured():
    init_db()
    set_setting("current_ip", "203.0.113.10")
    create_client("Alice")
    return capture_vpn_config()


@pytest.mark.parametrize("protocol", ["vless", "hysteria", "amnezia"])
def test_random_roundtrip_restores_original_profile_and_preserves_keys(configured, protocol):
    original = profile_values(configured, protocol)
    before_revision = get_vpn_state()["desired_revision"]
    for _ in range(3):
        update_tuning(protocol, "random", {})
        randomized = capture_vpn_config()
        assert profile_values(randomized, protocol) != original
        assert randomized.vless.private_key == configured.vless.private_key
        assert randomized.amnezia.server_private_key == configured.amnezia.server_private_key
        assert randomized.clients == configured.clients
        assert get_vpn_state()["applied_revision"] is None
    update_tuning(protocol, "defaults", {})
    assert profile_values(capture_vpn_config(), protocol) == original
    assert get_vpn_state()["desired_revision"] == before_revision + 4


def test_manual_values_reach_server_and_both_subscription_formats(configured):
    vless = profile_values(configured, "vless")
    vless.update(port="10443", target="example.org:443", server_names="example.org,www.example.org",
                 server_name="example.org", short_id="1234abcd", fingerprint="chrome", spider_x="/news")
    update_tuning("vless", "save", vless)
    hysteria = profile_values(capture_vpn_config(), "hysteria")
    hysteria.update(port="18443", obfs_password="new-salamander-password")
    update_tuning("hysteria", "save", hysteria)
    config = capture_vpn_config()
    script = build_eu_deploy_script(config, transactional=True)
    assert '"target": "example.org:443"' in script
    assert '"1234abcd"' in script
    assert "new-salamander-password" in script
    links = render_subscription(config, config.clients[0].as_dict())
    assert ":10443?" in links and ":18443/" in links and "sni=example.org" in links
    assert "sid=1234abcd" in links and "obfs-password=new-salamander-password" in links
    outbounds = render_sing_box_subscription(config, config.clients[0].as_dict())["outbounds"]
    assert outbounds[1]["tls"]["reality"]["short_id"] == "1234abcd"
    assert outbounds[2]["obfs"]["password"] == "new-salamander-password"


@pytest.mark.parametrize("protocol,change", [
    ("vless", {"port": "22"}), ("vless", {"port": "8443"}),
    ("vless", {"target": "ok.ru:443; touch /tmp/injection"}),
    ("vless", {"server_name": "other.example"}), ("vless", {"short_id": "abc"}),
    ("vless", {"spider_x": "/\nmalicious"}), ("vless", {"fingerprint": "unknown"}),
    ("hysteria", {"obfs_password": 'secret\nlisten: :1234'}),
    ("amnezia", {"jmin": "1000", "jmax": "999"}),
    ("amnezia", {"h1": "123", "h2": "123"}),
    ("amnezia", {"s1": "30", "s2": "86"}),
    ("amnezia", {"h1": "4294967296"}), ("amnezia", {"jc": "-1"}),
])
def test_invalid_profiles_do_not_mutate_or_capture_defaults(configured, protocol, change):
    values = profile_values(configured, protocol) | change
    before = canonical_vpn_config_json(configured)
    with pytest.raises(ValueError):
        update_tuning(protocol, "save", values)
    assert canonical_vpn_config_json(capture_vpn_config()) == before
    assert not get_setting(f"vpn.tuning_defaults.{protocol}")


def test_tuning_cannot_race_an_active_install(configured):
    prepare_install_operation(configured.current_ip)
    with pytest.raises(OperationBusyError):
        update_tuning("vless", "random", {})
    assert capture_vpn_config() == configured


def test_defaults_port_conflict_is_reported_without_partial_changes(configured):
    update_tuning("vless", "random", {})
    hy = profile_values(capture_vpn_config(), "hysteria") | {"port": str(configured.vless.protocol.port)}
    update_tuning("hysteria", "save", hy)
    before = capture_vpn_config()
    with pytest.raises(ValueError, match="Порт занят"):
        update_tuning("vless", "defaults", {})
    assert capture_vpn_config() == before


def test_tuning_ui_auth_csrf_and_saved_values(configured):
    set_setting("config.admin_password", "test-password")
    with TestClient(main.app, base_url="http://panel.local") as client:
        assert client.get("/admin/tuning").status_code == 401
        auth = ("admin", "test-password")
        page = client.get("/admin/tuning", auth=auth)
        assert page.status_code == 200 and "Рандом" in page.text and "По умолчанию" in page.text
        assert page.headers["cache-control"] == "private, no-store"
        assert client.post("/admin/tuning/vless", auth=auth, data={"action": "random"},
                           headers={"Origin": "http://evil.local"}).status_code == 403
        response = client.post("/admin/tuning/vless", auth=auth, data={"action": "random"},
                               headers={"Origin": "http://panel.local"})
        assert response.status_code == 200 and "Настройки сохранены" in response.text
        response = client.post("/admin/tuning/vless", auth=auth, data={"port": "22", "action": "save"},
                               headers={"Origin": "http://panel.local"})
        assert response.status_code == 400 and "Порт занят" in response.text


def test_generated_awg_parameters_are_valid(configured):
    from app.amnezia import generate_obfuscation_settings
    for _ in range(100):
        values = {key: str(value) for key, value in generate_obfuscation_settings().items()}
        validate_values("amnezia", values | {"port": "51820"}, configured)


def test_defaults_preserve_legacy_profile_outside_new_editing_limits(configured):
    # Older installations generated Jmax up to 1299; restoration must still work.
    set_setting("amnezia.jmax", "1299")
    original = profile_values(capture_vpn_config(), "amnezia")
    update_tuning("amnezia", "random", {})
    update_tuning("amnezia", "defaults", {})
    assert profile_values(capture_vpn_config(), "amnezia") == original


def test_custom_ssh_port_is_reserved(configured):
    set_setting("config.eu_ssh_port", "2222")
    with pytest.raises(ValueError, match="SSH"):
        update_tuning("vless", "save", profile_values(configured, "vless") | {"port": "2222"})
