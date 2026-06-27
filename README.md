# AutoVPN

Полностью нейрокод: проект целиком написан и собран AI/Codex.

FastAPI MVP for a RU source-of-truth server that manages VPN subscriptions and manual Aeza IPv4 rotation for an EU VPS.

## Architecture

- RU server runs this FastAPI service, SQLite, admin panel, and nginx.
- RU server is the source of truth for clients, tokens, protocol credentials, current EU IP, and operation history.
- EU/Aeza server hosts the actual VPN protocols: VLESS, Hysteria, and AmneziaWG for AmneziaVPN.
- Clients connect their VPN apps to the EU/Aeza IP, but receive that IP dynamically from the RU server through `/sub/{token}` or `/ip/{token}`.
- The RU server talks to Aeza API only to rotate the EU server IPv4 address.
- IP rotation is an optional Aeza-only capability; generic VPS install/sync does not require Aeza.

## What is included

- Basic Auth protected admin panel:
  - `/admin`
  - `/admin/clients`
  - `/admin/install`
  - `/admin/operations`
- Public client connection page:
  - `/client/{token}`
- Public dynamic subscription endpoint:
  - `/sub/{token}`
- Public current EU IP endpoint:
  - `/ip/{token}`
- AmneziaWG config endpoint:
  - `/amnezia/{token}`
- SQLite storage for settings, clients, and IP change operations.
- Aeza API client for IPv4 list, add, make-main, delete, and service reboot.
- Step-by-step IP change state machine with operation logging.
- EU install/sync over SSH from the RU control plane.
- TCP healthcheck for SSH, VLESS, and Hysteria ports.
- VLESS client traffic stats through Xray StatsService.
- AmneziaWG client traffic stats through `awg show awg0 dump`.

## Local run

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
cp .env.example .env
```

Edit `.env`, especially:

```bash
ADMIN_PASSWORD=...
AEZA_TOKEN=...
AEZA_SERVICE_ID=...
AEZA_IPV4_PAYMENT_METHOD=balance
AEZA_IPV4_DOMAIN=
DATABASE_PATH=./data/autovpn.sqlite3
EU_SSH_HOST=...
EU_SSH_USER=root
EU_SSH_KEY_PATH=/home/autovpn/.ssh/aeza_ed25519
```

Then run:

```bash
set -a
. .env
set +a
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/admin`.

## Server install

On the RU control-plane server, the intended install flow is:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

If the repository lives under another GitHub URL, override it:

```bash
AUTOVPN_REPO_URL=https://github.com/USER/AutoVPN.git \
  bash <(curl -Ls https://raw.githubusercontent.com/USER/AutoVPN/main/install.sh)
```

The installer asks for:

- admin username/password;
- current VPN server IP;
- optional Aeza token/service id for IP rotation;
- target VPN VPS SSH host/user/port/key;
- or target VPN VPS root/user password;
- nginx domain or public IP.

It installs system packages, clones/copies the app to `/opt/autovpn`, creates `/etc/autovpn.env`, installs Python dependencies into `/opt/autovpn/.venv`, writes a systemd unit, starts `autovpn`, and can configure nginx.

## Local install

AutoVPN can also run locally on a personal computer. In this mode your computer is the control plane: it stores SQLite, serves `/admin` and `/sub/{token}` on localhost, and connects to the EU/VPN VPS over SSH. The EU/VPN VPS still hosts VLESS, Hysteria2, and AmneziaWG.

This mode is meant for personal/self-hosted usage: open the local admin page, install/sync VPN on your VPS, open your own `/client/{token}` or `/sub/{token}` locally, import/update the config in your VPN client, done.

Local installers do not install systemd or nginx. They create a virtualenv, write `.env`, initialize SQLite, and generate a run script.

Requirements:

- Python 3.12+;
- Git;
- network access from the computer to the EU/VPN VPS SSH port;
- Windows PowerShell for Windows, or bash for macOS/Linux.

macOS/Linux from a cloned repository:

```bash
./install-local.sh \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-password 'your-root-password'
```

macOS/Linux without cloning first:

```bash
curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install-local.sh -o install-local.sh
chmod +x install-local.sh
./install-local.sh \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-password 'your-root-password'
```

Start the local admin app on macOS/Linux:

```bash
~/AutoVPN/run-local.sh
```

Windows from a cloned repository:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -EuHost 203.0.113.10 `
  -EuUser root `
  -EuPassword "your-root-password"
```

Windows without cloning first:

```powershell
iwr https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -EuHost 203.0.113.10 `
  -EuUser root `
  -EuPassword "your-root-password"
```

Windows with SSH key auth:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -EuHost 203.0.113.10 `
  -EuUser root `
  -EuKeyPath "$env:USERPROFILE\.ssh\id_ed25519"
```

If `install.ps1` is run outside a checkout, it clones `AUTOVPN_REPO_URL` or `https://github.com/nevrozzkie/AutoVPN.git` into `%USERPROFILE%\AutoVPN`.

Start the local admin app on Windows:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\AutoVPN\run-local.ps1"
```

Then open:

```text
http://127.0.0.1:8000/admin
```

Most installer answers can be passed through environment variables. For example, password auth to the EU/VPN VPS:

```bash
EU_SSH_HOST=203.0.113.10 \
EU_SSH_USER=root \
EU_SSH_PASSWORD='your-root-password' \
  bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

`CURRENT_IP` is optional in this case: if it is not set, the installer uses `EU_SSH_HOST` as the initial VPN IP.

The same can be passed as install arguments:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh) \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-password 'your-root-password'
```

With SSH key auth:

```bash
EU_SSH_HOST=203.0.113.10 \
EU_SSH_USER=root \
EU_SSH_KEY_PATH=/root/.ssh/id_ed25519 \
  bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

or:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh) \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-key-path /root/.ssh/id_ed25519
```

Useful non-interactive variables:

```bash
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...
CURRENT_IP=203.0.113.10
AEZA_TOKEN=...
AEZA_SERVICE_ID=...
AEZA_IPV4_DOMAIN=...
```

For VPN VPS auto-setup over SSH, either password auth or key auth can be used:

```bash
EU_SSH_HOST=203.0.113.10
EU_SSH_USER=root
EU_SSH_PORT=22
EU_SSH_PASSWORD=...
```

or:

```bash
EU_SSH_HOST=203.0.113.10
EU_SSH_USER=root
EU_SSH_PORT=22
EU_SSH_KEY_PATH=/home/autovpn/.ssh/id_ed25519
```

If `EU_SSH_PASSWORD` is set, it is used instead of `EU_SSH_KEY_PATH`.

## First setup

1. Set `current_ip` to the current EU/Aeza VPN IP in SQLite before the first rotation if Aeza does not mark the main IP clearly.
2. Create family clients on `/admin`.
3. Share `/client/{token}` with a client. It contains the subscription link, QR, AmneziaWG config/QR, and guide placeholders.
4. Use `/sub/{token}` directly in VPN apps that support subscription URLs.
5. Press `Установить VPN` or `Обновить клиентов на VPN` on `/admin` to install/sync enabled clients to the target VPS.
6. Press `Обновить IP` on `/admin` only for Aeza servers with `AEZA_TOKEN` and `AEZA_SERVICE_ID` configured.

## Admin layout

`/admin` is the main control page:

- protocol status for VLESS, Hysteria, and AmneziaWG;
- last check time and failed-since time for protocols;
- expandable clients;
- VLESS upload/download and last-seen per client;
- add, rename, enable/disable, and delete clients;
- client page and subscription URLs;
- buttons for syncing clients, installing VPN, and Aeza-only IP rotation.

`/client/{token}` is the public client page:

- combined VLESS + Hysteria subscription URL;
- QR button for subscription URL;
- AmneziaWG config download and QR;
- guide placeholders for iOS, Android, Windows, and macOS.

## Installing VPN on EU/Aeza

Open `/admin/install`.

The RU server can:

- generate an EU install/sync script at `/admin/install/script`;
- run that script on the EU VPS over SSH with `Install / Sync EU VPN`;
- install Xray, Hysteria2, and AmneziaWG systemd services;
- write Xray VLESS config from enabled RU clients;
- write Hysteria2 user/password config from enabled RU clients;
- write AmneziaWG `awg0` config from enabled RU clients;
- restart `xray`, `hysteria-server`, and `awg-quick@awg0`;
- store install output and errors in `vpn_install_operations`.
- enable Xray StatsService on `127.0.0.1:10085` for VLESS per-client traffic.

For SSH, configure:

```bash
EU_SSH_HOST=203.0.113.10
EU_SSH_USER=root
EU_SSH_PORT=22
EU_SSH_KEY_PATH=/home/autovpn/.ssh/aeza_ed25519
```

If `EU_SSH_HOST` is empty, the service uses `settings.current_ip` as the target host.

The generated MVP protocol format is:

- VLESS REALITY + Vision on `VLESS_PORT`, default `443/tcp`;
- VLESS REALITY server accepts `VLESS_REALITY_SERVER_NAMES`, default `ok.ru,www.ok.ru`;
- VLESS client links use `VLESS_REALITY_SERVER_NAME` as SNI, default `ok.ru`;
- Hysteria2 on `HYSTERIA_PORT` with self-signed TLS, so subscription links include `insecure=1`;
- Hysteria2 username is `client{id}`, password is `clients.hysteria_password`.
- AmneziaWG on `AMNEZIA_PORT`, default `51820/udp`;
- AmneziaWG client config is available at `/amnezia/{token}`;
- AmneziaWG QR is available on `/client/{token}`;
- AmneziaWG server config is written to `/etc/amnezia/amneziawg/awg0.conf`.

The current AmneziaWG installer uses the official Amnezia PPA flow, so this MVP assumes an Ubuntu-compatible VPS for Amnezia auto-setup.

VLESS REALITY settings:

```bash
VLESS_PORT=443
VLESS_REALITY_TARGET=ok.ru:443
VLESS_REALITY_SERVER_NAMES=ok.ru,www.ok.ru
VLESS_REALITY_SERVER_NAME=ok.ru
VLESS_REALITY_FINGERPRINT=chrome
VLESS_REALITY_SPIDER_X=/
```

REALITY private/public keys and shortId are generated once and stored in SQLite settings.

## Client statistics

`/admin` has an `Обновить статистику` button.

The current MVP collects client traffic from two sources:

- auto-setup enables Xray `StatsService`;
- each VLESS client gets an Xray `email` based on name and id;
- the RU server connects to the VPN VPS over SSH;
- it runs `/usr/local/bin/xray api statsquery --server=127.0.0.1:10085 -pattern 'user>>>'`;
- it also runs `awg show awg0 dump` for AmneziaWG peer counters;
- parsed upload/download counters are stored in SQLite `client_stats`;
- `last_seen_at` updates when counters grow.

Hysteria2 per-client traffic is not collected yet. It is a separate service in this MVP, not part of Xray, so Xray StatsService does not see Hysteria2 traffic.

Manual SQLite example:

```bash
sqlite3 ./data/autovpn.sqlite3 \
  "INSERT INTO settings(key, value) VALUES ('current_ip', '1.2.3.4')
   ON CONFLICT(key) DO UPDATE SET value = excluded.value;"
