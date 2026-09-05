from __future__ import annotations

import json
import shlex
from typing import Any

from app.amnezia import render_amnezia_server_config
from app.hysteria_auth import render_hysteria_server_auth
from app.vpn_config import CapturedVpnConfig


def _xray_config(config: CapturedVpnConfig) -> dict[str, Any]:
    clients = [
        {
            "id": client.vless_uuid,
            "email": _xray_client_email(client.name, client.id),
            "flow": "xtls-rprx-vision",
        }
        for client in config.enabled_clients
    ]
    payload: dict[str, Any] = {
        "log": {"loglevel": "warning"},
        "api": {"tag": "api", "services": ["StatsService"]},
        "stats": {},
        "policy": {
            "levels": {
                "0": {"statsUserUplink": True, "statsUserDownlink": True}
            },
            "system": {
                "statsInboundUplink": True,
                "statsInboundDownlink": True,
                "statsOutboundUplink": True,
                "statsOutboundDownlink": True,
            },
        },
        "inbounds": [
            {
                "tag": "api",
                "listen": "127.0.0.1",
                "port": 10085,
                "protocol": "dokodemo-door",
                "settings": {"address": "127.0.0.1"},
            }
        ],
        "outbounds": [
            {"protocol": "freedom", "tag": "direct"},
            {"protocol": "blackhole", "tag": "blocked"},
        ],
        "routing": {
            "rules": [
                {
                    "type": "field",
                    "inboundTag": ["api"],
                    "outboundTag": "api",
                }
            ]
        },
    }
    if config.vless.protocol.enabled:
        payload["inbounds"].append(
            {
                "tag": "vless-in",
                "listen": "0.0.0.0",
                "port": config.vless.protocol.port,
                "protocol": "vless",
                "settings": {"clients": clients, "decryption": "none"},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "target": config.vless.target,
                        "serverNames": list(config.vless.server_names),
                        "privateKey": config.vless.private_key,
                        "shortIds": [config.vless.short_id],
                    },
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            }
        )
    return payload


def _xray_client_email(name: str, client_id: int) -> str:
    import re

    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", name.strip()).strip("-")
    return f"{label or 'client'}-{client_id}"


def _hysteria_config(config: CapturedVpnConfig, cert_path: str, key_path: str) -> str:
    return f"""listen: :{config.hysteria.protocol.port}

tls:
  cert: {cert_path}
  key: {key_path}

{render_hysteria_server_auth(config)}

obfs:
  type: salamander
  salamander:
    password: {json.dumps(config.hysteria.obfs_password)}

masquerade:
  type: proxy
  proxy:
    url: https://example.com/
    rewriteHost: true
"""


def _write_artifacts(config: CapturedVpnConfig) -> str:
    blocks = []
    if config.vless.protocol.enabled:
        blocks.append(
            "cat >\"$STAGE/xray.json\" <<'AUTOVPN_XRAY'\n"
            + json.dumps(_xray_config(config), indent=2, ensure_ascii=False)
            + "\nAUTOVPN_XRAY\n"
        )
    if config.hysteria.protocol.enabled:
        validation_config = _hysteria_config(
            config, "__AUTOVPN_CERT__", "__AUTOVPN_KEY__"
        )
        blocks.append(
            "cat >\"$STAGE/hysteria.template.yaml\" <<'AUTOVPN_HYSTERIA'\n"
            + validation_config
            + "AUTOVPN_HYSTERIA\n"
            + "sed -e \"s#__AUTOVPN_CERT__#$STAGE/hysteria.crt#g\" "
            + "-e \"s#__AUTOVPN_KEY__#$STAGE/hysteria.key#g\" "
            + "\"$STAGE/hysteria.template.yaml\" >\"$STAGE/hysteria.validate.yaml\"\n"
            + "sed -e 's#__AUTOVPN_CERT__#/etc/autovpn/hysteria.crt#g' "
            + "-e 's#__AUTOVPN_KEY__#/etc/autovpn/hysteria.key#g' "
            + "\"$STAGE/hysteria.template.yaml\" >\"$STAGE/hysteria.yaml\"\n"
        )
    if config.amnezia.protocol.enabled:
        blocks.append(
            "cat >\"$STAGE/awg0.conf\" <<'AUTOVPN_AWG'\n"
            + render_amnezia_server_config(config)
            + "AUTOVPN_AWG\n"
        )
    return "\n".join(blocks)


