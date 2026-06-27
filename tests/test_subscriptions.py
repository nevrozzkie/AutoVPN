import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import create_client, init_db
from app.subscriptions import build_subscription


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