```

## IP rotation safety

IP rotation is disabled unless both `AEZA_TOKEN` and `AEZA_SERVICE_ID` are configured.
This action adds a new Aeza IPv4 before deleting the old one, so Aeza may charge extra money for the additional IPv4 while the operation is running or if it fails before cleanup.
The dashboard may show the current Aeza price, but the actual rotate button opens `/admin/ip/confirm`.
That confirmation page asks Aeza for the IPv4 price again and shows it before the operation can be started.
There is also a manual IP manager at `/admin/ip`.
It can:

- refresh the Aeza IPv4 list;
- show the current new IPv4 price;
- buy a new IPv4 after price confirmation;
- make a selected IPv4 the main one;
- delete only non-main IPv4 addresses.

When an IP is made main manually, AutoVPN updates `settings.current_ip` and synchronizes generated VPN configs to that selected IP.
Aeza account currency is EUR, and the API returns prices in minor units: `2` means `€0.02`.
The dashboard divides the API value by 100 and displays it with `€`.
In the captured HAR, Aeza returned:

```json
{
  "termLimits": {
    "default": 16,
    "max": 16,
    "hour": 3,
    "half_day": 36,
    "day": 72,
    "week": 504,
    "month": 16,
    "quarter_year": 6480,
    "half_year": 12960,
    "year": 26280,
    "eternal": 3000000000
  },
  "price": 2,
  "protectedPrice": 9
}
```

This means `price` is displayed as `€0.02`, and `protectedPrice` would be `€0.09`.

The HAR also showed IPv4 creation as:

```json
{"method":"balance","domain":"aqua"}
```

`AEZA_IPV4_PAYMENT_METHOD` controls `method`; `AEZA_IPV4_DOMAIN` is optional and should be set if Aeza requires the service name/domain for your account.

The state machine:

1. Refuses a new run while a `PENDING` or `RUNNING` operation exists.
2. Reads Aeza IPv4 list.
3. Finds and stores old main IP.
4. Adds a new IPv4.
5. Waits `AEZA_IPV4_AFTER_PURCHASE_DELAY_SECONDS`, default 300 seconds, because Aeza may not show the new IP immediately after purchase.
6. Waits until the new IPv4 appears in Aeza list.
7. Makes the new IPv4 main.
8. Reboots the VPS.
9. Marks the step as `wait_vps_health` while the RU server waits for the EU VPS to come back.
10. From the RU server, waits for TCP healthchecks on the EU IP: SSH, VLESS, and Hysteria.
11. When all checks pass, stores `server_reachable_at` and `healthcheck_result`.
12. Updates `settings.current_ip`.
13. Runs `sync_vpn_after_ip_change`: reconnects to the VPN VPS by the new IP and reapplies generated VPN configs.
14. Deletes the old IPv4.
15. Marks operation as `DONE`.

If VPN config sync fails after the new IP is active, the old IPv4 is not deleted automatically.

The dashboard shows `Ждём, когда сервер поднимется после reboot` during `wait_vps_health`.
After successful checks it shows `Сервер поднялся: ...` with the timestamp.

If any step fails before old IP deletion, the old IP is preserved and the operation is marked `FAILED`.

## Deployment notes

Example files:

- `deploy/autovpn.service`
- `deploy/nginx.conf`

On a server, copy the project to `/opt/autovpn`, create `/etc/autovpn.env` from `.env.example`, install the virtualenv, then enable the unit:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now autovpn
```

Configure nginx with your real domain and add TLS separately.
