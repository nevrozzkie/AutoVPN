"""Per-operation SSH credentials; also propagated by asyncio.to_thread."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class SshCredentials:
    user: str
    port: int
    key_path: str
    password: str = ""


_credentials: ContextVar[SshCredentials | None] = ContextVar("autovpn_ssh_credentials", default=None)


def current_ssh_credentials() -> SshCredentials | None:
    return _credentials.get()


@contextmanager
def use_ssh_credentials(credentials: SshCredentials) -> Iterator[None]:
    token = _credentials.set(credentials)
    try:
        yield
    finally:
        _credentials.reset(token)
