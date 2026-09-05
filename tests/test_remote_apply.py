from __future__ import annotations

import os
import shlex
import shutil
import sys
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

import app.eu_install as eu_install
from app.db import create_client, init_db, set_setting, update_client_name
from app.eu_install import build_eu_deploy_script, run_eu_install
from app.remote_apply import (
    _hysteria_validation_probe,
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
        "case \"$*\" in\n"
        " 'show xray.service --property=LoadState --value') echo \"${FAKE_XRAY_LOAD:-loaded}\"; exit 0 ;;\n"
        " 'show xray.service --property=DynamicUser --value') echo \"${FAKE_XRAY_DYNAMIC:-no}\"; exit 0 ;;\n"
        " 'show xray.service --property=User --value') echo \"${FAKE_XRAY_USER-}\"; exit 0 ;;\n"
        " 'show xray.service --property=Group --value') echo \"${FAKE_XRAY_GROUP-}\"; exit 0 ;;\n"
        "esac\n"
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
        "FAKE_XRAY_USER": subprocess.check_output(["id", "-un"], text=True).strip(),
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


def _probe_environment(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o700)
    fake_bin = tmp_path / "probe-bin"
    fake_bin.mkdir()
    # macOS has no Linux net namespaces. This shim tests command wiring only;
    # production always requires real unshare and has no isolation bypass.
    _write_executable(fake_bin / "unshare", '''#!/bin/sh
[ "$1 $2 $3" = "--net --fork --kill-child=KILL" ] || exit 91
[ "${FAIL_NAMESPACE:-0}" = 0 ] || exit 92
shift 3
exec "$@"
''')
    real_timeout = shutil.which("timeout") or shutil.which("gtimeout")
    assert real_timeout, "GNU coreutils timeout is required for probe tests"
    _write_executable(fake_bin / "timeout", f'''#!/bin/sh
[ "$1 $2 $3" = "--signal=TERM --kill-after=2s 15s" ] || exit 93
shift 3
exec {shlex.quote(real_timeout)} --signal=TERM --kill-after=0.2s "${{PROBE_TEST_TIMEOUT:-3s}}" "$@"
''')
    return stage, {
        **os.environ,
        "STAGE": str(stage),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "PROBE_PID_FILE": str(tmp_path / "probe.pid"),
    }


def _run_probe(environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "set -Eeuo pipefail\nfail_validation() { echo \"$1\" >&2; return 1; }\n"
         + _hysteria_validation_probe()],
        env=environment, text=True, capture_output=True, timeout=10,
    )


@pytest.mark.parametrize("mode,success", [
    ("ready", True), ("invalid", False), ("early_exit", False),
    ("silent", False), ("ignore_term", False), ("namespace_denied", False),
])
def test_hysteria_probe_requires_readiness_and_is_bounded(
    tmp_path: Path, mode: str, success: bool,
) -> None:
    stage, env = _probe_environment(tmp_path)
    env["PROBE_MODE"] = mode
    env["PROBE_TEST_TIMEOUT"] = "3s" if success else "0.6s"
    if mode == "namespace_denied":
        env["FAIL_NAMESPACE"] = "1"
    fake_bin = Path(env["PATH"].split(":")[0])
    _write_executable(fake_bin / "hysteria", '''#!/bin/sh
printf '%s' "$$" > "$PROBE_PID_FILE"
case "$PROBE_MODE" in
 invalid) echo 'private-config-secret'; exit 1 ;;
 ready|early_exit) echo '{"level":"info","msg":"server up and running"}' ;;
 ignore_term) trap '' TERM ;;
esac
[ "$PROBE_MODE" != early_exit ] || exit 1
exec sleep 60
''')
    result = _run_probe(env)
    assert (result.returncode == 0) is success, result.stderr
    assert "private-config-secret" not in result.stdout + result.stderr
    if mode == "namespace_denied":
        assert not Path(env["PROBE_PID_FILE"]).exists()
    if mode in {"ready", "silent"}:
        pid = int(Path(env["PROBE_PID_FILE"]).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_hysteria_validation_failure_never_switches_live_config(tmp_path: Path) -> None:
    root, env = _fake_remote_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":")[0])
    for tool in ("hysteria", "unshare", "timeout"):
        _write_executable(fake_bin / tool, "#!/bin/sh\nexit 1\n")
    _write_executable(fake_bin / "openssl", "#!/bin/sh\nexit 0\n")
    live = root / "etc/hysteria/config.yaml"
    live.parent.mkdir(parents=True)
    live.write_text("old-hysteria-config\n")
    config = _captured_config()
    result = _run_script(render_config_apply_script(config), env)
    assert result.returncode != 0
    assert "hysteria isolated startup validation failed" in result.stderr
    assert live.read_text() == "old-hysteria-config\n"
    assert not (tmp_path / "systemctl.log").exists()
    assert not list((root / "etc/autovpn").glob("apply-*"))


def test_hysteria_probe_tools_are_bootstrapped_and_no_check_flag_is_used() -> None:
    script = render_transactional_install_script(_captured_config())
    assert "! command -v unshare" in script
    assert "! command -v timeout" in script
    assert "ca-certificates coreutils" in script
    assert "unzip util-linux" in script
    assert "hysteria server --help" not in script
    assert " --check " not in script


