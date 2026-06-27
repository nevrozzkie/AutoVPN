# AutoVPN

Полностью нейрокод: проект целиком написан и собран AI/Codex.

AutoVPN — FastAPI-панель управления VPN-инфраструктурой: клиенты, подписки, установка VPN на VPS, ручная смена Aeza IPv4 и статистика.

## QuickStart

### Вариант 1: локально на macOS/Linux

```bash
curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install-local.sh -o install-local.sh
chmod +x install-local.sh
./install-local.sh
~/AutoVPN/run-local.sh
```

Открой:

```text
http://127.0.0.1:8000/setup
```

Если `8000` занят, `run-local.sh` автоматически выберет следующий свободный порт и напечатает правильный URL.

Введи в браузере:

- логин и пароль админки;
- IP VPS, где будет жить VPN;
- SSH-доступ к VPS: root/password или root/key;
- Aeza token/service id, если нужна смена IP через Aeza.

В install-скриптах необязательные поля можно оставлять пустыми. Их можно заполнить позже на `/setup`.

### Вариант 2: локально на Windows

```powershell
iwr https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\AutoVPN\run-local.ps1"
```

Открой:

```text
http://127.0.0.1:8000/setup
```

Если `8000` занят, `run-local.ps1` автоматически выберет следующий свободный порт и напечатает правильный URL.

### Вариант 3: серверный режим

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

Потом открой:

```text
http://SERVER_IP/setup
```

### Дальше

1. Зайди в `/admin`.
2. Добавь клиента.
3. Нажми `Установить VPN`.
4. Открой страницу клиента `/client/{token}`.
5. Импортируй подписку, QR или AmneziaWG-конфиг в VPN-клиент.
6. После добавления или удаления клиентов нажимай `Обновить клиентов на VPN`.
7. Если используешь Aeza, IP можно менять из админки.

## Архитектура

AutoVPN состоит из двух частей.

RU/control-plane сервер:

- хранит SQLite-базу;
- хранит клиентов, токены, UUID/password и WireGuard/Amnezia ключи;
- отдаёт `/admin`, `/client/{token}`, `/sub/{token}` и `/amnezia/{token}`;
- знает текущий active IP VPN VPS;
- ставит и синхронизирует VPN на VPS по SSH;
- при наличии Aeza API умеет покупать, переключать и удалять IPv4.

EU/VPN VPS:

- хостит сами VPN-протоколы;
- получает конфиги от AutoVPN;
- запускает Xray VLESS, Hysteria2 и AmneziaWG.

Клиенты подключаются к EU/VPN VPS, но актуальный IP и конфиги получают через AutoVPN.

## Режим без install-скрипта

Можно не передавать данные в install script. Достаточно запустить приложение с минимальным окружением, открыть `/setup` и ввести всё в браузере.

Минимальный ручной запуск:

```bash
git clone https://github.com/nevrozzkie/AutoVPN.git
cd AutoVPN
python3.12 -m venv .venv
. .venv/bin/activate
pip install -e .
DATABASE_PATH=./data/autovpn.sqlite3 uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Потом:

```text
http://127.0.0.1:8000/setup
```

После сохранения настроек откроется `/admin`. Данные из `/setup` сохраняются в SQLite, поэтому можно не держать `ADMIN_PASSWORD`, `EU_SSH_HOST`, `AEZA_TOKEN` и другие параметры в `.env`.
Пароль администратора вводится с подтверждением.
После первичной настройки `/setup` остаётся доступной, но уже требует текущий admin-логин и пароль.

Если при ручном запуске порт `8000` занят, укажи другой порт:

```bash
DATABASE_PATH=./data/autovpn.sqlite3 uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## Локальная установка

Локальный режим подходит, если человек разворачивает AutoVPN для себя: запускает панель на своём компьютере, ставит VPN на свой VPS, открывает локальную страницу клиента и импортирует подписку в свой VPN-клиент.

Все вопросы про админку, VPS, SSH и Aeza можно пропустить в installer-е и заполнить позже на `/setup`.

macOS/Linux с параметрами сразу:

```bash
./install-local.sh \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-password 'your-root-password'
```

SSH-ключ вместо пароля:

```bash
./install-local.sh \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-key-path ~/.ssh/id_ed25519
```

Запуск:

```bash
~/AutoVPN/run-local.sh
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -EuHost 203.0.113.10 `
  -EuUser root `
  -EuPassword "your-root-password"
```

Запуск:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\AutoVPN\run-local.ps1"
```

## Серверная установка

Серверный режим ставит AutoVPN как systemd-сервис и может настроить nginx перед приложением.

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

С параметрами:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh) \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-password 'your-root-password'
```

Через SSH-ключ:

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh) \
  --eu-host 203.0.113.10 \
  --eu-user root \
  --eu-key-path /root/.ssh/id_ed25519
