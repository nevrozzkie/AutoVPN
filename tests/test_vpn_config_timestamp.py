import os
import tempfile
from datetime import datetime

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import create_client, get_setting, init_db, update_client_name


def test_client_changes_mark_vpn_config_updated() -> None:
    init_db()

    client = create_client("Alice")
    created_timestamp = get_setting("vpn.config_updated_at")

    assert datetime.fromisoformat(created_timestamp)

    update_client_name(client["id"], "Bob")

    assert datetime.fromisoformat(get_setting("vpn.config_updated_at"))