def _hysteria_validation_probe() -> str:
    # Hysteria 2 has no --check flag. Validate with its real parser/server in a
    # private network namespace: no live port collision or external clients.
    # timeout supervises the entire process group, including cleanup if the
    # child ignores TERM. Never print the private log (it may contain secrets).
    probe = r'''
set -eu
ulimit -f 1024
hysteria server --config "$1" --disable-update-check --log-level info --log-format json >"$2" 2>&1 &
probe_pid=$!
cleanup_probe() {
  kill -TERM "$probe_pid" 2>/dev/null || true
  wait "$probe_pid" 2>/dev/null || true
}
trap cleanup_probe EXIT
trap 'exit 1' INT TERM
while kill -0 "$probe_pid" 2>/dev/null; do
  if grep -Fq '"msg":"server up and running"' "$2"; then
    sleep 0.1
    kill -0 "$probe_pid" 2>/dev/null || exit 1
    exit 0
  fi
  sleep 0.1
done
exit 1
'''
    return (
        "command -v unshare >/dev/null 2>&1 && command -v timeout >/dev/null 2>&1 || "
        "fail_validation 'hysteria validation requires util-linux and coreutils'\n"
        "timeout --signal=TERM --kill-after=2s 15s "
        "unshare --net --fork --kill-child=KILL sh -c "
        + shlex.quote(probe)
        + ' sh "$STAGE/hysteria.validate.yaml" "$STAGE/hysteria.validate.log" '
        ">/dev/null 2>&1 || fail_validation "
        "'hysteria isolated startup validation failed (config, network namespace or timeout)'"
    )


def _validation_script(config: CapturedVpnConfig) -> str:
    commands = []
    if config.vless.protocol.enabled:
        commands.append(
            "command -v xray >/dev/null 2>&1 || fail_validation 'xray is unavailable'\n"
            "xray run -test -config \"$STAGE/xray.json\" >/dev/null 2>&1 || "
            "fail_validation 'xray config validation failed'"
        )
    if config.hysteria.protocol.enabled:
        sni = shlex.quote(config.vless.server_name)
        commands.append(
            f"HYSTERIA_SNI={sni}\n"
            "if [ -f \"$LIVE_HYSTERIA_CERT\" ] && [ -f \"$LIVE_HYSTERIA_KEY\" ] "
            "&& openssl x509 -in \"$LIVE_HYSTERIA_CERT\" -noout -subject "
            "-nameopt RFC2253 2>/dev/null | grep -Fq \"CN=$HYSTERIA_SNI\"; then\n"
            "  cp -p \"$LIVE_HYSTERIA_CERT\" \"$STAGE/hysteria.crt\"\n"
            "  cp -p \"$LIVE_HYSTERIA_KEY\" \"$STAGE/hysteria.key\"\n"
            "else\n"
            "  openssl req -x509 -newkey rsa:2048 -nodes "
            "-keyout \"$STAGE/hysteria.key\" -out \"$STAGE/hysteria.crt\" "
            "-days 3650 -subj \"/CN=$HYSTERIA_SNI\" >/dev/null 2>&1 "
            "|| fail_validation 'hysteria certificate generation failed'\n"
            "fi\n"
            "command -v hysteria >/dev/null 2>&1 || "
            "fail_validation 'hysteria is unavailable'\n"
            + _hysteria_validation_probe()
        )
    if config.amnezia.protocol.enabled:
        commands.append(
            "command -v awg-quick >/dev/null 2>&1 || "
            "fail_validation 'awg-quick is unavailable'\n"
            "awg-quick strip \"$STAGE/awg0.conf\" >/dev/null 2>&1 || "
            "fail_validation 'AmneziaWG config validation failed'"
        )
    return "\n".join(commands)


