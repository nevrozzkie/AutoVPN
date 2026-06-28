#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/AutoVPN}"
DEFAULT_REPO_URL="https://github.com/nevrozzkie/AutoVPN.git"
REPO_URL="${AUTOVPN_REPO_URL:-$DEFAULT_REPO_URL}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"

usage() {
  cat <<EOF
Usage: bash install-local.sh [options]

Local AutoVPN installer for macOS/Linux. It does not install systemd or nginx.

Options:
  --app-dir VALUE                   App directory, default: ~/AutoVPN.
  --eu-host, --eu-ip VALUE          Target VPN VPS SSH host/IP.
  --eu-user, --eu-login VALUE       Target VPN VPS SSH user, usually root.
  --eu-port VALUE                   Target VPN VPS SSH port, default 22.
  --eu-password, --ssh-password VALUE
                                   SSH password for the target VPN VPS.
  --eu-key-path, --ssh-key VALUE    SSH private key path on this computer.
  --current-ip VALUE                Initial VPN IP stored in AutoVPN.
  --admin-username VALUE            Admin username.
  --admin-password VALUE            Admin password.
  --aeza-token VALUE                Optional Aeza API token.
  --aeza-service-id VALUE           Optional Aeza service id.
  --aeza-domain VALUE               Optional Aeza IPv4 domain/service name.
  -h, --help                        Show this help.
EOF
}

require_arg() {
  local option="$1"
  if [ "$#" -lt 2 ]; then
    echo "Missing value for $option"
    exit 1
  fi
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --app-dir)
        require_arg "$@"
        APP_DIR="$2"
        shift 2
        ;;
      --eu-host|--eu-ip)
        require_arg "$@"
        EU_SSH_HOST="$2"
        shift 2
        ;;
      --eu-user|--eu-login)
        require_arg "$@"
        EU_SSH_USER="$2"
        shift 2
        ;;
      --eu-port)
        require_arg "$@"
        EU_SSH_PORT="$2"
        shift 2
        ;;
      --eu-password|--ssh-password)
        require_arg "$@"
        EU_SSH_PASSWORD="$2"
        shift 2
        ;;
      --eu-key-path|--ssh-key)
        require_arg "$@"
        EU_SSH_KEY_PATH="$2"
        shift 2
        ;;
      --current-ip)
        require_arg "$@"
        CURRENT_IP="$2"
        shift 2
        ;;
      --admin-username)
        require_arg "$@"
        ADMIN_USERNAME="$2"
        shift 2
        ;;
      --admin-password)
        require_arg "$@"
        ADMIN_PASSWORD="$2"
        shift 2
        ;;
      --aeza-token)
        require_arg "$@"
        AEZA_TOKEN="$2"
        shift 2
        ;;
      --aeza-service-id)
        require_arg "$@"
        AEZA_SERVICE_ID="$2"
        shift 2
        ;;
      --aeza-domain)
        require_arg "$@"
        AEZA_IPV4_DOMAIN="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1"
        usage
        exit 1
        ;;
    esac
  done
}

ask() {
  local prompt="$1"
  local default="${2:-}"
  local value
  if [ -n "$default" ]; then
    read -r -p "$prompt [$default]: " value
    echo "${value:-$default}"
  else
    read -r -p "$prompt: " value
    echo "$value"
  fi
}

ask_secret() {
  local prompt="$1"
  local value
  read -r -s -p "$prompt: " value
  echo
  echo "$value"
}

yes_no() {
  local prompt="$1"
  local default="${2:-n}"
  local value
  read -r -p "$prompt [$default]: " value
  value="${value:-$default}"
  case "$value" in
    y|Y|yes|YES|Yes) return 0 ;;
    *) return 1 ;;
  esac
}

random_secret() {
  python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(32))
PY
}

sanitize_env_value() {
  printf "%s" "$1" | LC_ALL=C tr -d '[:cntrl:]'
}

env_line() {
  local key="$1"
  local value="${2:-}"
  printf "%s=%s\n" "$key" "$(sanitize_env_value "$value")"
}

require_command() {
  local name="$1"
  local hint="$2"
  if ! command -v "$name" >/dev/null 2>&1; then
    echo "$name is not installed. $hint"
    exit 1
  fi
}

prepare_source() {
  if [ -f "pyproject.toml" ] && [ -d "app" ]; then
    APP_DIR="$(pwd)"
    return
  fi

  if [ -d "$APP_DIR/.git" ]; then
    echo "[autovpn] updating $APP_DIR"
    git -C "$APP_DIR" pull --ff-only
  else
    echo "[autovpn] cloning $REPO_URL to $APP_DIR"
    git clone "$REPO_URL" "$APP_DIR"
  fi
}

