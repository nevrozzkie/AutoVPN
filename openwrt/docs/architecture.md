# Архитектура первого этапа

Статус: проектное решение, 4 сентября 2026 года. Документ не утверждает, что
описанные пакеты уже собраны или проверены на реальном устройстве.

Реализация 0.5 от 5 сентября описана в `openwrt/docs/` репозитория AutoVPN: автосоздание WPA2
SSID уже реализовано; текущий AWG backend управляет kernel-интерфейсом напрямую,
а не через netifd, и принимает профиль v1 текущего сервера. Упоминания AWG2/netifd
ниже остаются проектным направлением, не описанием доступного runtime.

## 1. Контекст и критерии готовности

Целевая система — Cudy WR3000S v1 с stock-layout и OpenWrt 25.12.x. Официальная
таблица оборудования указывает `mediatek/filogic`, `aarch64_cortex-a53`, 256 МиБ RAM
и 128 МиБ NAND. В 25.12 OpenWrt перешёл с opkg на apk, поэтому собственные и внешние
пакеты должны собираться именно как APK для точного релиза ядра.

Этап 1 считается архитектурно готовым, если последующая реализация сможет показать:

1. повторяемую установку в stock-layout без U-Boot-mod;
2. четыре изолированные managed-сети и неизменную административную `lan`;
3. VLESS/Hysteria2 через `sing-box-tiny` и AWG 2.0 через отдельный netifd-интерфейс;
4. RU-direct для VPN-сетей без утечки остального трафика в WAN;
5. локальный zapret для внешнего VPN transport flow до провайдера;
6. атомарное принятие ревизии подписки с автоматическим rollback;
7. восстановление после reboot, потери питания во время update и недоступного сайта;
8. app reset и отдельный подтверждаемый factory reset.

## 2. Что входит и не входит в первый этап

Входит:

- один клиент подписки на физический роутер, его отдельные token, `router_id` и
  хранилище двух ревизий;
- генерация конфигураций sing-box, AWG, маршрутизации, DNS и zapret;
- LuCI-страницы состояния, подписки, сетей, диагностики и reset;
- bootstrap/repair автоматически управляемых UCI-секций;
- IPv4-маршрутизация и fail-closed поведение.

Не входит:

- реализация сайта, биллинг, выдача токенов и админка сервера;
- произвольный редактор sing-box JSON, импорт URI или выполнение скриптов из
  подписки;
- multi-WAN, mesh, IPv6 managed-сетей, балансировка и автоматический выбор стратегии
  zapret; ручная ограниченная диагностика доступна отдельно;
- установка U-Boot-mod, изменение MTD-разметки и автообновление самой прошивки;
- обработка произвольного пользовательского трафика nfqws: zapret работает только
  по allowlist текущих outer VPN endpoint IP/port/WAN flow.

## 3. Сети и поведение

Адреса ниже — безопасные значения по умолчанию; bootstrap должен обнаружить коллизии
с существующими RFC1918-сетями и предложить другой свободный /24 до применения.

| UCI network | Подсеть по умолчанию | Назначение | Обычный egress | При отказе VPN |
| --- | --- | --- | --- | --- |
| `lan` | существующая | Администрирование | Как настроено пользователем | Не меняется |
| `rt_direct` | `192.168.10.0/24` | Полностью прямой доступ | WAN | WAN |
| `rt_vpn` | `192.168.20.0/24` | RU напрямую, остальное VPN | WAN или выбранный tunnel | Не-RU блокируется |
| `rt_vpn_zapret` | `192.168.30.0/24` | RU напрямую, остальное через VPN с zapret на outer transport | WAN или выбранный tunnel; outer flow проходит NFQUEUE | Не-RU блокируется |
| `rt_direct_zapret` | `192.168.40.0/24` | Прямой WAN с локальным nfqws | WAN + локальный zapret | WAN без скрытого VPN fallback |

Для каждой managed-сети создаются bridge, DHCPv4, firewall zone и один SSID на
выбранном пользователем radio. По умолчанию сети создаются disabled; LuCI включает
их после успешной проверки конфигурации. Если один SSID создаётся на двух диапазонах,
оба BSS подключаются к одному bridge.

