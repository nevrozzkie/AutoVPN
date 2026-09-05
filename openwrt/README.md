# autovpn-controller

Роутерный компонент [AutoVPN 2.0](../README.md), исходники находятся в `openwrt/`
того же репозитория. Корневой `install.sh` AutoVPN остаётся установщиком сайта;
его нельзя запускать на роутере.

Нативный OpenWrt-пакет control plane для Cudy WR3000S v1. Цель пакета —
небольшой control plane на ucode + ubus/rpcd + procd и современная LuCI JS-страница
без Python, Node.js и собственной БД на роутере.

Каждый installed controller представляет один физический Router: его `router_id`,
root-only сменяемый credential и journal/state отделены от других устройств. Серверный
реестр связывает credential с Router, поэтому замена или отзыв токена не меняет
историю и конфигурацию устройства.

Целевая сборка первого этапа зафиксирована как официальный OpenWrt
`mediatek/filogic`, profile `cudy_wr3000s-v1`, **только stock layout**. U-Boot-mod
не поддерживается и не нужен для этого milestone.

## Обслуживание из LuCI (0.7.0)

Страница **Services → AutoVPN → Maintenance** принимает новый токен, перепривязывает
роутер, сбрасывает только настройки AutoVPN и обновляет приложение из подписанного
GitHub Release. После сброса, перепривязки и установки обновления VPN остаётся
закрыт до явного возобновления. WAN/LAN, Wi-Fi и прошивка не сбрасываются.
Подробнее: [обслуживание и восстановление](docs/maintenance.md).

## Установка одной командой и настройка в панели

Добавлены [установщик](scripts/install.sh), [подготовка GitHub Release](docs/releases.md)
и мастер **Services → AutoVPN → Setup**. Сайт AutoVPN в раздаче пакетов не участвует:
он нужен только для API подписки и ключей. Обычные зависимости загружаются из
официальных HTTPS feeds OpenWrt; наш пакет — из конкретного GitHub Release.

**Релиз ещё не собран и не опубликован.** Файл `scripts/install.sh` является
шаблоном: без зафиксированных адреса релиза, хеша manifest и публичного ключа он
отказывается запускаться. Рабочей публичной команды пока нет. После подготовки
и публикации релиза пользователь скачает установщик одной командой, дальше SSH
для первичной настройки не нужен. Прошивка OpenWrt и работающее WAN-подключение
должны быть уже установлены; загрузчик, разметку и прошивку installer не меняет.

В мастере вводятся HTTPS-адрес сайта, Router ID, отдельный токен этого роутера,
базовое имя Wi-Fi и пароль WPA2. WAN определяется автоматически. Включить радио
или отключить hardware/software offloading при необходимости можно в стандартной
LuCI (Network → Wireless / Firewall). Затем мастер создаёт SSID, ждёт подтверждения
в течение 3 минут и только после подтверждения разрешает включить AutoVPN.
Подтверждение делается через **исходную административную LAN/SSID**, не через
управляемую сеть, доступ к админке из которой запрещён.

Токен передаётся из авторизованной LuCI-сессии через stdin и хранится в `0600`
файле, не в UCI/argv/логах. Используй HTTPS-доступ к LuCI или доверенную проводную
LAN: авторизация веб-сессии сама по себе не шифрует HTTP. Мастер не сбрасывает и
не перепривязывает уже работающую установку.

Installer проверяет точный release/target/architecture/kernel ABI, подписи APK,
SHA256 и бюджет свободного места, затем проверяет план зависимостей APK.
`sh install.sh --check` скачивает и проверяет релиз без установки пакетов.
Расхождение ABI прерывает **всю установку**, без частичного подбора модулей.
Доступность AWG определяется составом подписанного релиза: обязательна пара
`kmod-amneziawg` + `amneziawg-tools`. Без неё доступны VLESS/Hysteria2.
Это первый установщик, не средство отката пакетов или автоматического обновления.

