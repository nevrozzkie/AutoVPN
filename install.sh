#!/usr/bin/env bash
set -euo pipefail

APP_NAME="autovpn"
APP_USER="${APP_USER:-autovpn}"
APP_DIR="${APP_DIR:-/opt/autovpn}"
DATA_DIR="${DATA_DIR:-/var/lib/autovpn}"
DATABASE_PATH="$DATA_DIR/autovpn.sqlite3"
BACKUP_DIR="$DATA_DIR/backups"
LEGACY_DATABASE="$APP_DIR/data/autovpn.sqlite3"
ENV_FILE="${ENV_FILE:-/etc/autovpn.env}"
SERVICE_FILE="/etc/systemd/system/autovpn.service"
NGINX_SITE="/etc/nginx/sites-available/autovpn"
NGINX_LINK="/etc/nginx/sites-enabled/autovpn"
DEFAULT_REPO_URL="https://github.com/nevrozzkie/AutoVPN.git"
REPO_URL="${AUTOVPN_REPO_URL:-$DEFAULT_REPO_URL}"
APP_HOST="${APP_HOST:-127.0.0.1}"
APP_PORT="${APP_PORT:-8000}"
SOURCE_DIR=""
STAGING_DIR=""

WEBROOT_DIR="${WEBROOT_DIR:-/var/www/autovpn}"
IP_CERT_RENEW_SCRIPT="/usr/local/sbin/autovpn-renew-ip-cert"
IP_CERT_RENEW_SERVICE="/etc/systemd/system/autovpn-renew-ip-cert.service"
IP_CERT_RENEW_TIMER="/etc/systemd/system/autovpn-renew-ip-cert.timer"

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
  --panel-domain VALUE              Public domain for HTTPS panel.
  --tls-mode VALUE                  TLS mode: domain, ip, none. Default: domain.
  --public-ip VALUE                 Public IP for Let's Encrypt IP certificate.
  -h, --help                        Show this help.

Values can also be provided through matching environment variables, for example:
EU_SSH_HOST, EU_SSH_USER, EU_SSH_PASSWORD, EU_SSH_KEY_PATH, CURRENT_IP,
AUTOVPN_TLS_MODE, AUTOVPN_PUBLIC_IP, AUTOVPN_DOMAIN.
EOF
}

require_arg() {
  local option="$1"
  if [ "$#" -lt 2 ]; then
    echo "Missing value for $option" >&2
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
      --panel-domain)
        require_arg "$@"
        AUTOVPN_DOMAIN="$2"
        shift 2
        ;;
      --tls-mode)
        require_arg "$@"
        AUTOVPN_TLS_MODE="$2"
        shift 2
        ;;
      --public-ip)
        require_arg "$@"
        AUTOVPN_PUBLIC_IP="$2"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown option: $1" >&2
        usage
        exit 1
        ;;
    esac
  done
}

parse_args "$@"

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root: sudo bash install.sh" >&2
  exit 1
fi

read_value() {
  local _prompt="$1" _default="${2:-}" _v=""
  if [ -e /dev/tty ]; then
    if [ -n "$_default" ]; then
      printf '%s [%s]: ' "$_prompt" "$_default" >/dev/tty
    else
      printf '%s: ' "$_prompt" >/dev/tty
    fi
    IFS= read -r _v </dev/tty || _v=""
  fi
  printf '%s' "${_v:-$_default}"
}

read_secret() {
  local _prompt="$1" _v=""
  if [ -e /dev/tty ]; then
    printf '%s: ' "$_prompt" >/dev/tty
    IFS= read -r -s _v </dev/tty || _v=""
    printf '\n' >/dev/tty
  fi
  printf '%s' "$_v"
}

sanitize_env_value() {
  printf "%s" "$1" | LC_ALL=C tr -d '[:cntrl:]'
}

env_line() {
  local key="$1"
  local value="${2:-}"
  printf "%s=%s\n" "$key" "$(sanitize_env_value "$value")"
}

cleanup_existing_install() {
  echo "[autovpn] cleaning previous install"

  systemctl stop "$APP_NAME" 2>/dev/null || true
  systemctl disable "$APP_NAME" 2>/dev/null || true

  systemctl stop autovpn-renew-ip-cert.timer 2>/dev/null || true
  systemctl disable autovpn-renew-ip-cert.timer 2>/dev/null || true

  rm -f "$SERVICE_FILE"
  rm -f "$IP_CERT_RENEW_SERVICE"
  rm -f "$IP_CERT_RENEW_TIMER"
  rm -f "$IP_CERT_RENEW_SCRIPT"

  rm -f "$NGINX_LINK"
  rm -f "$NGINX_SITE"
  rm -f /etc/nginx/sites-enabled/default
  rm -f /etc/nginx/conf.d/default.conf

  systemctl daemon-reload
  systemctl reload nginx 2>/dev/null || true

  git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
}

