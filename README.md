# AutoVPN

Полностью нейрокод: проект целиком написан и собран AI/Codex.

AutoVPN — FastAPI-панель управления VPN-инфраструктурой: клиенты, подписки, установка VPN на VPS, gated-ротация Aeza IPv4 и статистика.

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
- хранит постоянную SQLite-базу отдельно, в `/var/lib/autovpn/autovpn.sqlite3`;
- создаёт `/etc/autovpn.env`;
- создаёт venv;
- ставит systemd unit;
- запускает `autovpn`;
- опционально настраивает nginx.

### Безопасное повторное обновление

Перед обновлением installer подготавливает исходники во временной директории и проверяет их,
а затем создаёт согласованный backup SQLite через SQLite backup API и проверяет его через
`PRAGMA integrity_check`. Каталоги данных и backup имеют права `0700`, файлы БД и backup —
`0600`.

Серверная установка использует `DATA_DIR=/var/lib/autovpn` по умолчанию. При первом
обновлении старый `/opt/autovpn/data/autovpn.sqlite3` автоматически копируется туда через
SQLite backup API; исходная legacy-БД остаётся на месте. Локальная установка использует
`~/.local/share/autovpn` и так же переносит существующую БД из `~/AutoVPN/data` или выбранного
`APP_DIR/data`. Другой каталог можно задать переменной `DATA_DIR`.

При повторной установке существующий `/etc/autovpn.env` или локальный `.env` сохраняется:
installer создаёт его backup в `DATA_DIR/backups` и меняет только `DATABASE_PATH` на новый
persistent path. Остальные значения при обновлении следует менять через `/admin/setup` или
в env-файле вручную. `data`, `.env`, `.git` и `.venv` исключены из синхронизации исходников.

Это безопасное in-place обновление, но не атомарный release/symlink deploy: если установка
зависимостей или синхронизация исходников оборвётся после preflight, автоматического rollback
к прежней версии кода нет. Проверенная резервная копия БД остаётся в `DATA_DIR/backups`.

### Desired/applied конфигурация VPN

Изменения VPN-настроек и клиентов увеличивают `desired revision`. При запуске
`Установить / синхронизировать VPN` AutoVPN в одной SQLite-транзакции сохраняет канонический
immutable snapshot этой ревизии и привязывает к нему install operation. Весь отправляемый на
VPS скрипт строится из сохранённого snapshot, поэтому параллельное изменение настроек не меняет
уже запущенную установку.

`applied revision` появляется только после успешного завершения SSH-установки и последующей
проверки протоколов. Для существующей БД состояние VPS после миграции считается неизвестным,
а не автоматически применённым: dashboard показывает `не подтверждено` до первой успешной
синхронизации. Ошибка сохраняет предыдущую подтверждённую ревизию. Удалённый клиент сразу
исчезает из UI и публичных URL, но его token/keys и адрес остаются в БД до успешного применения
ревизии с удалением; только после этого запись можно безопасно очистить.

Legacy shell-скрипт пока выполняет последовательные команды без атомарного rollback VPS.
Transactional install и безопасная ротация IP используют immutable snapshot операции, но оба
новых mutation-контура выключены по умолчанию до отдельной интеграционной проверки.

### Staged transactional apply (по feature flag)

Новый контур включается только через `ENABLE_TRANSACTIONAL_VPN_APPLY=1`; по умолчанию он
выключен, и `/admin/install/run` использует прежний install-script как compatibility fallback.
Это намеренный integration gate: перед включением флага скрипт нужно проверить на отдельном VPS
с теми же версиями Xray, Hysteria2, AmneziaWG и systemd, что используются в production.

При включённом флаге первый запуск при необходимости выполняет bootstrap пакетов, после чего
использует общий config-only apply. Повторный запуск с удовлетворённым bootstrap marker не
выполняет `apt`/download заново. Config-only script создаёт приватный staging-каталог, валидирует
включённые конфиги до переключения, сохраняет текущие файлы и состояния systemd, устанавливает
файлы через rename, перезапускает и проверяет сервисы. Ошибка после начала переключения запускает
rollback файлов и предыдущих enabled/active состояний; staging удаляется. Вывод validation-команд
подавляется, чтобы конфиги и секреты не попадали в журнал операции.

Перед switch создаётся приватный versioned backup вне staging:
`/var/lib/autovpn/config-backups/revision-...`. `manifest.ready` публикуется через rename только
после записи manifest, успешный apply оставляет marker `APPLIED`, rollback — `ROLLED_BACK`.
Текущий запуск восстанавливается именно из этого backup, а последние 10 каталогов сохраняются
для ручного восстановления; содержимое конфигов в operation log не выводится.

Этот механизм уменьшает риск частично применённой конфигурации, но до отдельной integration-
проверки не считается доказанным атомарным rollback на всех целевых дистрибутивах. Firewall rules
по-прежнему добавляются best-effort и не удаляются rollback-контуром.

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
- Стартовые порты протоколов: VLESS `443/tcp` включён, Hysteria `8443/udp` включена, AmneziaWG `51820/udp` включён. У включённых и выключенных протоколов нельзя сохранять одинаковые порты. После изменения портов нажмите `Установить / синхронизировать VPN`.
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
Ответы legacy URL с клиентским token получают `Cache-Control: private, no-store` и
`Referrer-Policy: no-referrer`. Сам token всё ещё находится в URL для обратной совместимости:
стандартный nginx access log может записать такой URI. Маскирование URI в reverse-proxy логах
остаётся отдельным deployment-шагом; текущая версия не заявляет, что эти логи очищены.

