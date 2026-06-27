from app.main import status_label


def test_verified_status_label_is_ok() -> None:
    assert status_label("VERIFIED") == "OK"