collect_admin_credentials() {
  if [ -z "${ADMIN_USERNAME:-}" ]; then
    ADMIN_USERNAME="$(read_value "Admin username" "admin")"
  fi
  ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"

  if [ -z "${ADMIN_PASSWORD:-}" ]; then
    if [ ! -e /dev/tty ]; then
      echo "ERROR: admin password is required. Pass --admin-password or set ADMIN_PASSWORD." >&2
      exit 1
    fi

    local _p1 _p2
    while true; do
      _p1="$(read_secret "Admin password")"
      _p2="$(read_secret "Confirm admin password")"
      if [ -z "$_p1" ]; then
        echo "Password cannot be empty." >&2
        continue
      fi
      if [ "$_p1" != "$_p2" ]; then
        echo "Passwords do not match, please try again." >&2
        continue
      fi
      ADMIN_PASSWORD="$_p1"
      break
    done
  fi

  export ADMIN_USERNAME ADMIN_PASSWORD
}

install_packages() {
  echo "[autovpn] installing system packages"
  apt-get update
  apt-get install -y git nginx python3 python3-venv python3-pip rsync sqlite3 curl
}

cleanup_staging() {
  if [ -n "$STAGING_DIR" ] && [ -d "$STAGING_DIR" ]; then
    rm -rf -- "$STAGING_DIR"
  fi
}

prepare_source() {
  if [ -f "pyproject.toml" ] && [ -d "app" ]; then
    SOURCE_DIR="$(pwd)"
  else
    echo "[autovpn] preparing source from $REPO_URL"
    STAGING_DIR="$(mktemp -d)"
    SOURCE_DIR="$STAGING_DIR/source"
    git clone "$REPO_URL" "$SOURCE_DIR"
  fi

  if [ ! -f "$SOURCE_DIR/pyproject.toml" ] || [ ! -d "$SOURCE_DIR/app" ] || \
      [ ! -f "$SOURCE_DIR/tools/prepare_data.py" ]; then
    echo "[autovpn] prepared source failed preflight" >&2
    exit 1
  fi
}

prepare_persistent_data() {
  echo "[autovpn] backing up and preparing persistent data"
  install -d -m 0700 "$DATA_DIR" "$BACKUP_DIR"
  python3 "$SOURCE_DIR/tools/prepare_data.py" \
    --database "$DATABASE_PATH" \
    --backup-dir "$BACKUP_DIR" \
    --legacy-database "$LEGACY_DATABASE"
}

deploy_source() {
  if [ "$SOURCE_DIR" = "$APP_DIR" ]; then
    return
  fi
  echo "[autovpn] updating application source in $APP_DIR"
  mkdir -p "$APP_DIR"
  rsync -a --delete \
    --exclude ".git" \
    --exclude ".venv" \
    --exclude "__pycache__" \
    --exclude "data" \
    --exclude ".env" \
    "$SOURCE_DIR/" "$APP_DIR/"
}

preserve_existing_env() {
  local env_backup env_tmp line saw_database
  env_backup="$BACKUP_DIR/autovpn.env.pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ)"
  install -m 0600 "$ENV_FILE" "$env_backup"
  env_tmp="$(mktemp "$(dirname "$ENV_FILE")/.autovpn.env.XXXXXX")"
  saw_database=0
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      DATABASE_PATH=*)
        env_line DATABASE_PATH "$DATABASE_PATH"
        saw_database=1
        ;;
      *) printf '%s\n' "$line" ;;
    esac
  done <"$ENV_FILE" >"$env_tmp"
  if [ "$saw_database" -eq 0 ]; then
    env_line DATABASE_PATH "$DATABASE_PATH" >>"$env_tmp"
  fi
  install -m 0600 "$env_tmp" "$ENV_FILE"
  rm -f "$env_tmp"
  echo "[autovpn] preserved existing environment; backup: $env_backup"
}