## Установка VPN на VPS

В админке нажми `Установить / синхронизировать VPN`.

AutoVPN подключится к VPS по SSH и настроит:

- Xray VLESS REALITY + Vision на настроенном VLESS TCP-порту, стартовое значение `443`;
- экспериментальный Hysteria2-конфиг на настроенном Hysteria UDP-порту, стартовое значение `8443`;
- AmneziaWG на настроенном UDP-порту, стартовое значение `51820`, если пакет доступен для версии Ubuntu;
- пользователей для всех enabled-клиентов;
- Xray StatsService для VLESS-статистики.

После изменения клиентов нажимай `Установить / синхронизировать VPN`.

## Протоколы

VLESS:

- Xray;
- REALITY + Vision;
- SNI `ok.ru`;
- serverNames `ok.ru,www.ok.ru`;
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

1. В одной SQLite-транзакции зафиксировать desired revision, immutable VPN snapshot, operation и общий VPS lease.
2. Получить список IPv4 и найти текущий main IP из bound snapshot.
3. Перед каждым потенциально платным/сетевым действием сохранить durable milestone, затем ровно один раз купить IPv4 и сделать его главным.
4. Проверить SSH на настроенном `EU_SSH_PORT` и применить только bound snapshot через config-only transactional apply.
5. Проверить VLESS/Hysteria через новый endpoint; одного UDP send недостаточно.
6. В одной SQLite-транзакции опубликовать новый `current_ip` и applied revision.
7. Только затем попытаться удалить старый IPv4. Потерянный ответ DELETE считается ambiguous и требует проверки в Aeza, а не доказательством сохранности или удаления IP.

Контур включается только когда одновременно заданы `ENABLE_TRANSACTIONAL_VPN_APPLY=1` и
`ENABLE_SAFE_AEZA_IP_ROTATION=1`; оба флага по умолчанию выключены.
При выключенном флаге старый небезопасный rotation runner не запускается. Существующие URL
`/admin/ip/buy`, `/admin/ip/{id}/make-main` и `/admin/ip/{id}/delete` сохранены для совместимости,
но прямые POST-мутации перенаправляют на gated safe rotation и не могут обходить lease,
snapshot или safety ordering.
`/admin/ip` остаётся read-only inventory.

Для AmneziaWG наличие peer key на сервере теперь честно считается только конфигурацией, а не
handshake или end-to-end проверкой нового public IP. После публикации нового IP при включённом
AmneziaWG операция сохраняет старый IPv4, завершает двухфазный переход с
`manual_verification_required` и просит проверить клиентский handshake вручную. Автоматического
cleanup старого IP до такого внешнего verifier нет.

Ambiguous cleanup и ожидающая проверки AmneziaWG-операция ставят safety hold на новые ротации.
После ручной сверки IP/main-state в кабинете Aeza администратор открывает ссылку
`Ручная сверка` в `/admin/operations`, вводит точное подтверждение `RECONCILE` и короткую заметку.
Этот POST только записывает `RECONCILED`, timestamp и note в SQLite; он не вызывает Aeza или SSH.

## Статус и перезагрузка VPS в Aeza

Страница `/admin/server` показывает последний явно полученный статус услуги Aeza, IP у
провайдера, доступность SSH и результаты проверок включённых VPN-сервисов. GET-запрос страницы
ничего не меняет: свежая проверка запускается отдельной POST-кнопкой и сохраняется в журнале
операций.

Перезагрузка доступна только при настроенных `AEZA_TOKEN` и `AEZA_SERVICE_ID` и требует ввести
точное подтверждение `REBOOT`. Перед внешним вызовом AutoVPN сохраняет состояние `SENDING`, а
после подтверждённого ответа Aeza — `SENT`. Команда перезагрузки отправляется ровно один раз.
Если ответ Aeza потерян или процесс остановлен после `SENDING`, операция становится
`AMBIGUOUS`: AutoVPN не повторяет reboot автоматически, чтобы не вызвать вторую перезагрузку.
Безопасный следующий шаг — отдельное обновление статуса Aeza на `/admin/server`.

Установка VPN, смена IP, проверка статуса и перезагрузка используют общий durable lock VPN VPS.
Одновременно запускается только одна такая операция. Lock освобождается завершившим его
владельцем; незавершённые операции восстанавливаются в terminal state при старте приложения.
Это координация control-plane, а не атомарный rollback действий на стороне Aeza или VPS.

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

# Необязательные bounded timeouts для статуса/перезагрузки Aeza VPS
SERVER_STATUS_TIMEOUT_SECONDS=15
SERVER_REBOOT_TIMEOUT_SECONDS=300
SERVER_POLL_INTERVAL_SECONDS=5
SERVER_SSH_PROBE_TIMEOUT_SECONDS=5
SERVER_COMMAND_TIMEOUT_SECONDS=30

# Default OFF: staged transactional install/config apply
ENABLE_TRANSACTIONAL_VPN_APPLY=0

# Default OFF: durable Aeza IP rotation; requires transactional apply canary first
ENABLE_SAFE_AEZA_IP_ROTATION=0
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
