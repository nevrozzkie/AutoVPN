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
http://127.0.0.1:8000/admin/setup
```

Если `8000` занят, `run-local.sh` автоматически выберет следующий свободный порт и напечатает правильный URL.

Введи в браузере:

- логин и пароль админки;
- IP VPS, где будет жить VPN;
- SSH-доступ к VPS: root/password или root/key;
- Aeza token/service id, если нужна смена IP через Aeza.

Для EU/VPN VPS сейчас лучше выбирать Ubuntu 24.04 LTS. Ubuntu 26.04 может работать для VLESS, но AmneziaWG PPA для неё может быть недоступен.

Install-скрипты просто ставят приложение. Настройки вводятся в браузере на `/admin/setup`.

### Вариант 2: локально на Windows

```powershell
iwr https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.ps1 -OutFile install.ps1
powershell -ExecutionPolicy Bypass -File .\install.ps1
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\AutoVPN\run-local.ps1"
```

Открой:

```text
http://127.0.0.1:8000/admin/setup
```

Если `8000` занят, `run-local.ps1` автоматически выберет следующий свободный порт и напечатает правильный URL.

### Вариант 3: серверный режим

```bash
bash <(curl -Ls https://raw.githubusercontent.com/nevrozzkie/AutoVPN/main/install.sh)
```

Потом открой:

```text
http://SERVER_IP:8000/admin/setup
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
- запускает Xray VLESS и AmneziaWG. Hysteria2 пока оставлена как экспериментальная заготовка.

Клиенты подключаются к EU/VPN VPS, но актуальный IP и конфиги получают через AutoVPN.

## Режим без install-скрипта

Можно не передавать данные в install script. Достаточно запустить приложение с минимальным окружением, открыть `/admin/setup` и ввести всё в браузере.

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
http://127.0.0.1:8000/admin/setup
```

После сохранения настроек откроется `/admin`. Данные из `/admin/setup` сохраняются в SQLite, поэтому можно не держать `ADMIN_PASSWORD`, `EU_SSH_HOST`, `AEZA_TOKEN` и другие параметры в `.env`.
Пароль администратора вводится с подтверждением.
После первичной настройки `/admin/setup` остаётся доступной, но уже требует текущий admin-логин и пароль.

Если при ручном запуске порт `8000` занят, укажи другой порт:

```bash
DATABASE_PATH=./data/autovpn.sqlite3 uvicorn app.main:app --host 127.0.0.1 --port 8001
```

## Локальная установка

Локальный режим подходит, если человек разворачивает AutoVPN для себя: запускает панель на своём компьютере, ставит VPN на свой VPS, открывает локальную страницу клиента и импортирует подписку в свой VPN-клиент.

Installer ставит приложение без вопросов про админку, VPS, SSH и Aeza. Всё это заполняется в браузере на `/admin/setup`.

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

Если пароль админки ещё не задан, AutoVPN открывает `/admin/setup` без Basic Auth.
После задания пароля `/admin/setup` остаётся доступной для перенастройки VPS/SSH/Aeza, но требует текущий admin-логин и пароль.

На `/admin/setup` задаются:

- логин и пароль админки;
- подтверждение пароля админки;
- текущий IP VPN VPS;
- отдельный блок портов протоколов: включение/выключение VLESS, Hysteria и AmneziaWG плюс порт для каждого;
- SSH host/user/port;
- SSH password или путь к SSH key;
- Aeza token/service id/domain, если VPS в Aeza и нужна смена IP.

Если пароль уже задан, его можно оставить пустым, чтобы не менять. Секретные поля вроде SSH password и AEZA_TOKEN тоже можно оставить пустыми, чтобы сохранить старые значения.
Там же находится кнопка `Сбросить сервер и Aeza`: она очищает current IP, SSH/Aeza-настройки, статусы, статистику и историю операций, но оставляет клиентов.

Пояснения к полям:

- `SSH host` можно оставить пустым, тогда AutoVPN будет подключаться по SSH к текущему IP VPN.
- `SSH key path` можно оставить пустым, если используется SSH password.
- Порты протоколов по умолчанию: VLESS `443/tcp` включён, Hysteria `8443/udp` выключена, AmneziaWG `51820/udp` включён. У включённых и выключенных протоколов нельзя сохранять одинаковые порты. После изменения портов нажмите `Установить / синхронизировать VPN`.
- `AEZA_SERVICE_ID` проще получить через Network-панель браузера в my.aeza.net: откройте страницу VPS и найдите requests вида `/api/v2/services/{serviceId}/...`. Также service id равен номеру услуги в истории/карточке VPS.
- `AEZA_IPV4_DOMAIN` — название VPS в Aeza. Aeza требует его при покупке нового IPv4.

## Админка

Главная страница `/admin` показывает:

- текущий VPN IP;
- статусы включённых протоколов; выключенные протоколы не показываются клиентам;
- последнюю операцию смены IP;
- клиентов;
- ссылки на клиентские страницы;
- статистику VLESS и AmneziaWG;
- кнопки установки VPN, синхронизации клиентов, обновления статистики и смены IP.

## Клиенты

Для каждого клиента создаётся:

- token;
- VLESS UUID;
- общий Hysteria2 password, зарезервированный для будущей доработки;
- AmneziaWG private/public/preshared key;
- персональная страница `/client/{token}`;
- подписка `/sub/{token}`;
- AmneziaWG config `/amnezia/{token}`.

`/client/{token}` показывает:

- health-статусы протоколов;
- ссылку на подписку для включённых subscription-протоколов;
- QR подписки;
- AmneziaWG config;
- QR для AmneziaWG;
- места под будущие гайды.

Подписки генерируются динамически. После смены `current_ip` все `/sub/{token}` сразу начинают отдавать новый IP.

## Установка VPN на VPS

В админке нажми `Установить / синхронизировать VPN`.

AutoVPN подключится к VPS по SSH и настроит:

- Xray VLESS REALITY + Vision на настроенном VLESS TCP-порту, по умолчанию `443`;
- экспериментальный Hysteria2-конфиг на настроенном Hysteria UDP-порту, по умолчанию `8443`, но протокол выключен по умолчанию и в UI считается не заведённым;
- AmneziaWG на настроенном UDP-порту, по умолчанию `51820`, если пакет доступен для версии Ubuntu;
- пользователей для всех enabled-клиентов;
- Xray StatsService для VLESS-статистики.

После изменения клиентов нажимай `Установить / синхронизировать VPN`.

## Протоколы

VLESS:

- Xray;
- REALITY + Vision;
- default SNI `ok.ru`;
- serverNames по умолчанию `ok.ru,www.ok.ru`;
- client link содержит `serverName`/`sni`.

Hysteria2:

- отдельный сервис `hysteria-server`;
- пока не считается рабочим протоколом в AutoVPN;
- на странице клиента показывается заглушка: `не получилось пока завести, увы`;
- текущая попытка использует общий `hysteria.password` и `auth.type: password`;
- в подписке используется `insecure=1`, потому MVP генерирует self-signed TLS;
- статус Hysteria2 не проверяет реальное подключение клиента и не должен восприниматься как готовность протокола.

AmneziaWG:

- конфиг сервера пишется в `/etc/amnezia/amneziawg/awg0.conf`;
- клиентский конфиг отдаётся на `/amnezia/{token}`;
- QR доступен на странице клиента.

## Статистика

Кнопка `Обновить статистику` собирает данные с VPS по SSH.

Сейчас собирается:

- VLESS через Xray StatsService;
- AmneziaWG через `awg show awg0 dump`.

Hysteria2-статистика не собирается: протокол пока оставлен как заготовка и не считается заведённым.

## Смена IP Aeza

Смена IP доступна только если заданы:

- `AEZA_TOKEN`;
- `AEZA_SERVICE_ID`.

Их можно ввести на `/admin/setup` или передать через env/install script.

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
9. Дождаться healthcheck SSH и включённых VPN-протоколов.
10. Обновить `current_ip`.
11. Синхронизировать VPN-конфиги на новом IP.
12. Удалить старый IPv4.

Старый IP не удаляется до успешного переключения и обновления `current_ip`.

Есть и ручной режим `/admin/ip`:

- обновить список IP;
- купить новый IP; новокупленный IPv4 может появиться в списке с задержкой примерно 1-2 минуты;
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
AEZA_IPV4_DOMAIN=your-vps-name
```

Если значения заданы через `/admin/setup`, они хранятся в SQLite как `config.*` и имеют приоритет в runtime.

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

- Hysteria2 пока не заведена и оставлена как экспериментальная заготовка.
- AmneziaVPN не объединяется в общую подписку, отдаётся отдельным config/QR.
- Для AmneziaWG auto-setup лучше использовать Ubuntu 24.04 LTS. Если AmneziaWG-пакет недоступен, установка VLESS продолжится, а AmneziaWG будет пропущен с warning в логе.
- UI простой, без React.