Совместимость со штатным ucode 25.12.5: subprocess-вызовы проходят через общий
`autovpn.process`, который формирует строку для старого `fs.popen`, отдельно
экранируя каждый аргумент POSIX-кавычками. Секреты остаются в stdin/приватных
файлах. Ошибки отсутствия файла различаются через `fs.error()`, а не сравнением
`fs.access()` с `false`. Проверено по [версии ucode в OpenWrt 25.12.5](https://github.com/openwrt/openwrt/blob/v25.12.5/package/utils/ucode/Makefile)
и [её реализации fs](https://github.com/jow-/ucode/blob/85922056ef7abeace3cca3ab28bc1ac2d88e31b1/lib/fs.c).

## Ранее реализованный runtime (0.5.0)

Добавлен runtime sing-box для VLESS/Hysteria2, auto через HTTPS URLTest, локальные
direct-исключения, IPv4 TUN routing, DNS через VPN, guard/kill-switch, отдельный
procd-сервис и применение policy из LuCI. Подробности, ограничения и первый запуск:
[Runtime](docs/runtime.md). Дополнительно реализованы [автосоздание WPA2 SSID](docs/networks.md)
с пользовательскими базовым именем и паролем, rollback/подтверждением, а также
[kernel AmneziaWG](docs/amnezia.md) с опциональными пакетами под точный kernel ABI.
Hysteria2 по умолчанию сохраняет `insecure` из подписки. В 0.9 добавлен опциональный
[zapret2 на внешнем VPN-транспорте](docs/zapret.md), с установкой и настройкой в LuCI.

- package `Makefile`, UCI defaults, procd init и rpcd/ubus object
  `luci.autovpn` с методами `status` и `refresh`; одна menu-bound ACL-группа даёт
  обычной авторизованной admin-сессии read/status и write/refresh;
- read-only по отношению к секретам LuCI JS overview: UI получает только redacted
  status; отдельный мастер принимает новый credential без возможности прочитать его;
- root-only хранение Bearer credential и snapshot journal;
- JSON Schema server snapshot v3, совпадающая с текущим
  `GET /api/v2/router/snapshot`, и исполняемый ucode validator этого subset;
- атомарно заменяемый journal с `desired`, `applied`, `last_good`, rejected state и
  write-ahead фазами `PREPARING` / `PREPARED` / `ACTIVATING` / `VERIFYING` /
  `ROLLING_BACK`;
- восстановление незавершённого apply через обязательный adapter rollback и
  `FAIL_CLOSED`, если rollback не подтверждён;
- rollback на любой journal persist failure после внешнего `prepare`, `activate`
  или `verify`; commit после verify строится из копии, поэтому rollback видит
  прежний durable `VERIFYING`, а не новый `applied`;
- отдельный journal-independent runtime action `fail-closed`, вызываемый также при
  structurally invalid journal: устанавливает статический deny guard и останавливает
  managed sing-box;
- полная allowlist/invariant-проверка journal при загрузке с пределом 256 KiB;
- ETag/304 no-op и no-op для уже применённого ETag;
- durable pending apply-result с неизменяемыми `idempotency_key`, body и `If-Match`.
  Он удаляется только после успешного ответа adapter’а;
- rolled-back apply может отправить `FAILED` result в той же refresh-итерации, но
  итог refresh остаётся `ok=false`, `outcome=ROLLED_BACK` и никогда не называется
  `applied`;
- чистая health state machine: 3 ошибки переводят outbound в `down`, 2 успеха — в
  `up`, после `down` действует cooldown; manual override не выбирает unhealthy
  outbound и потому не отключает fail-closed;
- host-side тесты перечисленных переходов;
- production HTTPS adapter на официальном OpenWrt `curl`: только HTTPS, системный
  CA bundle, TLS peer/hostname verification, redirects/proxy выключены, bounded
  connect/total timeout, network body и accepted headers не более 16 KiB;
- hard streaming sink для body и headers: private FIFO + BusyBox
  `head -c 16385` закрывает producer при overflow, так что chunked response не может
  сначала вырасти без ограничения в `/tmp`;
- GET snapshot с Bearer из root-only файла, conditional `If-None-Match` только для
  strong ETag, строгими вариантами `200 object + ETag` и `304 empty + ETag`;
- snapshot не проходит через stdout: adapter оставляет уникальный `0600` handoff,
  controller читает его bounded-вызовом и удаляет перед state validation;
- PUT берёт только exact `pending_report` из bounded 256 KiB journal, повторно
  сверяет key/ETag и server-compatible schema, принимает только документированный
  HTTP 200 response (включая `replayed=true`) и не маскирует `201`/`409`/`412`;
- credential, headers и PUT body передаются curl только через private config/body
  files, а не argv/env; trap удаляет весь transport workspace на любом исходе;
- controller читает adapter stdout максимум на 4097 bytes; packaged adapter имеет
  собственный hard bound 4096 bytes и печатает только allowlisted metadata/error.

`snapshot.schema.json` предназначена также для server/tooling. На роутере общий
JSON-Schema engine не ставится ради размера: `state.uc` содержит эквивалентную
allowlist-проверку обязательных полей, типов, диапазонов и связей enabled/payload.

## Что намеренно не объявляется готовым

Поставляемые `http-adapter` и sing-box `runtime-adapter` реализованы и проверены на
host. Проверка Linux networking на самом роутере ещё не выполнена.

Не реализованы:

- сборка и измерение реального APK-набора (включая AmneziaWG) и проверка lifecycle
  на устройстве; установочный ABI gate реализован, но не заменяет такую проверку;
- отдельные zapret-SSID и packet-level проверка маркировки на устройстве;
  [первый этап outer transport](docs/zapret.md) уже реализован, но результат обхода
  у конкретного провайдера не проверен;
- router-side и hardware-in-the-loop тесты.

Текущий server snapshot v3 содержит stable `router_id` и protocol credentials.
В версии 0.8 `auto` переключает только после отказа текущего VPN. В LuCI добавлена
ручная диагностика `Ping all` через YouTube/Instagram без изменения выбора.
Подробности и ограничения: [runtime](docs/runtime.md).
Локальная policy хранится в UCI и проходит отдельную строгую проверку перед рендером;
сервер не может прислать shell-команды или глобальный JSON sing-box.

Текущий Hysteria2 snapshot может содержать `tls.insecure=true`. Настройка LuCI
«Use subscription setting» сохраняет его; «Require a trusted certificate»
исключает такой профиль из auto и отказывает при ручном выборе. Шифрование остаётся,
но в первом режиме подписка может отключать проверку сертификата VPN-сервера.

## Хранение и граница секретов

| Путь | Mode | Содержимое |
| --- | --- | --- |
| `/etc/config/autovpn` | `0600` | URL, router_id, policy, базовое имя и пароль Wi-Fi; без API credential и tunnel keys; доступен LuCI admin-сессии |
| `/etc/autovpn/networks/journal.json` | `0600`, parent `0700` | before/after сетевых UCI-конфигов, включая Wi-Fi ключи; не передаётся в RPC/на сайт |
| `/etc/autovpn/credentials` | `0600`, parent `0700` | одна строка `avrt_<id>.<secret>` |
| `/etc/autovpn/state/journal.json` | `0600`, parent `0700` | server snapshots, включая protocol secrets, и transactional state |
| `/etc/autovpn/runtime/` | файлы `0600`, parent `0700` | current/previous/prepared bundles, сгенерированные конфиги для запуска |

Credential задаётся через stdin, чтобы не попадать в argv процесса на роутере. Для
интерактивного ввода без сохранения самого токена в истории bash/zsh можно использовать
скрытый `read`. Перед
атомарной записью проверяется точный server regex:
`avrt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{43,128}`:

```sh
ssh root@router autovpnctl set-router-id '<ROUTER_ID_FROM_AUTOVPN>'
read -rs AUTOVPN_ROUTER_TOKEN
printf '\n'
printf '%s\n' "$AUTOVPN_ROUTER_TOKEN" | ssh root@router autovpnctl set-credential
unset AUTOVPN_ROUTER_TOKEN
```

`option router_id` обязателен в `autovpn.main`; controller сверяет его с `router_id`
из snapshot до staging. Credential остаётся
сменяемым секретом и серверная сторона должна принять его только для этого Router.
LuCI показывает только `credential_configured`. Controller не пишет snapshot,
credential, adapter stdout или полный URL в системный лог. HTTP adapter не выводит
их также в stdout/stderr и не помещает credential/body в argv или environment.
Curl видит secrets только через private `0600` config/body files в private `0700`
directory, удаляемой обязательным trap.

Journal пишется в `journal.json.new` с mode `0600`, затем заменяется `rename(2)` в
том же каталоге. Это даёт атомарную видимость одного старого или одного нового
состояния. В ucode fs API нет явного file/directory `fsync`, поэтому сохранность
последней записи при внезапном отключении питания на UBIFS остаётся hardware gate;
write-ahead phase позволяет после загрузки выбрать rollback вместо продолжения
неизвестной транзакции.

## Проверка на host

Для host-тестов требуются Node.js и Python:

```sh
sh scripts/check.sh
```

Тесты загружают **тот же** pure ucode source `state.uc`, `journal.uc`, `health.uc`,
`orchestration.uc` и `http.uc` через небольшой Node compatibility harness. Shell
adapter дополнительно исполняется end-to-end с fake `curl`/`ucode`: тесты проверяют
  argv/env boundary, exact headers/body, timeouts/TLS/redirect options, 304,
  chunked body/header termination и физический предел capture 16385 bytes, journal
  больше 16 KiB, malformed status/JSON/ETag, exact replay и cleanup. Fault-injection
  отдельно ломает persist после prepare/activate/verify и проверяет rollback плюс
  fallback в fail-closed.

На текущем host нет бинарника `ucode`, поэтому OpenWrt entrypoints проверяются
статически и через тот же pure module, но не настоящим target interpreter. Перед
установкой всё ещё нужны `ucode -c`, package build и HTTPS smoke test в точном
OpenWrt 25.12.x image. Локальный HTTP integration server намеренно не используется:
production gate никогда не принимает `http://`.

## Установка в OpenWrt build tree

Каталог имеет форму самостоятельного package source. Его можно поместить, например,
в `package/autovpn-controller`, затем выбрать
`Network -> VPN -> autovpn-controller`. В зависимостях явно есть `curl`, `ca-bundle`,
`busybox`, `sing-box-tiny`, `ip-full`, `firewall4` и `kmod-nft-nat`.
AWG-пакеты ставятся отдельно под точный kernel ABI; `kmod-nft-queue` пока не включён.

После установки сервис выключен (`enabled=0`). Перед первым refresh нужно подготовить
VPN bridge, firewall, WAN device, `base_url`, `router_id` и credential по
[инструкции первого запуска](docs/runtime.md#первый-запуск-на-устройстве).

## Следующие этапы

1. Собрать package в точном OpenWrt 25.12.x SDK/ImageBuilder и выполнить target-side
   `ucode -c` плюс HTTPS smoke tests с реальным CA/DNS/server.
2. Проверить WPA2 SSID/подтверждение/boot rollback и reset приложения на устройстве.
3. Добавить policy для `direct_zapret`/`vpn_zapret`; последний означает локальный
   nfqws2 на отдельном внешнем transport до VPN-сервера (без смешения с обычным VPN SSID).
4. Проверить коллизии nft/route rules, firewall reload и DNS на реальном устройстве.
5. Отдельно собрать и проверить AWG под точный kernel ABI; отдельно — zapret и
   outer-packet marking для каждого protocol/endpoint IP/port/WAN candidate.
   Сейчас `Ping all` проверяет каждый VPN с явно выбранной для него zapret-политикой;
   это не отдельные кандидаты с zapret/без него и не автоматический подбор стратегии.
6. Собрать единый stock-layout image и измерить SquashFS/sysupgrade, затем выполнить
   reboot/power-loss/rollback tests на Cudy WR3000S v1.

Точный adapter ABI описан в [docs/adapter-contract.md](docs/adapter-contract.md), а
формат journal — в [docs/journal.md](docs/journal.md).
