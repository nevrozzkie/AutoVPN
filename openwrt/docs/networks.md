# Автосоздание WPA2 SSID

Пользовательский режим создаёт базовый SSID, обычный VPN-SSID `-в` и
«негативный VPN» SSID `-нв`. Legacy `-з` и `-вз` могут встречаться в старых
UCI/runtime данных, но скрыты из текущего LuCI и не активируются этим сценарием.

При первой установке имя и пароль запрашивает терминальный мастер. В дальнейшем
они меняются в LuCI **Services → AutoVPN → Settings**, затем применяются через Networks:

- `Base Wi-Fi name`: собственное имя без суффиксов, например `Общага`;
- `WPA2-PSK password`: один собственный пароль для всех управляемых SSID.

Пароля по умолчанию нет. Шифрование — WPA2-PSK/AES (`psk2+ccmp`), не WPA/WPA3 mixed.
Для пароля допустимы 8–63 печатных ASCII-символа или ровно 64 шестнадцатеричных символа.
Базовое имя ограничено 27 **байтами UTF-8**, чтобы суффикс `-нв` помещался
в лимит SSID 32 байта. Например, русская буква занимает два байта.

## Какие сети создаются

На radio с `band=2g` и `band=5g` используются одинаковые имена и пароль; клиент
выбирает диапазон самостоятельно. DAWN/usteer и активный band steering не ставятся.
Первоначальный bootstrap требует оба диапазона, включает отключённые radio и
отключает только точно распознанный открытый заводской `default_radioN` с SSID
`OpenWrt`. Неизвестный активный профиль на выключенном radio приводит к отказу.
Каналы, страна, PPPoE и существующая LAN сохраняются. Обычное обновление сетей
через Networks работает только с уже включёнными radio.

| SSID для базы `Общага` | Мост / IPv4 | Состояние |
| --- | --- | --- |
| `Общага` | Существующая `lan` (на штатном роутере `br-lan`, `192.168.1.1/24`) | Прямой интернет и доступ к админке |
| `Общага-в` | VPN-мост и LAN4 | «всё кроме прямых исключений» через выбранный VPN; при сбое VPN forwarding закрыт |
| `Общага-нв` | отдельный IPv4-мост и LAN3 | «только выбранные адреса/домены» через тот же VPN, всё остальное напрямую |

Installer создаёт три WPA2 SSID. Только `Общага` присоединена к существующей LAN
и даёт обычный доступ к LuCI/SSH. `Общага-в` и LAN4 находятся в одной VPN-сети,
получают адреса из одного DHCP и используют текущий выбранный VPN-профиль.
`Общага-нв` и LAN3 образуют другую сеть с отдельным DHCP: в ней только выбранные
в LuCI назначения идут через **тот же** текущий VPN-профиль, а всё невыбранное
остаётся прямым WAN-трафиком. Пока общий VPN runtime недоступен, fail-closed guard
закрывает всю `-нв`, поэтому выбранные назначения не могут случайно уйти напрямую.

Для двух VPN-политик создаются отдельные мосты, UCI interface, DHCPv4 и firewall
zone; RA/DHCPv6 отключены, IPv6 блокируется. LAN3 атомарно переносится в `-нв`,
LAN4 — в `-в`; LAN1/LAN2 остаются в основной сети. При `bridge-vlan`, чужом использовании
этих портов или неоднозначной DSA-топологии AutoVPN отказывается менять конфиг.
Основная сеть использует существующие LAN DHCP/firewall/IPv6 и ничего в них не меняет.
Для AP включён `isolate=1`
(изоляция клиентов в пределах AP; это не гарантия полной L2-изоляции между radio).
Администрирование LuCI/SSH доступно через основную сеть по правилам LAN.
Прежние установки без `wifi.primary_lan=1` сохраняют старую изолированную прямую
сеть `br-avpnd`, `192.168.29.1/24` без доступа в LAN/LuCI/SSH. Автомиграции нет.

## Первая установка