Managed-секции получают стабильные имена и метки `router_managed=1`. Приложение не
удаляет и не переименовывает чужие секции. Повторный bootstrap идемпотентен: он
добавляет отсутствующие собственные секции, исправляет только принадлежащие ему поля
и показывает конфликт вместо захвата пользовательского объекта.

### Приоритет правил

Правила применяются сверху вниз:

1. loopback, адреса самого роутера, DHCP, DNS и локальные RFC1918/ULA — local/direct;
2. IP VPN endpoints, NTP, DNS upstream и сайт подписки — WAN direct, чтобы избежать
   рекурсивного заворачивания в туннель;
3. для `rt_vpn*` российские domain/CIDR rulesets — WAN direct;
4. остальной `rt_vpn` — назначенный VLESS, Hysteria2 или AWG;
5. остальной `rt_vpn_zapret` — выбранный VPN; только его outer packets к текущему
   endpoint IP/port через WAN проходят NFQUEUE;
6. `rt_direct_zapret` — WAN; это отдельный опциональный режим с собственной
   allowlist NFQUEUE, а не prerequisite для VPN+zapret;
7. отсутствие требуемого tunnel route — `unreachable`, а не main table.

На первом этапе RA и DHCPv6 в managed-сетях выключены, IPv6 forwarding из них
блокируется. Иначе клиент может обойти IPv4 policy через IPv6.

## 4. Компоненты на роутере

### `router-core`

Небольшой пакет на POSIX `ash` и `ucode`:

- UCI-конфигурация `/etc/config/autovpn` хранит выданный сервером неизменный
  `router_id`, локальные настройки и ссылку на secret file, но не generated runtime
  JSON; server registry связывает отдельный сменяемый token с этим `router_id`;
- updater получает manifest/artifacts, проверяет их и собирает revision directory;
- renderer преобразует только типизированные поля API в sing-box JSON, AWG UCI и
  nft/fw4 fragments;
- activator переключает текущую ревизию и координирует reload/rollback;
- procd-сервис, cron и hotplug hooks сериализуются одним lock;
- ubus API отдаёт LuCI только redacted status и запускает разрешённые действия.

Python, Node.js, `jq` и демон-база данных не используются. Для JSON применяются
`ucode`/`jsonfilter`/`jshn`; для HTTPS-клиента выбирается `curl` только если PoC
покажет, что стандартный `uclient-fetch` не позволяет надёжно получить статус,
ETag и response headers. Выбор фиксируется после сравнения размера финального образа.

### `luci-app-router`

Современный client-rendered LuCI JavaScript frontend вызывает объект
`luci.router` через ubus/rpcd. ACL разделены на read и write:

- read: status, active revision, redacted profiles, health и последние ошибки;
- write: save local settings, refresh, bootstrap/repair, enable/disable network,
  app reset;
- factory reset вынесен в отдельный метод, требует свежей LuCI-сессии и одноразового
  challenge.

Frontend не читает secret file и не получает приватные ключи. Ввод токена — поле
password: новое значение передаётся один раз и затем заменяется признаком `configured`.
Такой layout соответствует современному примеру LuCI и модели ACL rpcd.

### Data plane

- `sing-box-tiny`: один TUN instance для VLESS/Hysteria2, с generated config и
  `sing-box check` до активации;
- netifd + `amneziawg-tools` + `kmod-amneziawg`: отдельный `rt_awg` interface;
- firewall4/nftables: application-owned include, sets, marks и drop rules;
- dnsmasq-full: принудительный DNS managed-клиентов и population nft sets;
- `nfqws` + `kmod-nft-queue`: только минимальный бинарник и собственный procd/fw4
  wrapper, без полного дерева установщика zapret.

Сетевые marks, tables и nft names выделяются из документированного диапазона
проекта. Activator сначала проверяет отсутствие коллизий; молча использовать чужой
mark/table нельзя.

## 5. VLESS и Hysteria2

Подписка не передаёт готовый sing-box JSON. Renderer принимает allowlisted модель
из API и создаёт:

- TUN inbound для managed VPN-сетей;
- direct outbound;
- VLESS outbounds с TLS/Reality и только поддержанными transport fields;
- Hysteria2 outbounds с TLS и поддержанным текущей pinned-версией obfs;
- локальные binary SRS rulesets, скачанные как artifacts текущей ревизии;
- route rules по source subnet и ruleset с явным `final`;
- DNS interception для managed-сетей.

