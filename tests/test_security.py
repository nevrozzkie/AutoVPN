import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=False).name

from fastapi.testclient import TestClient

from app.db import init_db, set_setting
from app.security import (
    LoginRateLimiter,
    hash_password,
    is_hashed,
    verify_password,
)
import app.main as main


def _client() -> TestClient:
    init_db()
    set_setting("config.admin_username", "admin")
    set_setting("config.admin_password", hash_password("pw12345"))
    return TestClient(main.app, base_url="http://panel.local")


def test_hash_password_roundtrip() -> None:
    stored = hash_password("correct horse")
    assert is_hashed(stored)
    assert stored != "correct horse"
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong", stored)


def test_verify_password_supports_plaintext_bootstrap() -> None:
    # A plaintext (env/bootstrap) value must still verify in constant time.
    assert verify_password("bootstrap", "bootstrap")
    assert not verify_password("bootstrap", "other")
    assert not verify_password("anything", "")


def test_security_headers_present() -> None:
    client = _client()
    response = client.get("/healthz")
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_hashed_login_authenticates() -> None:
    client = _client()
    assert client.get("/admin/clients").status_code == 401
    assert client.get("/admin/clients", auth=("admin", "nope")).status_code == 401
    assert client.get("/admin/clients", auth=("admin", "pw12345")).status_code == 200


def test_csrf_cross_origin_post_blocked() -> None:
    client = _client()
    blocked = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={"Origin": "http://evil.example"},
    )
    assert blocked.status_code == 403

    allowed = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={"Origin": "http://panel.local"},
        follow_redirects=False,
    )
    assert allowed.status_code in (200, 303)


def test_csrf_allows_loopback_host_aliases() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "127.0.0.1:8000",
            "Origin": "http://localhost:8000",
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)


def test_login_rate_limiter_blocks_after_threshold() -> None:
    limiter = LoginRateLimiter(max_failures=3, window_seconds=60, block_seconds=60)
    key = "1.2.3.4"
    assert limiter.retry_after(key) == 0
    for _ in range(3):
        limiter.register_failure(key)
    assert limiter.retry_after(key) > 0
    # A successful login clears the block.
    limiter.register_success(key)
    assert limiter.retry_after(key) == 0
