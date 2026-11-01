"""Managed SprintBOX key: private material never enters settings, logs or API calls."""
from __future__ import annotations

import fcntl
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.config import settings
from app.ssh_credentials import SshCredentials


@dataclass(frozen=True)
class SprintboxSsh:
    credentials: SshCredentials
    public_key: str


def managed_key_path() -> Path:
    if settings.sprintbox_ssh_key_path:
        return Path(settings.sprintbox_ssh_key_path).expanduser().absolute()
    if settings.database_path == ":memory:" or settings.database_path.startswith("file:"):
        raise RuntimeError("Set SPRINTBOX_SSH_KEY_PATH when using a SQLite URI or in-memory database")
    return Path(settings.database_path).expanduser().absolute().parent / "ssh" / "sprintbox_ed25519"


def prepare_sprintbox_ssh() -> SprintboxSsh:
    path = managed_key_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise RuntimeError("SSH key directory must not be a symlink")
        path.parent.chmod(0o700)
        lock_fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(lock_fd, "a+b") as lock:
            if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
                raise RuntimeError("SSH key lock must be a regular file")
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass  # Reuse, never rotate or replace a key behind existing servers.
            else:
                key = Ed25519PrivateKey.generate()
                with os.fdopen(fd, "wb") as output:
                    output.write(key.private_bytes(
                        serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                        serialization.NoEncryption(),
                    ))
                    output.flush()
                    os.fsync(output.fileno())
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise RuntimeError("SSH key must be a regular file")
                os.fchmod(source.fileno(), 0o600)
                key = serialization.load_ssh_private_key(source.read(), password=None)
            public_key = key.public_key().public_bytes(
                serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH,
            ).decode()
        return SprintboxSsh(SshCredentials("root", 22, str(path)), public_key)
    except Exception:
        # Parser exceptions must never expose key bytes or filesystem contents.
        raise RuntimeError("Не удалось подготовить SSH-ключ SprintBOX. Проверьте доступ пользователя панели к каталогу данных; существующий ключ не заменялся.") from None