Installer до установки пакетов проверяет наличие управляющего терминала.
Затем установленный `install-wifi` спрашивает имя и дважды пароль без эха;
секрет передаётся `wifi-bootstrap` только через stdin, не argv/env.
После применения нужно проверить основную сеть другим устройством и ответить
`yes` за 180 секунд. Только подтверждённая неизменённая транзакция получает
`wifi.bootstrap_completed=1`. При отказе/обрыве ввода pending-транзакция откатится;
после точного отката начальную настройку можно повторить. Если journal уже подтверждён,
но отметка завершения не сохранилась, повтор installer восстанавливает только эту
отметку после проверки неизменности конфигов, без нового пароля и перенастройки сети.
Установка пакетов при
этом не отменяется. В LuCI Setup привязка сайта использует уже подтверждённый Wi-Fi,
пароль повторно вводить не требуется. Подтверждение сетей не означает проверку VPN.

## Применение и откат

1. Подключиться к роутеру по основной Wi-Fi либо через LAN1/LAN2: LAN3 будет
   перенесён в `-нв`, LAN4 — в `-в`; оба перестанут давать доступ к LuCI/SSH.
   При обычном обновлении включить нужные radio; software/hardware flow offload должен быть выключен.
2. В Settings указать имя/пароль, выполнить **Save & Apply**. Пароль хранится в
   `/etc/config/autovpn` и после создания — в стандартном `/etc/config/wireless`;
   он не отправляется в AutoVPN API, не передаётся через argv и не возвращается в status RPC.
3. На странице **Networks** нажать **Create / update SSIDs**. Runtime VPN на время
   закрывается и останавливается. Перезапуск radio может кратко оборвать старый Wi-Fi.
4. Проверить новые сети. Вернуться в Networks через исходную management-сеть и
   (или основную LAN-сеть после bootstrap) и нажать **Keep these networks** в течение трёх минут. Проверяется также,
   что LAN3 и LAN4 действительно стали участниками своих управляемых мостов;
   наличие кабеля не требуется.
5. В Overview выполнить **Refresh snapshot** либо **Apply saved VPN settings**,
   чтобы запустить VPN. После успешной проверки включить автоматический refresh.

Если подтверждение не пришло, отдельный procd watchdog восстанавливает исходные
`network`, `wireless`, `dhcp`, `firewall`. Срок считается по `/proc/uptime`, поэтому
коррекция NTP не продлевает его. Watchdog проверяет срок каждые несколько секунд и
использует общий lock с контроллером; уже выполняющаяся операция может задержать откат.
Незавершённая транзакция при следующей загрузке откатывается до старта netifd.
Пока журнал находится в `pending` или `rollback_conflict`, runtime не открывает
VPN даже при фоновом refresh. Перед живыми UCI-правками журнал синхронизируется
на диск; восстановленные конфиги синхронизируются до завершения rollback.

Перед изменениями проверяются совпадение имён, SSID, IP-подсетей и WAN-маршрутов,
наличие маскарадинга в zone `wan` и отсутствие незакоммиченных системных UCI-правок.
Нестандартную схему с другой WAN zone следует подготовить вручную. Одновременно
редактировать системные network/firewall/wireless/dhcp в другой LuCI-сессии нельзя.

Повторное создание обновляет только секции `avpn_*` с маркером `autovpn_owner=ssid-v1`;
одноимённая чужая секция приводит к отказу, а не захвату её настроек. Сети, созданные
вручную для runtime 0.4 без маркера, не присваиваются автоматически.

Журнал `/etc/autovpn/networks/journal.json` содержит private before/after backups.
Если конфиг после применения изменил кто-то ещё, откат **не перезаписывает чужие
изменения**, сохраняет backup и возвращает `network_rollback_requires_attention`.
Такой конфликт требует ручного разбора через management LAN; автоматическое
создание новых сетей до его устранения запрещено.

CLI: `autovpnctl network-setup`, `autovpnctl network-status`,
`autovpnctl network-confirm TRANSACTION_ID`. Пароль передавать через CLI не нужно.

## Негативный VPN: что выбирается в LuCI

В **Services → AutoVPN → Settings** для `-нв` задаётся список IPv4-адресов или
CIDR, которые должны идти через VPN, и набор доменных переключателей. Домены
сопоставляются по DNS и протоколам внутри локального sing-box; статические IP общих CDN
намеренно не поставляются. Все остальные IPv4-назначения `-нв` используют WAN напрямую.
Это не режим «весь интернет через VPN» и не замена правилам `-в`.

