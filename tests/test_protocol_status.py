import asyncio
import os
import tempfile

os.environ["DATABASE_PATH"] = tempfile.NamedTemporaryFile(delete=True).name

from app.db import get_setting, init_db
from app.protocol_status import refresh_protocol_statuses


def test_refresh_protocol_statuses_treats_hysteria_as_udp_configured(monkeypatch) -> None:
    init_db()

    async def fake_tcp_check(host: str, port: int) -> bool:
        return port == 443

    monkeypatch.setattr("app.protocol_status.tcp_check", fake_tcp_check)

    statuses = asyncio.run(refresh_protocol_statuses("203.0.113.10"))
    by_key = {status["key"]: status for status in statuses}

    assert by_key["hysteria"]["status"] == "CONFIGURED_UDP"
    assert by_key["amnezia"]["status"] == "CONFIGURED_UDP"
    assert get_setting("protocol.hysteria.failed_since") == ""