@pytest.mark.parametrize("invalid", [False, True])
def test_hysteria_probe_with_real_binary(tmp_path: Path, invalid: bool) -> None:
    binary = os.environ.get("AUTOVPN_TEST_HYSTERIA_BINARY")
    if not binary:
        pytest.skip("Set AUTOVPN_TEST_HYSTERIA_BINARY for the real Hysteria smoke test")
    stage, env = _probe_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":")[0])
    (fake_bin / "hysteria").symlink_to(Path(binary).resolve())
    subprocess.run([
        "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(stage / "key.pem"), "-out", str(stage / "cert.pem"),
        "-days", "1", "-subj", "/CN=localhost",
    ], check=True, capture_output=True)
    # Bind only ephemeral loopback in this portable smoke test. Linux namespace
    # isolation itself must be checked separately on Linux, not claimed here.
    (stage / "hysteria.validate.yaml").write_text(
        f"listen: 127.0.0.1:0\ntls:\n  cert: {stage}/cert.pem\n  key: {stage}/key.pem\n"
        + ("auth:\n  type: invalid\n" if invalid else
           "auth:\n  type: userpass\n  userpass: {test-user: test-password}\n")
        + "obfs:\n  type: salamander\n  salamander:\n    password: test-obfs-password\n"
        + "masquerade:\n  type: proxy\n  proxy:\n    url: https://example.com/\n"
    )
    result = _run_probe(env)
    assert (result.returncode == 0) is not invalid, result.stderr


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


@pytest.mark.parametrize("service_user,explicit_group,mode", [
    ("nobody", "", 0o640), ("vpn-daemon", "vpn-config", 0o640),
    ("", "", 0o600), ("root", "", 0o600),
])
def test_xray_config_uses_effective_service_group_before_restart(
    tmp_path: Path, service_user: str, explicit_group: str, mode: int,
) -> None:
    root, env = _fake_remote_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":")[0])
    env.update(FAKE_XRAY_USER=service_user, FAKE_XRAY_GROUP=explicit_group)
    # Resolve simulated service identities to the test process's real group so
    # the test performs actual chmod/chgrp/rename without requiring root.
    _write_executable(fake_bin / "id", f'''#!/bin/sh
case "$1:$3" in
  -u:root) echo 0 ;;
  -u:*) echo 65534 ;;
  -g:*) echo {os.getgid()} ;;
  *) exit 1 ;;
esac
''')
    _write_executable(fake_bin / "getent", f'''#!/bin/sh
[ "$1:$2:$3" = '--:group:vpn-config' ] || exit 1
echo 'vpn-config:x:{os.getgid()}:'
''')
    config = _vless_only(_captured_config())
    live = root / "usr/local/etc/xray/config.json"
    # The assertion is invoked by the fake service at restart time, not merely
    # after the deploy has completed, to catch chmod/chgrp after restart bugs.
    check = tmp_path / "check-xray-mode.py"
    check.write_text(
        "import os, pathlib, stat\n"
        f"p = pathlib.Path({str(live)!r})\n"
        f"assert stat.S_IMODE(p.stat().st_mode) == {mode}\n"
        f"assert p.stat().st_gid == {os.getgid()}\n"
        "assert not (p.stat().st_mode & 0o007)\n"
    )
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(systemctl.read_text().replace(
        "  restart:xray)\n", f"  restart:xray)\n    {shlex.quote(sys.executable)} {shlex.quote(str(check))} || exit 23\n",
    ))
    result = _run_script(render_config_apply_script(config), env)
    assert result.returncode == 0, result.stderr
    assert 'restart xray' in (tmp_path / "systemctl.log").read_text()


@pytest.mark.parametrize("failure", ["missing_unit", "dynamic_user", "unknown_user", "unknown_group"])
def test_xray_unresolved_service_identity_preserves_live_config(tmp_path: Path, failure: str) -> None:
    root, env = _fake_remote_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":")[0])
    if failure == "missing_unit":
        env["FAKE_XRAY_LOAD"] = "not-found"
    elif failure == "dynamic_user":
        env["FAKE_XRAY_DYNAMIC"] = "yes"
    elif failure == "unknown_user":
        env["FAKE_XRAY_USER"] = "autovpn-test-nonexistent-account"
    else:
        env["FAKE_XRAY_GROUP"] = "autovpn-test-nonexistent-group"
        _write_executable(fake_bin / "getent", "#!/bin/sh\nexit 2\n")
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("previous-config\n")
    live.chmod(0o644)
    result = _run_script(render_config_apply_script(_vless_only(_captured_config())), env)
    assert result.returncode != 0
    assert "validation failed:" in result.stderr
    assert live.read_text() == "previous-config\n"
    assert live.stat().st_mode & 0o777 == 0o644
    assert not (tmp_path / "systemctl.log").exists()


def test_xray_restart_failure_restores_previous_file_permissions(tmp_path: Path) -> None:
    root, env = _fake_remote_environment(tmp_path)
    env["FAIL_RESTART"] = "1"
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("previous-config\n")
    live.chmod(0o604)
    previous = live.stat()
    result = _run_script(render_config_apply_script(_vless_only(_captured_config())), env)
    assert result.returncode != 0
    assert live.read_text() == "previous-config\n"
    assert live.stat().st_mode == previous.st_mode
    assert live.stat().st_uid == previous.st_uid
    assert live.stat().st_gid == previous.st_gid


def test_xray_group_assignment_failure_does_not_publish_new_config(tmp_path: Path) -> None:
    root, env = _fake_remote_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(":")[0])
    _write_executable(fake_bin / "chgrp", "#!/bin/sh\nexit 1\n")
    live = root / "usr/local/etc/xray/config.json"
    live.parent.mkdir(parents=True)
    live.write_text("previous-config\n")
    live.chmod(0o644)
    result = _run_script(render_config_apply_script(_vless_only(_captured_config())), env)
    assert result.returncode != 0
    assert live.read_text() == "previous-config\n"
    assert live.stat().st_mode & 0o777 == 0o644
    assert not live.with_name("config.json.autovpn-new").exists()
    assert " applied; backup " not in result.stdout


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