write_env_file() {
  echo
  echo "[autovpn] writing configuration. Open Setup in the browser to finish configuration."
  local admin_username admin_password current_ip
  local aeza_token aeza_service_id aeza_domain
  local eu_ssh_host eu_ssh_user eu_ssh_port eu_ssh_key_path eu_ssh_password

  admin_username="${ADMIN_USERNAME:-admin}"
  admin_password="${ADMIN_PASSWORD:-}"
  current_ip="${CURRENT_IP:-}"
  if [ -z "$current_ip" ]; then
    current_ip="${EU_SSH_HOST:-}"
  fi
  aeza_token="${AEZA_TOKEN:-}"
  aeza_service_id="${AEZA_SERVICE_ID:-}"
  aeza_domain="${AEZA_IPV4_DOMAIN:-}"
  eu_ssh_host="${EU_SSH_HOST:-}"
  eu_ssh_user="${EU_SSH_USER:-root}"
  eu_ssh_user="${eu_ssh_user:-root}"
  eu_ssh_port="${EU_SSH_PORT:-22}"
  eu_ssh_port="${eu_ssh_port:-22}"
  eu_ssh_password="${EU_SSH_PASSWORD:-}"
  eu_ssh_key_path="${EU_SSH_KEY_PATH:-}"
  if [ -z "$eu_ssh_host" ]; then
    eu_ssh_password=""
    eu_ssh_key_path=""
  elif [ -n "$eu_ssh_password" ]; then
    eu_ssh_key_path=""
  fi

  mkdir -p "$APP_DIR/data"
  {
    env_line APP_HOST "$APP_HOST"
    env_line APP_PORT "$APP_PORT"
    env_line DATABASE_PATH "$APP_DIR/data/autovpn.sqlite3"
    env_line ADMIN_USERNAME "$admin_username"
    env_line ADMIN_PASSWORD "$admin_password"
    echo
    env_line AEZA_API_BASE "https://my.aeza.net"
    env_line AEZA_TOKEN "$aeza_token"
    env_line AEZA_SERVICE_ID "$aeza_service_id"
    env_line AEZA_IPV4_PAYMENT_METHOD "balance"
    env_line AEZA_IPV4_DOMAIN "$aeza_domain"
    env_line AEZA_IPV4_AFTER_PURCHASE_DELAY_SECONDS "300"
    echo
    env_line EU_SSH_HOST "$eu_ssh_host"
    env_line EU_SSH_USER "$eu_ssh_user"
    env_line EU_SSH_PORT "$eu_ssh_port"
    env_line EU_SSH_KEY_PATH "$eu_ssh_key_path"
    env_line EU_SSH_PASSWORD "$eu_ssh_password"
    env_line SSH_CONNECT_TIMEOUT_SECONDS "15"
    echo
    env_line VLESS_PORT "443"
    env_line VLESS_REALITY_TARGET "ok.ru:443"
    env_line VLESS_REALITY_SERVER_NAMES "ok.ru,www.ok.ru"
    env_line VLESS_REALITY_SERVER_NAME "ok.ru"
    env_line VLESS_REALITY_FINGERPRINT "firefox"
    env_line VLESS_REALITY_SPIDER_X "/"
    env_line HYSTERIA_PORT "8443"
    env_line AMNEZIA_PORT "51820"
    env_line AMNEZIA_NETWORK_PREFIX "10.66.66"
    env_line AMNEZIA_DNS "1.1.1.1, 8.8.8.8"
    env_line SSH_PORT "22"
    echo
    env_line IP_APPEAR_TIMEOUT_SECONDS "300"
    env_line IP_APPEAR_INTERVAL_SECONDS "5"
    env_line REBOOT_WAIT_SECONDS "30"
    env_line HEALTHCHECK_TIMEOUT_SECONDS "300"
    env_line HEALTHCHECK_INTERVAL_SECONDS "10"
  } >"$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"

  export AUTOVPN_INITIAL_CURRENT_IP="$current_ip"
}

write_run_script() {
  cat >"$APP_DIR/run-local.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$APP_DIR"
while IFS='=' read -r key value; do
  if [ -z "\$key" ] || [[ "\$key" == \#* ]]; then
    continue
  fi
  if [[ ! "\$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    continue
  fi
  export "\$key=\$value"
done < "$APP_DIR/.env"
PYTHON_BIN="$APP_DIR/.venv/bin/python"
RUN_HOST="\${APP_HOST:-127.0.0.1}"
RUN_PORT="\$("\$PYTHON_BIN" - <<'PY'
import os
import socket

host = os.getenv("APP_HOST", "127.0.0.1")
port = int(os.getenv("APP_PORT", "8000"))
bind_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
while True:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((bind_host, port))
        except OSError:
            port += 1
            continue
    print(port)
    break
PY
)"
if [ "\$RUN_PORT" != "\${APP_PORT:-8000}" ]; then
  echo "Port \${APP_PORT:-8000} is busy, using \$RUN_PORT."
fi
echo "Open: http://\$RUN_HOST:\$RUN_PORT/admin/setup"
exec "\$PYTHON_BIN" -m uvicorn app.main:app --host "\$RUN_HOST" --port "\$RUN_PORT"
EOF
  chmod +x "$APP_DIR/run-local.sh"
}

init_local_db() {
  while IFS='=' read -r key value; do
    if [ -z "$key" ] || [[ "$key" == \#* ]]; then
      continue
    fi
    if [[ ! "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
      continue
    fi
    export "$key=$value"
  done < "$APP_DIR/.env"
  "$APP_DIR/.venv/bin/python" - <<'PY'
import os
from app.db import init_db, set_setting

init_db()
current_ip = os.getenv("AUTOVPN_INITIAL_CURRENT_IP", "")
if current_ip:
    set_setting("current_ip", current_ip)
PY
}

main() {
  parse_args "$@"
  echo "[autovpn] local installer"
  require_command git "Install git first."
  require_command python3 "Install Python 3.12+ first."
  prepare_source
  cd "$APP_DIR"
  write_env_file

  echo "[autovpn] creating virtualenv"
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
  "$APP_DIR/.venv/bin/python" -m pip install -e "$APP_DIR"
  init_local_db
  write_run_script

  echo
  echo "AutoVPN local install is ready."
  echo "App dir: $APP_DIR"
  echo "Config: $APP_DIR/.env"
  echo "Run: $APP_DIR/run-local.sh"
  echo "Open: http://$APP_HOST:$APP_PORT/admin"
}

main "$@"