write_env_file() {
  echo
  echo "[autovpn] writing configuration. Open Setup in the browser to finish configuration."

  if [ -f "$ENV_FILE" ]; then
    preserve_existing_env
    return
  fi

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
    env_line DATABASE_PATH "$DATABASE_PATH"
    env_line ADMIN_USERNAME "$admin_username"
    echo
    env_line AEZA_API_BASE "https://my.aeza.net"
    env_line AEZA_TOKEN "$aeza_token"
    env_line AEZA_SERVICE_ID "$aeza_service_id"
    env_line AEZA_IPV4_PAYMENT_METHOD "balance"
    env_line AEZA_IPV4_DOMAIN "$aeza_domain"
    env_line AEZA_IPV4_AFTER_PURCHASE_DELAY_SECONDS "120"
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
    local current_ip_sql
    current_ip_sql="${current_ip//\'/\'\'}"
    sqlite3 "$DATABASE_PATH" \
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

  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --upgrade pip
  "$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"
  chown -R "$APP_USER:$APP_USER" "$APP_DIR"
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
  chmod 0700 "$DATA_DIR" "$BACKUP_DIR"
  if [ -f "$DATABASE_PATH" ]; then
    chmod 0600 "$DATABASE_PATH"
  fi
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

bootstrap_admin() {
  echo "[autovpn] storing admin credentials (hashed) in the database"
  AUTOVPN_BOOT_USER="$ADMIN_USERNAME" \
  AUTOVPN_BOOT_PASS="$ADMIN_PASSWORD" \
  AUTOVPN_BOOT_DB="$DATABASE_PATH" \
  "$APP_DIR/.venv/bin/python" - <<'PY'
import os, sqlite3
from app.security import hash_password

db = os.environ["AUTOVPN_BOOT_DB"]
os.makedirs(os.path.dirname(db), exist_ok=True)
conn = sqlite3.connect(db)
conn.execute(
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
)
for key, value in (
    ("config.admin_username", os.environ.get("AUTOVPN_BOOT_USER") or "admin"),
    ("config.admin_password", hash_password(os.environ["AUTOVPN_BOOT_PASS"])),
):
    existing = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not existing or not existing[0]:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
conn.commit()
conn.close()
PY
  chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
  chmod 0700 "$DATA_DIR" "$BACKUP_DIR"
  chmod 0600 "$DATABASE_PATH"
}

install_certbot_for_domain() {
  if command -v certbot >/dev/null 2>&1; then
    return
  fi
  apt-get install -y certbot python3-certbot-nginx || true
}

install_certbot_for_ip() {
  if command -v certbot >/dev/null 2>&1; then
    return
  fi

  apt-get install -y snapd || true
  if command -v snap >/dev/null 2>&1; then
    snap install core || true
    snap refresh core || true
    snap install --classic certbot || true
    ln -sf /snap/bin/certbot /usr/bin/certbot || true
  fi

  if ! command -v certbot >/dev/null 2>&1; then
    apt-get install -y certbot python3-certbot-nginx || true
  fi
}

detect_public_ip() {
  curl -fsS4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}'
}

write_nginx_plain_proxy_block() {
  local server_name="$1"
  cat >"$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    server_name $server_name;

    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t
  systemctl reload nginx
}

write_nginx_ip_acme_bootstrap_block() {
  mkdir -p "$WEBROOT_DIR/.well-known/acme-challenge"
  cat >"$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    server_name _;

    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location /.well-known/acme-challenge/ {
        root $WEBROOT_DIR;
        try_files \$uri =404;
    }

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t
  systemctl reload nginx
}

write_nginx_https_ip_block() {
  local public_ip="$1"
  cat >"$NGINX_SITE" <<EOF
server {
    listen 80 default_server;
    server_name _;

    location /.well-known/acme-challenge/ {
        root $WEBROOT_DIR;
        try_files \$uri =404;
    }

    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    server_name _;

    ssl_certificate /etc/letsencrypt/live/$public_ip/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$public_ip/privkey.pem;

    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer" always;

    location / {
        proxy_pass http://127.0.0.1:$APP_PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Host \$host;
        proxy_set_header X-Forwarded-Port \$server_port;
        proxy_set_header X-Forwarded-Proto https;
    }
}
EOF
  ln -sf "$NGINX_SITE" "$NGINX_LINK"
  nginx -t
  systemctl reload nginx
}

setup_ip_cert_renewal() {
  local public_ip="$1"

  cat >"$IP_CERT_RENEW_SCRIPT" <<EOF
#!/usr/bin/env bash
set -euo pipefail

certbot certonly \\
  --preferred-profile shortlived \\
  --webroot \\
  --webroot-path "$WEBROOT_DIR" \\
  --ip-address "$public_ip" \\
  --non-interactive \\
  --agree-tos \\
  --register-unsafely-without-email \\
  --keep-until-expiring

systemctl reload nginx
EOF
  chmod 700 "$IP_CERT_RENEW_SCRIPT"

  cat >"$IP_CERT_RENEW_SERVICE" <<EOF
[Unit]
Description=Renew AutoVPN Let's Encrypt IP certificate
After=network-online.target nginx.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$IP_CERT_RENEW_SCRIPT
EOF

  cat >"$IP_CERT_RENEW_TIMER" <<EOF
[Unit]
Description=Renew AutoVPN Let's Encrypt IP certificate every 12 hours

[Timer]
OnBootSec=10min
OnUnitActiveSec=12h
Persistent=true

[Install]
WantedBy=timers.target
EOF

  systemctl daemon-reload
  systemctl enable --now autovpn-renew-ip-cert.timer
}