def _service_switch_script(config: CapturedVpnConfig) -> str:
    values = {
        "xray": config.vless.protocol.enabled,
        "hysteria-server": config.hysteria.protocol.enabled,
        "awg-quick@awg0": config.amnezia.protocol.enabled,
    }
    return "\n".join(
        f"apply_service {shlex.quote(service)} {'1' if enabled else '0'}"
        for service, enabled in values.items()
    )


def _service_check_script(config: CapturedVpnConfig) -> str:
    values = {
        "xray": config.vless.protocol.enabled,
        "hysteria-server": config.hysteria.protocol.enabled,
        "awg-quick@awg0": config.amnezia.protocol.enabled,
    }
    checks = []
    for service, enabled in values.items():
        quoted = shlex.quote(service)
        if enabled:
            checks.append(
                f"systemctl is-active --quiet {quoted} || "
                f"fail_apply 'service check failed: {service}'"
            )
        else:
            checks.append(
                f"if systemctl is-active --quiet {quoted}; then "
                f"fail_apply 'disabled service is still active: {service}'; fi\n"
                f"if systemctl is-enabled --quiet {quoted}; then "
                f"fail_apply 'disabled service is still enabled: {service}'; fi"
            )
    return "\n".join(checks)


def _firewall_script(config: CapturedVpnConfig) -> str:
    ports: list[tuple[int, str]] = []
    if config.vless.protocol.enabled:
        ports.append((config.vless.protocol.port, "tcp"))
    if config.hysteria.protocol.enabled:
        ports.append((config.hysteria.protocol.port, "udp"))
    if config.amnezia.protocol.enabled:
        ports.append((config.amnezia.protocol.port, "udp"))
    blocks = []
    for port, protocol in ports:
        blocks.append(
            f"if command -v ufw >/dev/null 2>&1; then ufw allow {port}/{protocol} >/dev/null || true; fi\n"
            f"if command -v iptables >/dev/null 2>&1; then iptables -C INPUT -p {protocol} --dport {port} -j ACCEPT 2>/dev/null || iptables -I INPUT -p {protocol} --dport {port} -j ACCEPT >/dev/null || true; fi\n"
            f"if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then firewall-cmd --permanent --add-port={port}/{protocol} >/dev/null || true; firewall-cmd --reload >/dev/null || true; fi"
        )
    return "\n".join(blocks)


