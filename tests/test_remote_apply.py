from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import app.eu_install as eu_install
from app.db import create_client, init_db, set_setting, update_client_name
from app.eu_install import build_eu_deploy_script, run_eu_install
from app.remote_apply import (
    render_config_apply_script,
    render_transactional_install_script,
)
from app.vpn_config import CapturedVpnConfig, capture_vpn_config
from app.vpn_state import prepare_install_operation


def _captured_config() -> CapturedVpnConfig:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    create_client("Alice")
    return capture_vpn_config()


def _vless_only(config: CapturedVpnConfig) -> CapturedVpnConfig:
    return replace(
        config,
        hysteria=replace(
            config.hysteria,
            protocol=replace(config.hysteria.protocol, enabled=False),
        ),
        amnezia=replace(
            config.amnezia,
            protocol=replace(config.amnezia.protocol, enabled=False),
        ),
    )


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _fake_remote_environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "root"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "systemctl.log"
    _write_executable(
        fake_bin / "xray",
        "#!/bin/sh\n"
        "if [ \"${FAIL_VALIDATE:-0}\" = 1 ]; then exit 9; fi\n"
        "exit 0\n",
    )
    _write_executable(
        fake_bin / "systemctl",
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >>\"$FAKE_SYSTEMCTL_LOG\"\n"
        "service=\"${3:-$2}\"\n"
        "case \"$1:$service\" in\n"
        "  is-enabled:xray) echo enabled; exit 0 ;;\n"
        "  is-enabled:*) exit 1 ;;\n"
        "  is-active:xray) echo active; exit 0 ;;\n"
        "  is-active:*) exit 1 ;;\n"
        "  restart:xray)\n"
        "    if [ \"${FAIL_RESTART:-0}\" = 1 ] && [ ! -f \"$FAKE_RESTART_MARKER\" ]; then\n"
        "      : >\"$FAKE_RESTART_MARKER\"\n"
        "      exit 7\n"
        "    fi\n"
        "    ;;\n"
        "esac\n"
        "exit 0\n",
    )
    environment = {
        **os.environ,
        "AUTOVPN_ROOT": str(root),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "FAKE_SYSTEMCTL_LOG": str(log),
        "FAKE_RESTART_MARKER": str(tmp_path / "restart-failed"),
    }
    return root, environment


def _run_script(script: str, environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash"],
        input=script,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        check=False,
    )


def test_config_only_script_is_staged_transactional_and_has_no_bootstrap() -> None:
    config = _captured_config()

    script = render_config_apply_script(config)

    assert f"apply-r{config.revision}.XXXXXX" in script
    assert script.index("validating staged VPN configuration") < script.index(
        "SWITCH_STARTED=1"
    )
    assert "backup_file \"$LIVE_XRAY\"" in script
    assert "config-backups" in script
    assert "manifest.ready" in script
    assert "mark_backup_result APPLIED" in script
    assert "kept\" -gt 10" in script
    assert "previous VPN configuration and service state restored" in script
    assert "trap on_error ERR" in script
    assert "atomic_install" in script
    assert "systemctl is-active --quiet xray" in script
    assert "apt-get" not in script
    assert "curl -fsSL" not in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_disabled_protocols_remove_config_and_restore_prior_state_on_failure() -> None:
    script = render_config_apply_script(_vless_only(_captured_config()))

    assert 'apply_service hysteria-server 0' in script
    assert 'apply_service awg-quick@awg0 0' in script
    assert 'rm -f -- "$LIVE_HYSTERIA" "$LIVE_HYSTERIA_CERT"' in script
    assert 'rm -f -- "$LIVE_AWG"' in script
    assert "AUTOVPN_HYSTERIA" not in script
    assert "AUTOVPN_AWG" not in script
    assert "restore_service hysteria-server hysteria" in script
    assert "restore_service awg-quick@awg0 awg" in script
    assert "disabled service is still active: hysteria-server" in script
    assert "disabled service is still enabled: awg-quick@awg0" in script


def test_validation_failure_never_switches_live_config(tmp_path: Path) -> None:
    root, environment = _fake_remote_environment(tmp_path)
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("old-config\n", encoding="utf-8")
    environment["FAIL_VALIDATE"] = "1"

    result = _run_script(
        render_config_apply_script(_vless_only(_captured_config())), environment
    )

    assert result.returncode != 0
    assert live.read_text(encoding="utf-8") == "old-config\n"
    assert "validation failed: xray config validation failed" in result.stderr
    assert not (tmp_path / "systemctl.log").exists()
    assert not list((root / "etc/autovpn").glob("apply-*"))


