from __future__ import annotations

import asyncio
import stat
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import serialization

import app.sprintbox_ssh as managed
from app.ssh_credentials import SshCredentials, current_ssh_credentials, use_ssh_credentials


@pytest.fixture
def key_path(tmp_path, monkeypatch):
    path = tmp_path / "ssh" / "managed_ed25519"
    monkeypatch.setattr(managed, "settings", replace(managed.settings, sprintbox_ssh_key_path=str(path)))
    return path


def test_generate_reuse_permissions_and_public_key(key_path):
    first = managed.prepare_sprintbox_ssh()
    private = key_path.read_bytes()
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    key = serialization.load_ssh_private_key(private, password=None)
    assert first.public_key == key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    assert first.credentials == SshCredentials("root", 22, str(key_path))
    assert "PRIVATE" not in first.public_key
    key_path.chmod(0o644)
    second = managed.prepare_sprintbox_ssh()
    assert second == first and key_path.read_bytes() == private
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_concurrent_creation_keeps_one_identity(key_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(lambda _: managed.prepare_sprintbox_ssh(), range(8)))
    assert len({item.public_key for item in identities}) == 1


def test_invalid_existing_key_is_not_replaced_or_leaked(key_path):
    key_path.parent.mkdir()
    key_path.write_bytes(b"private-secret-sentinel")
    with pytest.raises(RuntimeError) as error:
        managed.prepare_sprintbox_ssh()
    assert "private-secret-sentinel" not in str(error.value)
    assert key_path.read_bytes() == b"private-secret-sentinel"


def test_key_symlink_is_rejected_without_touching_target(key_path, tmp_path):
    key_path.parent.mkdir()
    target = tmp_path / "other-key"
    target.write_bytes(b"do-not-touch")
    key_path.symlink_to(target)
    with pytest.raises(RuntimeError):
        managed.prepare_sprintbox_ssh()
    assert target.read_bytes() == b"do-not-touch"


def test_ssh_override_is_thread_local_and_restores_after_failure():
    credentials = SshCredentials("root", 22, "/managed/key")
    async def check():
        outside = asyncio.create_task(asyncio.to_thread(current_ssh_credentials))
        with pytest.raises(ValueError):
            with use_ssh_credentials(credentials):
                assert await asyncio.to_thread(current_ssh_credentials) == credentials
                assert await outside is None
                raise ValueError("stop")
        assert current_ssh_credentials() is None
    asyncio.run(check())
