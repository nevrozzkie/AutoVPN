#!/usr/bin/env bash
set -euo pipefail

APP_NAME="autovpn"
APP_USER="${APP_USER:-autovpn}"
APP_DIR="${APP_DIR:-/opt/autovpn}"
ENV_FILE="${ENV_FILE:-/etc/autovpn.env}"
SERVICE_FILE="/etc/systemd/system/autovpn.service"
NGINX_SITE="/etc/nginx/sites-available/autovpn"
NGINX_LINK="/etc/nginx/sites-enabled/autovpn"
DEFAULT_REPO_URL="https://github.com/nevrozzkie/AutoVPN.git"
REPO_URL="${AUTOVPN_REPO_URL:-$DEFAULT_REPO_URL}"
APP_HOST="${APP_HOST:-0.0.0.0}"
APP_PORT="${APP_PORT:-8000}"

usage() {
  cat <<EOF
Usage: bash install.sh [options]

Options:
  --eu-host, --eu-ip VALUE          Target VPN VPS SSH host/IP.
  --eu-user, --eu-login VALUE       Target VPN VPS SSH user, usually root.
  --eu-port VALUE                   Target VPN VPS SSH port, default 22.
  --eu-password, --ssh-password VALUE
                                   SSH password for the target VPN VPS.
  --eu-key-path, --ssh-key VALUE    SSH private key path on this RU server.
  --current-ip VALUE                Initial VPN IP stored in AutoVPN.
  --admin-username VALUE            Admin username.
  --admin-password VALUE            Admin password.
  --aeza-token VALUE                Optional Aeza API token.
  --aeza-service-id VALUE           Optional Aeza service id.
  --aeza-domain VALUE               Optional Aeza IPv4 domain/service name.
  -h, --help                        Show this help.

Values can also be provided through matching environment variables, for example:
EU_SSH_HOST, EU_SSH_USER, EU_SSH_PASSWORD, EU_SSH_KEY_PATH, CURRENT_IP.
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

parse_args "$@"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash install.sh"
  exit 1
fi

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

install_packages() {
  echo "[autovpn] installing system packages"
  apt-get update
  apt-get install -y git nginx python3 python3-venv python3-pip rsync sqlite3
}

prepare_source() {
  if [ -f "pyproject.toml" ] && [ -d "app" ]; then
    local current_dir
    current_dir="$(pwd)"
    if [ "$current_dir" != "$APP_DIR" ]; then
      echo "[autovpn] copying current checkout to $APP_DIR"
      mkdir -p "$APP_DIR"
      rsync -a --delete \
        --exclude ".git" \
        --exclude ".venv" \
        --exclude "__pycache__" \
        "$current_dir/" "$APP_DIR/"
    fi
    return
  fi

  echo "[autovpn] cloning $REPO_URL to $APP_DIR"
  if [ -d "$APP_DIR/.git" ]; then
    git -C "$APP_DIR" pull --ff-only
  else
    rm -rf "$APP_DIR"
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

  install -d -m 0755 "$(dirname "$ENV_FILE")"
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
  } >"$ENV_FILE"
  chmod 600 "$ENV_FILE"

  if [ -n "$current_ip" ]; then
    mkdir -p "$APP_DIR/data"
    local current_ip_sql
    current_ip_sql="${current_ip//\'/\'\'}"
    sqlite3 "$APP_DIR/data/autovpn.sqlite3" \
      "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
       INSERT INTO settings(key, value) VALUES ('current_ip', '$current_ip_sql')
       ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
  fi
}

install_python_app() {
  echo "[autovpn] creating user and virtualenv"
  if ! getent group "$APP_USER" >/dev/null 2>&1; then
    groupadd --system "$APP_USER"
  fi
  if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --gid "$APP_USER" --home "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
  fi
  mkdir -p "$APP_DIR/data"
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
}

write_systemd() {
  echo "[autovpn] writing systemd unit"
  cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=AutoVPN RU control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/.venv/bin/uvicorn app.main:app --host $APP_HOST --port $APP_PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$APP_NAME"
}

configure_nginx() {
  if [ "${CONFIGURE_NGINX:-0}" != "1" ]; then
    return
  fi

  local domain
  domain="${AUTOVPN_DOMAIN:-_}"
  cat >"$NGINX_SITE" <<EOF
server {
    listen 80;
    server_name $domain;

    location / {
        proxy_pass http://$APP_HOST:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t
  systemctl reload nginx
}

main() {
  install_packages
  prepare_source
  write_env_file
  install_python_app
  write_systemd
  configure_nginx

  echo
  echo "AutoVPN installed."
  echo "Service status: systemctl status autovpn"
  echo "Config file: $ENV_FILE"
  echo "Open: http://SERVER:$APP_PORT/admin/setup"
}

main "$@"
