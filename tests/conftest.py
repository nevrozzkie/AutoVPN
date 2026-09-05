from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest


# Pytest imports conftest before collecting test modules. Set the path here and
# also update an already-imported Settings instance defensively, so collection
# order (or a plugin import) can never point the suite at ./data.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="autovpn-tests-"))
_TEST_DATABASE = _TEST_DATA_DIR / "autovpn.sqlite3"
os.environ["DATABASE_PATH"] = str(_TEST_DATABASE)

from app.config import settings  # noqa: E402

object.__setattr__(settings, "database_path", str(_TEST_DATABASE))


def _remove_test_database() -> None:
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{_TEST_DATABASE}{suffix}")
        if path.exists():
            path.unlink()


@pytest.fixture(autouse=True)
def isolated_database() -> None:
    _remove_test_database()
    yield
    _remove_test_database()


def pytest_sessionfinish() -> None:
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