Перед activation обязательны `sing-box version`, feature compatibility и
`sing-box check -c <staged-config>`. `tls.insecure=true` контрактом запрещён.
Capabilities, появившиеся в sing-box 1.14+, нельзя принимать на сборке 1.12/1.13:
manifest содержит минимальную версию, а клиент отвергает несовместимую ревизию.

## 6. AmneziaWG 2.0

AWG остаётся отдельным network interface. Это даёт netifd lifecycle, понятный route
target и не связывает поддержку AWG с build tags `sing-box-tiny`.

Надёжность поставки — главный риск. Официальная инструкция Amnezia предупреждает,
что текущие Premium-конфиги AWG 3.1 не конвертируются в AWG 2.0 для OpenWrt.
Сторонний проект публикует APK AWG 2.0 для OpenWrt 25.12.5/mediatek-filogic, но
runtime-установка через `--allow-untrusted` недопустима. Требуемый путь:

1. pin конкретных source commits kernel module/tools/OpenWrt package definition;
2. аудит лицензий и build provenance;
3. собственная воспроизводимая сборка под точный OpenWrt kernel ABI;
4. подпись APK собственным feed key либо включение в ImageBuilder image;
5. capability test параметров AWG 2.0 (`S3`, `S4`, range `H1-H4`, `I1-I5`);
6. реальный handshake/data test с целевым сервером после каждого kernel update.

Если capability test не пройден, AWG-профили помечаются unsupported, но VLESS/HY2
продолжают работать. AWG endpoint всегда получает отдельное WAN-direct исключение.

## 7. Российские сайты напрямую

Один суффикс `.ru` недостаточен: российские сервисы используют другие TLD и CDN, а
часть иностранных сервисов географически резолвится в российские адреса. Поэтому
сервер подписки публикует ревизионные artifacts:

- `ru-domains.srs` для sing-box;
- нормализованный `ru-domains.txt` для dnsmasq nftset;
- `ru-ipv4.txt` с CIDR как дополнительный, но не единственный сигнал.

Сайт обязан фиксировать provenance, license и generated timestamp каждого ruleset.
Роутер не скачивает `latest` напрямую с GitHub: только same-origin artifacts из
manifest с размером и SHA-256. Это одновременно обеспечивает атомарность, повторяемость
и возможность удалить источник с неясной лицензией на серверной стороне.

Клиентский DoH/DoT может обойти domain-to-nftset mapping. Этап 1 перехватывает plain
DNS 53, предлагает блокировку известных DoT/DoH endpoints и показывает предупреждение,
но не обещает исчерпывающую классификацию произвольного шифрованного DNS. CIDR rules
смягчают, но не устраняют этот риск.

## 8. Zapret

### Outer VPN transport

Zapret применяется до VPN, на WAN-bound соединении роутера с VPN-сервером. Для
каждого активного кандидата renderer создаёт отдельную строго ограниченную NFQUEUE
allowlist: destination IP из endpoint set, точный destination port, transport
protocol и WAN egress. VLESS-кандидат обычно требует TCP/TLS-стратегии, Hysteria2 —
UDP/QUIC, AmneziaWG — UDP; ни одна стратегия не переносится между протоколами без
отдельного packet-level теста.

Пользовательский трафик после входа в туннель, server API/подписка, локальный DNS,
NTP и остальные WAN flows не совпадают с этими правилами и не передаются в nfqws.
Подписка может выбирать versioned strategy и массив безопасных аргументов `nfqws`,
но не shell строку. Renderer отвергает неизвестные флаги, path arguments и
`@config`.

Flow offloading способен обходить обработку NFQUEUE. Bootstrap сохраняет исходное
значение и отключает offloading только после явного согласия пользователя; app reset
возвращает сохранённое значение, если его после этого не изменял пользователь.

### Режимы и Auto

`vpn_zapret` использует тот же выбранный VPN-профиль, что и `vpn`, но включает
его outer transport в protocol-specific NFQUEUE. Для этого не нужна отдельная
серверная capability и не требуется обработка на VPN-сервере.

