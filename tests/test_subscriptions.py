import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import create_client, init_db
from app.config import settings
from app.subscriptions import build_sing_box_subscription, build_subscription


def test_subscription_contains_vless_reality_params() -> None:
    init_db()
    client = create_client("Alice")

    subscription = build_subscription(client, "203.0.113.10")

    assert "vless://" in subscription
    assert "security=reality" in subscription
    assert "sni=ok.ru" in subscription
    assert "fp=firefox" in subscription
    assert "flow=xtls-rprx-vision" in subscription
    assert "hysteria2://" in subscription
    assert f"hysteria2://client{client['id']}%3A" in subscription
    assert f"@203.0.113.10:{settings.hysteria_port}/?insecure=1&sni=autovpn-eu" in subscription
    assert "%5BAutoVPN%5D%20Alice%20-%20vless" in subscription
    assert "%5BAutoVPN%5D%20Alice%20-%20hysteria" in subscription


def test_sing_box_subscription_contains_vless_and_hysteria() -> None:
    init_db()
    client = create_client("Alice")

    subscription = build_sing_box_subscription(client, "203.0.113.10")
    outbounds = {outbound["tag"]: outbound for outbound in subscription["outbounds"]}

    assert subscription["inbounds"][0]["type"] == "tun"
    assert outbounds["proxy"]["type"] == "selector"
    assert outbounds["proxy"]["outbounds"] == ["vless-reality", "hysteria2"]
    assert outbounds["vless-reality"]["type"] == "vless"
    assert outbounds["vless-reality"]["server"] == "203.0.113.10"
    assert outbounds["vless-reality"]["server_port"] == settings.vless_port
    assert outbounds["vless-reality"]["uuid"] == client["vless_uuid"]
    assert outbounds["vless-reality"]["flow"] == "xtls-rprx-vision"
    assert outbounds["vless-reality"]["tls"]["reality"]["enabled"] is True
    assert outbounds["hysteria2"]["type"] == "hysteria2"
    assert outbounds["hysteria2"]["server_port"] == settings.hysteria_port
    assert outbounds["hysteria2"]["password"] == f"client{client['id']}:{client['hysteria_password']}"
    assert outbounds["hysteria2"]["tls"]["insecure"] is True
