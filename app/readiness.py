from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from app.migrations import MIGRATIONS


def database_is_ready(database_path: str) -> bool:
    """Check the database schema without creating or migrating the database."""
    if database_path == ":memory:" or database_path.startswith("file:"):
        return False
    path = Path(database_path)
    if not path.is_file():
        return False
    expected = {
        migration.version: (migration.name, migration.checksum)
        for migration in MIGRATIONS
    }
    try:
        with closing(
            sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("SELECT 1").fetchone()
            rows = connection.execute(
                "SELECT version, name, checksum FROM schema_migrations"
            ).fetchall()
    except (OSError, sqlite3.Error):
        return False
    applied = {int(row[0]): (str(row[1]), str(row[2])) for row in rows}
    return applied == expected