def render_config_apply_script(
    config: CapturedVpnConfig,
    *,
    revision: int | None = None,
) -> str:
    """Render a config-only staged apply script without package/network installers."""
    revision = config.revision if revision is None else revision
    write_artifacts = _write_artifacts(config)
    validate = _validation_script(config)
    switch_services = _service_switch_script(config)
    check_services = _service_check_script(config)
    firewall = _firewall_script(config)
    xray_enabled = "1" if config.vless.protocol.enabled else "0"
    hysteria_enabled = "1" if config.hysteria.protocol.enabled else "0"
    amnezia_enabled = "1" if config.amnezia.protocol.enabled else "0"

    return f"""#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${{AUTOVPN_ROOT:-}}"
STAGE_PARENT="$ROOT/etc/autovpn"
BACKUP_PARENT="$ROOT/var/lib/autovpn/config-backups"
LIVE_XRAY="$ROOT/usr/local/etc/xray/config.json"
LIVE_HYSTERIA="$ROOT/etc/hysteria/config.yaml"
LIVE_HYSTERIA_CERT="$ROOT/etc/autovpn/hysteria.crt"
LIVE_HYSTERIA_KEY="$ROOT/etc/autovpn/hysteria.key"
LIVE_AWG="$ROOT/etc/amnezia/amneziawg/awg0.conf"

install -d -m 0700 "$STAGE_PARENT"
STAGE="$(mktemp -d "$STAGE_PARENT/apply-r{revision}.XXXXXX")"
chmod 0700 "$STAGE"
BACKUP=""
SWITCH_STARTED=0

cleanup() {{
  rm -rf -- "$STAGE"
}}

fail_validation() {{
  echo "[autovpn] validation failed: $1" >&2
  return 1
}}

fail_apply() {{
  echo "[autovpn] apply failed: $1" >&2
  return 1
}}

backup_file() {{
  local source="$1" name="$2"
  if [ -e "$source" ]; then
    cp -a -- "$source" "$BACKUP/$name"
    : >"$BACKUP/$name.present"
  fi
}}

prepare_backup() {{
  local revision_padded created_at
  revision_padded="$(printf '%020d' {revision})"
  created_at="$(date -u +%Y%m%dT%H%M%SZ)"
  install -d -m 0700 "$BACKUP_PARENT"
  BACKUP="$(mktemp -d "$BACKUP_PARENT/revision-$revision_padded-$created_at.XXXXXX")"
  chmod 0700 "$BACKUP"
  backup_file "$LIVE_XRAY" xray.json
  backup_file "$LIVE_HYSTERIA" hysteria.yaml
  backup_file "$LIVE_HYSTERIA_CERT" hysteria.crt
  backup_file "$LIVE_HYSTERIA_KEY" hysteria.key
  backup_file "$LIVE_AWG" awg0.conf
  capture_service xray xray
  capture_service hysteria-server hysteria
  capture_service awg-quick@awg0 awg
  {{
    printf 'revision=%s\n' {revision}
    printf 'created_at=%s\n' "$created_at"
    for name in xray.json hysteria.yaml hysteria.crt hysteria.key awg0.conf; do
      if [ -f "$BACKUP/$name.present" ]; then printf '%s=present\n' "$name"; else printf '%s=absent\n' "$name"; fi
    done
    printf 'xray_enabled=%s\n' "$(cat "$BACKUP/xray.enabled")"
    printf 'xray_active=%s\n' "$(cat "$BACKUP/xray.active")"
    printf 'hysteria_enabled=%s\n' "$(cat "$BACKUP/hysteria.enabled")"
    printf 'hysteria_active=%s\n' "$(cat "$BACKUP/hysteria.active")"
    printf 'awg_enabled=%s\n' "$(cat "$BACKUP/awg.enabled")"
    printf 'awg_active=%s\n' "$(cat "$BACKUP/awg.active")"
  }} >"$BACKUP/manifest.pending"
  chmod 0600 "$BACKUP/manifest.pending"
  mv -f -- "$BACKUP/manifest.pending" "$BACKUP/manifest.ready"
}}

mark_backup_result() {{
  local result="$1"
  printf 'result=%s\n' "$result" >"$BACKUP/result.pending"
  chmod 0600 "$BACKUP/result.pending"
  mv -f -- "$BACKUP/result.pending" "$BACKUP/$result"
}}

prune_backups() {{
  local kept=0 candidate
  while IFS= read -r candidate; do
    kept=$((kept + 1))
    if [ "$kept" -gt 10 ]; then rm -rf -- "$candidate"; fi
  done < <(find "$BACKUP_PARENT" -mindepth 1 -maxdepth 1 -type d -name 'revision-*' -print | LC_ALL=C sort -r)
}}

restore_file() {{
  local target="$1" name="$2"
  if [ -f "$BACKUP/$name.present" ]; then
    install -d -m 0755 "$(dirname "$target")"
    cp -a -- "$BACKUP/$name" "$target.autovpn-rollback"
    mv -f -- "$target.autovpn-rollback" "$target"
  else
    rm -f -- "$target"
  fi
}}

capture_service() {{
  local service="$1" key="$2"
  if ! systemctl is-enabled "$service" >"$BACKUP/$key.enabled" 2>/dev/null && [ ! -s "$BACKUP/$key.enabled" ]; then
    printf 'disabled\n' >"$BACKUP/$key.enabled"
  fi
  if ! systemctl is-active "$service" >"$BACKUP/$key.active" 2>/dev/null && [ ! -s "$BACKUP/$key.active" ]; then
    printf 'inactive\n' >"$BACKUP/$key.active"
  fi
}}

restore_service() {{
  local service="$1" key="$2" enabled active
  enabled="$(cat "$BACKUP/$key.enabled")"
  active="$(cat "$BACKUP/$key.active")"
  case "$enabled" in
    enabled|enabled-runtime) systemctl enable "$service" >/dev/null 2>&1 || true ;;
    masked|masked-runtime) systemctl mask "$service" >/dev/null 2>&1 || true ;;
    static|indirect|generated|transient) : ;;
    *) systemctl disable "$service" >/dev/null 2>&1 || true ;;
  esac
  case "$active" in
    active|reloading|activating)
    systemctl restart "$service" >/dev/null 2>&1 || systemctl start "$service" >/dev/null 2>&1 || true
    ;;
    *) systemctl stop "$service" >/dev/null 2>&1 || true ;;
  esac
}}

rollback() {{
  set +e
  restore_file "$LIVE_XRAY" xray.json
  restore_file "$LIVE_HYSTERIA" hysteria.yaml
  restore_file "$LIVE_HYSTERIA_CERT" hysteria.crt
  restore_file "$LIVE_HYSTERIA_KEY" hysteria.key
  restore_file "$LIVE_AWG" awg0.conf
  restore_service xray xray
  restore_service hysteria-server hysteria
  restore_service awg-quick@awg0 awg
  mark_backup_result ROLLED_BACK
  echo "[autovpn] previous VPN configuration and service state restored" >&2
}}

on_error() {{
  local exit_code=$?
  trap - ERR
  if [ "$SWITCH_STARTED" = 1 ]; then
    rollback
  elif [ -n "$BACKUP" ] && [ ! -f "$BACKUP/manifest.ready" ]; then
    rm -rf -- "$BACKUP"
  fi
  if [ -d "$BACKUP_PARENT" ]; then prune_backups; fi
  cleanup
  exit "$exit_code"
}}

trap on_error ERR
trap cleanup EXIT

{write_artifacts}

echo "[autovpn] validating staged VPN configuration revision {revision}"
{validate}

prepare_backup

atomic_install() {{
  local source="$1" target="$2" mode="$3"
  install -d -m 0755 "$(dirname "$target")"
  install -m "$mode" "$source" "$target.autovpn-new"
  mv -f -- "$target.autovpn-new" "$target"
}}

apply_service() {{
  local service="$1" enabled="$2"
  if [ "$enabled" = 1 ]; then
    systemctl enable "$service" >/dev/null
    systemctl restart "$service"
  else
    systemctl stop "$service" >/dev/null 2>&1 || true
    systemctl disable "$service" >/dev/null 2>&1 || true
  fi
}}

SWITCH_STARTED=1
if [ {xray_enabled} = 1 ]; then atomic_install "$STAGE/xray.json" "$LIVE_XRAY" 0600; else rm -f -- "$LIVE_XRAY"; fi
if [ {hysteria_enabled} = 1 ]; then
  atomic_install "$STAGE/hysteria.yaml" "$LIVE_HYSTERIA" 0640
  atomic_install "$STAGE/hysteria.crt" "$LIVE_HYSTERIA_CERT" 0644
  atomic_install "$STAGE/hysteria.key" "$LIVE_HYSTERIA_KEY" 0640
  if id hysteria >/dev/null 2>&1; then chown root:hysteria "$LIVE_HYSTERIA" "$LIVE_HYSTERIA_CERT" "$LIVE_HYSTERIA_KEY"; fi
else
  rm -f -- "$LIVE_HYSTERIA" "$LIVE_HYSTERIA_CERT" "$LIVE_HYSTERIA_KEY"
fi
if [ {amnezia_enabled} = 1 ]; then atomic_install "$STAGE/awg0.conf" "$LIVE_AWG" 0600; else rm -f -- "$LIVE_AWG"; fi

{switch_services}
sleep 1
{check_services}

{firewall}

mark_backup_result APPLIED
BACKUP_ID="$(basename "$BACKUP")"
SWITCH_STARTED=0
trap - ERR
prune_backups || true
cleanup || true
trap - EXIT
echo "[autovpn] VPN configuration revision {revision} applied; backup $BACKUP_ID retained"
"""


