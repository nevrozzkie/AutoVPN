from fastapi.testclient import TestClient

from app.db import init_db, set_setting
from app.security import (
    LoginRateLimiter,
    hash_password,
    is_hashed,
    is_public_token_path,
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
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


def test_robots_txt_disallows_all() -> None:
    client = _client()
    response = client.get("/robots.txt")

    assert response.status_code == 200
    assert response.text == "User-agent: *\nDisallow: /\n"
    assert response.headers["X-Robots-Tag"] == "noindex, nofollow, noarchive"


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


def test_csrf_allows_zero_bind_loopback_alias() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "0.0.0.0:8000",
            "Origin": "http://127.0.0.1:8000",
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)


def test_csrf_allows_null_origin_for_loopback_host() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "localhost:8000",
            "Origin": "null",
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)


def test_csrf_blocks_null_origin_for_non_loopback_host() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "vpn.example.com",
            "Origin": "null",
        },
    )

    assert response.status_code == 403


def test_csrf_allows_null_origin_for_external_host_with_same_origin_metadata() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "vpn.example.com",
            "Origin": "null",
            "Sec-Fetch-Site": "same-origin",
        },
        follow_redirects=False,
    )

    assert response.status_code in (200, 303)


def test_csrf_blocks_null_origin_with_cross_site_metadata() -> None:
    client = _client()
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        auth=("admin", "pw12345"),
        headers={
            "Host": "localhost:8000",
            "Origin": "null",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403


def test_public_token_path_detection_is_narrow() -> None:
    for path in (
        "/sub/token",
        "/sing-box/token",
        "/ip/token",
        "/amnezia/token",
        "/amnezia-key/token",
        "/client/token",
        "/client/token/subscription.qr",
        "/client/token/amnezia.qr",
        "/client/token/protocols/refresh",
    ):
        assert is_public_token_path(path)

    for path in ("/admin", "/admin/clients", "/client", "/client/token/unknown", "/static/token"):
        assert not is_public_token_path(path)


def test_csrf_blocked_public_token_route_keeps_private_cache_headers() -> None:
    client = _client()
    response = client.post(
        "/client/secret-token/protocols/refresh",
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "secret-token" not in response.text


def test_csrf_error_does_not_echo_secret_referer() -> None:
    client = _client()
    secret = "do-not-echo-client-token"
    response = client.post(
        "/admin/clients",
        data={"name": "x"},
        headers={"Referer": f"http://evil.example/client/{secret}"},
    )

    assert response.status_code == 403
    assert secret not in response.text
    assert response.text == "Cross-origin request blocked (CSRF protection)."


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
