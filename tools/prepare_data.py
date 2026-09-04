#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote


def _open_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)


def _verify_database(path: Path) -> None:
    with closing(_open_read_only(path)) as connection:
        result = connection.execute("PRAGMA integrity_check").fetchone()
    if result != ("ok",):
        raise sqlite3.DatabaseError(f"SQLite integrity check failed for {path}: {result}")


def _sqlite_backup(source: Path, destination: Path) -> None:
    temporary = destination.with_name(f".{destination.name}.tmp-{os.getpid()}")
    try:
        temporary.unlink(missing_ok=True)
        with closing(_open_read_only(source)) as source_connection, closing(
            sqlite3.connect(temporary)
        ) as destination_connection:
            source_connection.backup(destination_connection)
        _verify_database(temporary)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def prepare_data_storage(
    database: Path,
    backup_dir: Path,
    *,
    legacy_database: Path | None = None,
    timestamp: str | None = None,
) -> Path | None:
    database = database.expanduser().resolve()
    backup_dir = backup_dir.expanduser().resolve()
    legacy_database = legacy_database.expanduser().resolve() if legacy_database else None

    database.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(database.parent, 0o700)
    os.chmod(backup_dir, 0o700)

    source = database if database.is_file() else None
    if source is None and legacy_database and legacy_database.is_file():
        source = legacy_database
    if source is None:
        return None

    os.chmod(source, 0o600)
    timestamp = timestamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / f"autovpn-pre-upgrade-{timestamp}.sqlite3"
    if backup.exists():
        raise FileExistsError(f"Refusing to overwrite existing backup: {backup}")

    _sqlite_backup(source, backup)
    if source != database and not database.exists():
        _sqlite_backup(backup, database)

    os.chmod(backup, 0o600)
    if database.exists():
        os.chmod(database, 0o600)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create and verify a consistent AutoVPN SQLite backup, then migrate legacy data if needed."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--backup-dir", required=True, type=Path)
    parser.add_argument("--legacy-database", type=Path)
    args = parser.parse_args()

    backup = prepare_data_storage(
        args.database,
        args.backup_dir,
        legacy_database=args.legacy_database,
    )
    if backup:
        print(f"Verified SQLite backup: {backup}")
    else:
        print("No existing SQLite database; initialized secure data directories.")


if __name__ == "__main__":
    main()