configure_nginx() {
  if [ "${CONFIGURE_NGINX:-1}" != "1" ]; then
    echo "[autovpn] WARNING: nginx not configured. The panel is only reachable on"
    echo "[autovpn] 127.0.0.1:$APP_PORT. Put a TLS reverse proxy or SSH tunnel in front of it."
    NGINX_TLS="skipped"
    return
  fi

  local tls_mode="${AUTOVPN_TLS_MODE:-}"
  if [ -z "$tls_mode" ]; then
    tls_mode="$(read_value "TLS mode: domain, ip, none" "domain")"
  fi

  case "$tls_mode" in
    ip)
      local public_ip="${AUTOVPN_PUBLIC_IP:-}"
      if [ -z "$public_ip" ]; then
        public_ip="$(detect_public_ip)"
      fi

      echo "[autovpn] configuring nginx ACME webroot for IP certificate: $public_ip"
      write_nginx_ip_acme_bootstrap_block
      install_certbot_for_ip

      if certbot certonly \
          --preferred-profile shortlived \
          --webroot \
          --webroot-path "$WEBROOT_DIR" \
          --ip-address "$public_ip" \
          --non-interactive \
          --agree-tos \
          --register-unsafely-without-email; then
        write_nginx_https_ip_block "$public_ip"
        setup_ip_cert_renewal "$public_ip"
        NGINX_TLS="yes"
        AUTOVPN_PANEL_URL="https://$public_ip/admin/setup"
      else
        NGINX_TLS="failed"
        echo "[autovpn] IP certificate failed. Check:"
        echo "[autovpn] - port 80 is open from the internet"
        echo "[autovpn] - curl http://$public_ip/.well-known/acme-challenge/test returns a file from $WEBROOT_DIR"
        echo "[autovpn] - certbot version supports --ip-address and --preferred-profile shortlived"
      fi
      return
      ;;
    none)
      write_nginx_plain_proxy_block "_"
      NGINX_TLS="no"
      return
      ;;
    domain|"")
      ;;
    *)
      echo "[autovpn] unknown TLS mode: $tls_mode" >&2
      exit 1
      ;;
  esac

  local domain="${AUTOVPN_DOMAIN:-}"
  if [ -z "$domain" ]; then
    domain="$(read_value "Public domain for the panel (recommended — enables HTTPS; leave empty to skip TLS)" "")"
  fi

  if [ -z "$domain" ] || [ "$domain" = "_" ]; then
    write_nginx_plain_proxy_block "_"
    NGINX_TLS="no"
    echo "[autovpn] WARNING: no domain provided, the panel is served over PLAIN HTTP."
    echo "[autovpn] Admin password, SSH and Aeza secrets would travel UNENCRYPTED over the network."
    return
  fi

  write_nginx_plain_proxy_block "$domain"
  install_certbot_for_domain

  if command -v certbot >/dev/null 2>&1; then
    if certbot --nginx -d "$domain" --non-interactive --agree-tos \
        --register-unsafely-without-email --redirect; then
      NGINX_TLS="yes"
      AUTOVPN_PANEL_URL="https://$domain/admin/setup"
    else
      NGINX_TLS="failed"
      echo "[autovpn] certbot failed. Finish TLS manually: certbot --nginx -d $domain --redirect"
    fi
  else
    NGINX_TLS="failed"
    echo "[autovpn] certbot is not available. Install it and run: certbot --nginx -d $domain --redirect"
  fi
}

main() {
  trap cleanup_staging EXIT
  install_packages
  collect_admin_credentials
  prepare_source
  prepare_persistent_data
  cleanup_existing_install
  deploy_source
  write_env_file
  install_python_app
  bootstrap_admin
  write_systemd
  configure_nginx

  echo
  echo "AutoVPN installed."
  echo "Service status: systemctl status autovpn"
  echo "Config file: $ENV_FILE"
  echo "Data directory: $DATA_DIR"
  echo "Admin login was set during install (stored hashed in the database)."
  case "${NGINX_TLS:-}" in
    yes)
      echo "Open: ${AUTOVPN_PANEL_URL:-https://SERVER/admin/setup}"
      ;;
    no|failed|skipped)
      echo "Open: http://SERVER/admin/setup  (NOT encrypted — set up TLS before real use)"
      ;;
    *)
      echo "Open: http://SERVER/admin/setup"
      ;;
  esac
}

main "$@"