```

Installer:

- ставит системные пакеты;
- клонирует проект в `/opt/autovpn`;
- создаёт `/etc/autovpn.env`;
- создаёт venv;
- ставит systemd unit;
- запускает `autovpn`;
- опционально настраивает nginx.

## Первичная настройка через сайт

Если пароль админки ещё не задан, AutoVPN открывает `/setup` без Basic Auth.
После задания пароля `/setup` остаётся доступной для перенастройки VPS/SSH/Aeza, но требует текущий admin-логин и пароль.

На `/setup` задаются:

- логин и пароль админки;
- подтверждение пароля админки;
- текущий IP VPN VPS;
- SSH host/user/port;
- SSH password или путь к SSH key;
- Aeza token/service id/domain, если VPS в Aeza и нужна смена IP.

Если пароль уже задан, его можно оставить пустым, чтобы не менять. Секретные поля вроде SSH password и AEZA_TOKEN тоже можно оставить пустыми, чтобы сохранить старые значения.

## Админка

Главная страница `/admin` показывает:

- текущий VPN IP;
- статусы VLESS, Hysteria2 и AmneziaWG;
- последнюю операцию смены IP;
- клиентов;
- ссылки на клиентские страницы;
- статистику VLESS и AmneziaWG;
- кнопки установки VPN, синхронизации клиентов, обновления статистики и смены IP.
- кнопку `Сбросить сервер и Aeza`, которая очищает current IP, SSH/Aeza-настройки, статусы, статистику и историю операций, но оставляет клиентов.

## Клиенты

Для каждого клиента создаётся:

- token;
- VLESS UUID;
- Hysteria2 password;
- AmneziaWG private/public/preshared key;
- персональная страница `/client/{token}`;
- подписка `/sub/{token}`;
- AmneziaWG config `/amnezia/{token}`.

`/client/{token}` показывает:

- health-статусы протоколов;
- ссылку на подписку VLESS + Hysteria2;
- QR подписки;
- AmneziaWG config;
- QR для AmneziaWG;
- места под будущие гайды.

Подписки генерируются динамически. После смены `current_ip` все `/sub/{token}` сразу начинают отдавать новый IP.

## Установка VPN на VPS

В админке нажми `Установить VPN`.

AutoVPN подключится к VPS по SSH и настроит:

- Xray VLESS REALITY + Vision на `443/tcp`;
- Hysteria2 на `8443/tcp/udp`;
- AmneziaWG на `51820/udp`;
- пользователей для всех enabled-клиентов;
- Xray StatsService для VLESS-статистики.

После изменения клиентов нажимай `Обновить клиентов на VPN`.

## Протоколы

VLESS:

- Xray;
- REALITY + Vision;
- default SNI `ok.ru`;
- serverNames по умолчанию `ok.ru,www.ok.ru`;
- client link содержит `serverName`/`sni`.

Hysteria2:

- отдельный сервис `hysteria-server`;
- username `client{id}`;
- password из `clients.hysteria_password`;
- в подписке используется `insecure=1`, потому MVP генерирует self-signed TLS.

AmneziaWG:

- конфиг сервера пишется в `/etc/amnezia/amneziawg/awg0.conf`;
- клиентский конфиг отдаётся на `/amnezia/{token}`;
- QR доступен на странице клиента.

## Статистика

Кнопка `Обновить статистику` собирает данные с VPS по SSH.

Сейчас собирается:

- VLESS через Xray StatsService;
- AmneziaWG через `awg show awg0 dump`.

Hysteria2-статистика пока не собирается, потому Hysteria2 работает отдельным сервисом и не проходит через Xray.

## Смена IP Aeza

Смена IP доступна только если заданы:

- `AEZA_TOKEN`;
- `AEZA_SERVICE_ID`.

Их можно ввести на `/setup` или передать через env/install script.

Важно: покупка нового IPv4 в Aeza может списывать деньги. Цена показывается в евро на странице подтверждения.

Автоматический цикл смены IP:

1. Проверить, что другая операция не идёт.
2. Получить список IPv4.
3. Найти текущий main IP.
4. Купить новый IPv4.
5. Подождать 5 минут, потому Aeza может показать IP не сразу.
6. Дождаться появления нового IP в списке.
7. Сделать новый IP главным.
8. Перезагрузить VPS.
9. Дождаться healthcheck SSH, VLESS и Hysteria.
10. Обновить `current_ip`.
11. Синхронизировать VPN-конфиги на новом IP.
12. Удалить старый IPv4.

Старый IP не удаляется до успешного переключения и обновления `current_ip`.

Есть и ручной режим `/admin/ip`:

- обновить список IP;
- купить новый IP;
- выбрать IP главным;
- удалить не главный IP.

## Переменные окружения

Основные:

```bash
DATABASE_PATH=./data/autovpn.sqlite3
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

EU_SSH_HOST=203.0.113.10
EU_SSH_USER=root
EU_SSH_PORT=22
EU_SSH_PASSWORD=...
EU_SSH_KEY_PATH=/home/autovpn/.ssh/id_ed25519

AEZA_TOKEN=...
AEZA_SERVICE_ID=...
AEZA_IPV4_DOMAIN=
```

Если значения заданы через `/setup`, они хранятся в SQLite как `config.*` и имеют приоритет в runtime.

## Сервисные команды

На сервере:

```bash
systemctl status autovpn
systemctl restart autovpn
journalctl -u autovpn -f
```

Локально:

```bash
~/AutoVPN/run-local.sh
```

## Ограничения MVP

- Hysteria2 per-client traffic пока не собирается.
- AmneziaVPN не объединяется в общую подписку, отдаётся отдельным config/QR.
- Для AmneziaWG auto-setup ожидается Ubuntu-compatible VPS с Amnezia PPA.
- UI простой, без React.