В режиме `auto` health sampler проверяет реальный HTTPS запрос через каждый
доступный вариант, а не ICMP ping. Кандидаты разделены по протоколу и zapret:
VLESS, VLESS+zapret, Hysteria2, Hysteria2+zapret, AmneziaWG и AmneziaWG+zapret.
Выбор учитывает задержку, hysteresis, failure threshold и cooldown; вариант без
zapret не считается эквивалентом варианта с zapret. Если нужный tunnel не поднят,
сети VPN остаются fail-closed. Поведение при сбое самого nfqws выбирается явно в
политике: либо не запускать VPN-кандидат, либо использовать его вариант без zapret;
никакого fallback пользовательского трафика в WAN нет.

### Реестр устройств и UCI design

Сервер хранит отдельную сущность `Router`: стабильный `router_id`, название,
назначенный VPN client, локальную policy, desired/applied revision, last-known
health, `last_seen` и историю безопасных diagnostics. У Router может быть несколько
credentials для ротации, но только активный credential принимает запросы. При отзыве
или замене credential сам Router и его состояние не создаются заново.

Каждый физический OpenWrt-роутер имеет собственный `/etc/autovpn/state` и root-only
credential file. Проектируемая UCI секция `autovpn.main` получает обязательный
`option router_id '<server-issued-id>'`; это не секрет и не заменяет Bearer token.
GET snapshot и PUT apply result аутентифицируются токеном, а server registry должен
возвращать только snapshot соответствующего Router. Snapshot также содержит
`router_id`, который контроллер сверяет с UCI перед staging. Несовпадение —
authentication/configuration error, staging не начинается.

Общая серверная VPN-ревизия может использоваться несколькими Router, но ETag
рассчитывается по индивидуальному snapshot: изменения ключей, режима Auto/forced,
zapret policy или назначенного client меняют revision именно затронутого роутера.
Отдельные apply reports (`APPLIED`, `FAILED`; rollback передаётся как безопасная
diagnostic причина `FAILED`) и `last_seen` позволяют сайту показать состояние каждого
устройства, не влияя на маршрутизацию остальных.

## 9. Маршрутизация: собственный тонкий слой вместо зависимости от PBR

PBR подтверждает рабочие идеи: nft sets, fw4 include, policy по source/destination и
поддержку WireGuard/tun interfaces. Но обязательной runtime-зависимостью он не выбран:

- документация PBR прямо говорит, что killswitch router mode не поддержан;
- domain policies требуют совместимых `dnsmasq-full`/nftset и DNS клиента через
  роутер;
- отдельная LuCI PBR-панель создаёт вторую точку управления;
- AGPL-3.0-or-later и набор зависимостей требуют отдельного footprint/license gate;
- интеграция address-less AWG и sing-box TUN должна быть проверена, а не предположена.

Вместо копирования PBR реализуется минимальная application-owned policy:

- фиксированные source subnets и четыре режима, без общего редактора правил;
- отдельные route tables для WAN, sing-box, AWG и unreachable;
- fw4 include генерируется целиком, проверяется через `fw4 check`/`nft -c` и затем
  заменяется;
- при остановке tunnel его table заменяется `unreachable`, поэтому main WAN не
  становится fallback;
- nft objects принадлежат только приложению и удаляются целиком на reset.

До реализации проводится PoC против PBR. Если PBR сможет обеспечить ту же
fail-closed семантику и уложится в образ, решение можно пересмотреть, подключив его
как отдельный пакет без копирования исходников.

## 10. Атомарная ревизия и rollback