Состав встроенных переключателей прозрачен и фиксирован в версии пакета:

| Переключатель | Домены, направляемые через VPN |
| --- | --- |
| Telegram + API | `t.me`, `telegram.org`, `telegram.me`, `telegram.dog`, `tdesktop.com`, `telegram-cdn.org`, `api.telegram.org`, `core.telegram.org`, `web.telegram.org`, `desktop.telegram.org`, `updates.tdesktop.com`; дополнительно `149.154.160.0/20`, `91.108.4.0/22` |
| YouTube | `youtube.com`, `youtu.be`, `youtube-nocookie.com`, `googlevideo.com`, `ytimg.com`, `youtubei.googleapis.com`, `youtube.googleapis.com`, `yt3.ggpht.com` |
| Instagram | `instagram.com`, `cdninstagram.com`, `i.instagram.com`, `graph.instagram.com`, `api.instagram.com`, `l.instagram.com` |
| X / Twitter | `x.com`, `twitter.com`, `t.co`, `twimg.com`, `api.x.com`, `api.twitter.com` |
| ChatGPT | `chatgpt.com`, `chat.openai.com`, `openai.com`, `auth.openai.com`, `platform.openai.com`, `oaistatic.com`, `oaiusercontent.com` |
| Claude | `claude.ai`, `anthropic.com`, `console.anthropic.com`, `api.anthropic.com`, `anthropic-static.com` |
| Остальные популярные внешние сервисы | Discord: `discord.com`, `discord.gg`, `discordapp.com`, `discordapp.net`, `discord.media`, `discordstatus.com`; Facebook/Messenger: `facebook.com`, `fb.com`, `fb.me`, `fbcdn.net`, `messenger.com`; LinkedIn: `linkedin.com`, `licdn.com`; Reddit: `reddit.com`, `redd.it`, `redditmedia.com`, `redditstatic.com`; TikTok: `tiktok.com`, `tiktokv.com`, `tiktokcdn.com`; Signal: `signal.org`, `signal.art`, `signal.me`, `whispersystems.org`; Twitch: `twitch.tv`, `ttvnw.net`, `jtvnw.net`; Viber: `viber.com`, `viber.me`; также `soundcloud.com`, `clubhouse.com`, `patreon.com` |

Этот последний переключатель — перечисленный curated-набор, а не полный реестр
ограниченных ресурсов РФ и не гарантия доступности. Общие домены, например
`google.com`, общий `googleapis.com` и `challenges.cloudflare.com`, намеренно
не включены: они обслуживают несвязанные сайты и могли бы без необходимости
увести их трафик в VPN. ECH, сторонний DoH, IP без доменного имени и изменение
инфраструктуры сервиса могут не позволить сопоставить трафик с доменным правилом;
для этого остаётся ручной IPv4/CIDR-список. Два Telegram CIDR являются
дополнительными опубликованными диапазонами и могут измениться. IPv6 в `-нв` не
используется и не получает прямого обходного маршрута. При недоступном runtime
закрывается вся сеть `-нв`: это не позволяет выбранному трафику случайно уйти напрямую.

## Проверка на устройстве обязательна

Host-тесты исполняют planner, transaction и helper с моделью UCI/файловой системы.
Они не заменяют OpenWrt SDK build, native ucode, реальный netifd/fw4/hostapd и
испытания на WR3000S v1: WPA2 association на обоих radio, DHCP/DNS, запрет LAN/IPv6,
падение VPN, отмена подтверждения, reboot и loss-of-power между UCI writes.
Пакет пока нельзя считать проверенным готовым образом прошивки.

Форматы сверены с upstream [UCI cursor](https://ucode.mein.io/module-uci.cursor.html),
[netifd init](https://github.com/openwrt/openwrt/blob/openwrt-25.12/package/network/config/netifd/files/etc/init.d/network),
[hostapd](https://github.com/openwrt/openwrt/blob/openwrt-25.12/package/network/config/wifi-scripts/files/lib/netifd/hostapd.sh).