def _bootstrap_body(config: CapturedVpnConfig) -> str:
    install_xray = config.vless.protocol.enabled
    install_hysteria = config.hysteria.protocol.enabled
    install_awg = config.amnezia.protocol.enabled
    requirements = ["[ ! -f \"$BOOTSTRAP_MARKER\" ]"]
    if install_xray:
        requirements.append("! command -v xray >/dev/null 2>&1")
    if install_hysteria:
        requirements.extend(
            [
                "! command -v hysteria >/dev/null 2>&1",
                "! command -v openssl >/dev/null 2>&1",
                "! command -v unshare >/dev/null 2>&1",
                "! command -v timeout >/dev/null 2>&1",
            ]
        )
    if install_awg:
        requirements.append("! command -v awg-quick >/dev/null 2>&1")
    condition = " || ".join(requirements)
    xray = (
        'bash -c "$(curl -fsSL https://github.com/XTLS/Xray-install/raw/main/install-release.sh)" -- install'
        if install_xray
        else ":"
    )
    hysteria = (
        'bash -c "$(curl -fsSL https://get.hy2.sh/)"'
        if install_hysteria
        else ":"
    )
    awg = "install_amneziawg_best_effort" if install_awg else ":"
    return f"""ROOT="${{AUTOVPN_ROOT:-}}"
BOOTSTRAP_MARKER="$ROOT/var/lib/autovpn/bootstrap-v1"
if {condition}; then
  export DEBIAN_FRONTEND=noninteractive
  remove_amnezia_apt_sources() {{
    find /etc/apt/sources.list.d -maxdepth 1 -type f \\( -iname '*amnezia*' -o -iname '*ppa_amnezia_ppa*' \\) -delete 2>/dev/null || true
  }}
  install_amneziawg_best_effort() {{
    remove_amnezia_apt_sources
    if apt-get update && apt-cache show amneziawg >/dev/null 2>&1 && apt-get install -y amneziawg; then return 0; fi
    local codename=""
    if [ -r /etc/os-release ]; then codename="$(. /etc/os-release && printf '%s' "${{UBUNTU_CODENAME:-${{VERSION_CODENAME:-}}}}")"; fi
    case "$codename" in focal|jammy|noble) add-apt-repository -y ppa:amnezia/ppa || true ;; esac
    apt-get update || true
    apt-get install -y amneziawg || true
    remove_amnezia_apt_sources
  }}
  remove_amnezia_apt_sources
  apt-get update
  apt-get install -y ca-certificates coreutils curl gnupg iptables openssl software-properties-common unzip util-linux
  {xray}
  {hysteria}
  {awg}
  install -d -m 0700 "$ROOT/var/lib/autovpn"
  : >"$BOOTSTRAP_MARKER"
else
  echo "[autovpn] bootstrap already satisfied"
fi
"""


def render_bootstrap_script(config: CapturedVpnConfig) -> str:
    return "#!/usr/bin/env bash\nset -Eeuo pipefail\n\n" + _bootstrap_body(config)


def render_transactional_install_script(config: CapturedVpnConfig) -> str:
    apply_script = render_config_apply_script(config, revision=config.revision)
    apply_body = apply_script.split("\n", 3)[-1]
    return (
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n\n"
        + _bootstrap_body(config)
        + "\n"
        + apply_body
    )