Подробный HTTP-контракт описан в разделе
[`Router API v2`](../../README.md#router-api-v2). На роутере update идёт
так:

1. updater захватывает exclusive lock; cron, LuCI и boot не обновляют параллельно;
2. посылает `If-None-Match` для **последней успешно активированной** ревизии;
3. при `304` ничего не меняет; при `200` сохраняет manifest во временный каталог;
4. выполняет JSON Schema subset и полную semantic validation с лимитами;
5. последовательно скачивает same-origin artifacts с Bearer auth, сверяет size и
   SHA-256;
6. генерирует все конфиги в staging и выполняет dry-run/check каждого компонента;
7. записывает `/etc/router/revisions/<revision>.new`, делает fsync и rename;
8. атомарно переключает symlink `current` на новую immutable directory;
9. reload выполняется в порядке sing-box/AWG -> routing/DNS -> zapret; health probes
   должны завершиться за bounded timeout;
10. при любой ошибке symlink возвращается на предыдущую ревизию и повторно
    применяется старый runtime; ETag новой ревизии не становится current;
11. после успеха сохраняются ETag/revision и удаляются более старые ревизии, оставляя
    current + previous.

Файловое переключение атомарно. Несколько системных daemon reload не образуют ACID
транзакцию, поэтому bounded health-check и rollback являются обязательной частью
гарантии. После потери питания boot всегда применяет directory, на которую указывает
валидный `current`; каталоги `.new` игнорируются и удаляются.

## 11. Bootstrap, repair и reset

### Bootstrap/repair

Перед изменением UCI создаётся baseline snapshot только затрагиваемых файлов с
checksums. Через UCI batch создаются owned-секции network/dhcp/firewall/wireless,
после чего выполняются validation и reload. Для потенциально отрезающего сеть
изменения используется rollback timeout; административная `lan` и WAN не удаляются.

Repair сравнивает ожидаемые owned-секции с фактическими. Он не перезаписывает чужие
секции и не возвращает пользовательское изменение автоматически: конфликт показывается
в LuCI с diff-подобным описанием.

### App reset

После подтверждения:

- останавливает updater, sing-box instance, AWG и nfqws;
- удаляет только секции с `router_managed=1`, nft objects, revisions, token и state;
- возвращает сохранённые system options (например, flow offloading), только если их
  текущее значение всё ещё равно значению, установленному приложением;
- не трогает `lan`, WAN, пароль root, пользовательские SSID и сторонние пакеты.

### Factory reset

Это отдельная красная операция LuCI: показать точный эффект, потребовать ввод
`RESET`, одноразовый challenge и повторную аутентификацию. Затем вызывается штатный
механизм OpenWrt factory reset и reboot. Он удаляет **всю** overlay-конфигурацию,
включая доступ к Интернету, пароль, подписку и чужие настройки. Приложение никогда не
запускает его автоматически и не смешивает с app reset.

## 12. LuCI UX первого этапа

1. **Overview**: версия app/OpenWrt/sing-box/AWG/nfqws, current/available revision,
   tunnel health, состояние четырёх сетей, flash/RAM, last error.
2. **Subscription**: base URL, token replacement, manual refresh, ETag, timestamps;
   secrets redacted.
3. **Networks**: bootstrap/repair, SSID/radio/password, выбранный profile, явный
   индикатор RU-direct, outer-transport zapret и kill-switch.
4. **Diagnostics**: bounded probes по каждому path, redacted support bundle без
   ключей/tokens/full URLs.
5. **Reset**: раздельные app reset и factory reset.

LuCI не предоставляет shell textbox и не разрешает загружать произвольную
конфигурацию. Все destructive actions возвращают structured result через ubus.

## 13. Бюджет flash/RAM

Предыдущий ASU/ImageBuilder замер для 25.12.5:

- OpenWrt + LuCI: SquashFS около 4.77 МиБ;
- OpenWrt + `sing-box-tiny` + `kmod-nft-queue`: около 12.84 МиБ;
- sysupgrade второго варианта: 17,807,636 байт.

Это не итоговая сборка. Release gate требует один образ с точными версиями:

- `sing-box-tiny`, TUN dependencies;
- минимальный LuCI и `router-core`;
- JSON/HTTPS dependencies;
- `dnsmasq-full`, nft/NFQUEUE dependencies;
- pinned AWG tools/kmod/proto;
- только `nfqws`, нужные libraries и strategy data;
- factory defaults и две типичные subscription revisions.

Gate: image помещается в stock UBI с резервом для overlay, две ревизии не заполняют
flash, update staging не приводит к OOM на 256 МиБ RAM. Считать суммы размеров APK
недостаточно — измеряется реальный SquashFS и runtime free space.

## 14. Исследованные решения и переиспользование

| Проект | Полезное | Решение | Лицензия/ограничение |
| --- | --- | --- | --- |
| Podkop | Российский сценарий, generated sing-box, lists, LuCI | Переиспользовать идеи UX и тестовые сценарии; не брать runtime/code | GPL-2.0-or-later; beta, меняет dnsmasq/sing-box, требует `curl`/`jq`/full sing-box |
| HomeProxy | Современный LuCI, ucode generator, procd/ujail | Брать только OpenWrt-паттерны и сверять поведение; не копировать код | GPL-2.0-only; China-centric defaults, зависит от full sing-box |
| PBR | nft sets, fw4 include, policy routing | Reference + PoC; не обязательная dependency этапа 1 | AGPL-3.0-or-later; documented killswitch/DNS caveats |
| sing-box | VLESS/HY2, TUN, SRS и config check | Переиспользовать официальный `sing-box-tiny` package/API | GPL-3.0-or-later; feature set зависит от build/version |
| zapret | `nfqws`, OpenWrt nft/firewall4 опыт | Переиспользовать pinned binary/source с собственным узким wrapper | MIT; стратегия ISP-specific, flow offload conflict |
| AmneziaWG | kernel tunnel и tools | Собирать pinned AWG 2.0 отдельно от sing-box | GPL components; kernel ABI и provenance внешних APK |

Никаких файлов из этих проектов в репозитории сейчас нет. Перед будущим копированием
даже небольшого фрагмента требуется file-level license review и сохранение notices.

## 15. Открытые риски и обязательные проверки

### Блокирующие до реализации

- точный final image size со всеми пакетами и двумя ревизиями;
- AWG 2.0 source/build provenance и совместимость с 25.12.x kernel ABI;
- sing-box-tiny содержит все build tags для выбранных VLESS/Reality/Hysteria2 полей;
- TUN + source policy не создаёт routing loop для DNS, subscription и endpoints;
- local nft layer обеспечивает kill-switch при crash/reload каждого daemon;
- protocol-specific nfqws стратегия проверена у целевого ISP для каждого outer flow
  (VLESS/TCP, Hysteria2/QUIC и AWG/UDP) и выключенного offload;
- router registry корректно изолирует snapshot, token rotation, ETag и apply reports
  нескольких физических устройств.

### Регрессионная матрица

- clean install, sysupgrade keep-settings и cold boot;
- 200, 304, timeout, TLS error, 401, 429, malformed JSON, oversized body/artifact,
  hash mismatch, incompatible version;
- потеря питания на каждом шаге update;
- отказ sing-box, AWG, dnsmasq, firewall и nfqws;
- смена профиля, удаление активного профиля и expired manifest;
- RU/non-RU DNS+CIDR cases, DoH client, IPv6 leak test;
- изоляция клиентов между сетями и доступ к LuCI только из admin `lan`;
- app reset с сохранением чужих UCI-секций;
- factory reset только после явного подтверждения и восстановление через штатный
  OpenWrt onboarding.

## 16. Ссылки на первичные источники

- [OpenWrt 25.12 release notes](https://openwrt.org/releases/25.12/notes-25.12.0)
- [Cudy WR3000S v1 device page](https://openwrt.org/toh/cudy/wr3000s_v1)
- [OpenWrt sing-box package](https://github.com/openwrt/packages/blob/master/net/sing-box/Makefile)
- [sing-box VLESS](https://sing-box.sagernet.org/configuration/outbound/vless/)
  и [Hysteria2](https://sing-box.sagernet.org/configuration/outbound/hysteria2/)
- [LuCI JavaScript example](https://github.com/openwrt/luci/tree/master/applications/luci-app-example)
  и [rpcd ucode example](https://github.com/openwrt/rpcd/blob/master/examples/ucode/example-plugin.uc)
- [Podkop package](https://github.com/itdoginfo/podkop/blob/main/podkop/Makefile)
- [HomeProxy package](https://github.com/immortalwrt/homeproxy/blob/master/Makefile)
- [PBR documentation](https://docs.mossdef.org/pbr/) и
  [OpenWrt package](https://github.com/openwrt/packages/blob/master/net/pbr/Makefile)
- [zapret documentation](https://github.com/bol-van/zapret/blob/master/docs/readme.en.md)
  и [license](https://github.com/bol-van/zapret/blob/master/docs/LICENSE.txt)
- [AmneziaWG OpenWrt releases](https://github.com/Slava-Shchipunov/awg-openwrt/releases),
  [kernel module](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module),
  [tools](https://github.com/amnezia-vpn/amneziawg-tools)