def test_restart_failure_rolls_back_file_and_previous_service_state(
    tmp_path: Path,
) -> None:
    root, environment = _fake_remote_environment(tmp_path)
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("old-config\n", encoding="utf-8")
    environment["FAIL_RESTART"] = "1"

    result = _run_script(
        render_config_apply_script(_vless_only(_captured_config())), environment
    )

    assert result.returncode != 0
    assert live.read_text(encoding="utf-8") == "old-config\n"
    assert "previous VPN configuration and service state restored" in result.stderr
    service_calls = (tmp_path / "systemctl.log").read_text(encoding="utf-8")
    assert service_calls.count("restart xray") == 2
    backups = list((root / "var/lib/autovpn/config-backups").glob("revision-*"))
    assert len(backups) == 1
    assert (backups[0] / "manifest.ready").is_file()
    assert (backups[0] / "ROLLED_BACK").read_text(encoding="utf-8") == (
        "result=ROLLED_BACK\n"
    )
    assert not list((root / "etc/autovpn").glob("apply-*"))


def test_success_keeps_versioned_backup_and_prunes_old_backups(tmp_path: Path) -> None:
    root, environment = _fake_remote_environment(tmp_path)
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("old-config\n", encoding="utf-8")
    backup_parent = root / "var/lib/autovpn/config-backups"
    backup_parent.mkdir(parents=True)
    for index in range(11):
        (backup_parent / f"revision-{index:020d}-old.000000").mkdir()
    config = _vless_only(_captured_config())

    result = _run_script(render_config_apply_script(config), environment)

    assert result.returncode == 0
    assert "old-config" not in live.read_text(encoding="utf-8")
    backups = list(backup_parent.glob("revision-*"))
    assert len(backups) == 10
    applied = [path for path in backups if (path / "APPLIED").is_file()]
    assert len(applied) == 1
    assert (applied[0] / "manifest.ready").is_file()
    assert applied[0].name in result.stdout
    assert config.vless.private_key not in result.stdout + result.stderr


def test_transactional_full_install_bootstraps_then_reuses_same_apply() -> None:
    config = _captured_config()
    apply_script = render_config_apply_script(config)
    full_script = render_transactional_install_script(config)

    assert "BOOTSTRAP_MARKER" in full_script
    assert "apt-get install" in full_script
    assert apply_script.split("\n", 3)[-1] in full_script
    subprocess.run(["bash", "-n"], input=full_script, text=True, check=True)


def test_transactional_deploy_is_default_off_compatibility_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _captured_config()

    monkeypatch.setattr(eu_install, "transactional_vpn_apply_enabled", lambda: False)
    legacy = build_eu_deploy_script(config)
    monkeypatch.setattr(eu_install, "transactional_vpn_apply_enabled", lambda: True)
    transactional = build_eu_deploy_script(config)

    assert "installing base packages" in legacy
    assert "apply-r" not in legacy
    assert "BOOTSTRAP_MARKER" in transactional
    assert "validating staged VPN configuration" in transactional


@pytest.mark.anyio
async def test_transactional_install_uses_only_bound_immutable_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_db()
    set_setting("current_ip", "203.0.113.10")
    client = create_client("Alice")
    prepared = prepare_install_operation("203.0.113.10")
    update_client_name(client["id"], "Bob")
    scripts: list[str] = []
    updates: list[dict[str, object]] = []

    def fake_run(
        host: str, script: str, timeout: float | None = None
    ) -> tuple[int, str]:
        scripts.append(script)
        return 0, "apply ok"

    async def fake_refresh(host: str) -> list[dict[str, object]]:
        return []

    real_update = eu_install.update_install_operation

    def recording_update(operation_id: int, **fields: object) -> None:
        updates.append(fields)
        real_update(operation_id, **fields)

    monkeypatch.setattr(eu_install, "transactional_vpn_apply_enabled", lambda: True)
    monkeypatch.setattr(eu_install, "_run_script_over_ssh", fake_run)
    monkeypatch.setattr(eu_install, "refresh_protocol_statuses", fake_refresh)
    monkeypatch.setattr(eu_install, "update_install_operation", recording_update)

    await run_eu_install(prepared.operation_id)

    assert f"Alice-{client['id']}" in scripts[0]
    assert f"Bob-{client['id']}" not in scripts[0]
    assert f"revision {prepared.revision}" in scripts[0]
    assert any(
        "Transactional VPN deployment started" in str(update.get("output", ""))
        for update in updates
    )
