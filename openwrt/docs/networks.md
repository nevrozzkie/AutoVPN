# Автосоздание WPA2 SSID

В LuCI **Services → AutoVPN → Settings** задаются:

- `Base Wi-Fi name`: собственное имя без суффиксов, например `Общага`;
- `WPA2-PSK password`: один собственный пароль для всех управляемых SSID.

Пароля по умолчанию нет. Шифрование — WPA2-PSK/AES (`psk2+ccmp`), не WPA/WPA3 mixed.
Для пароля допустимы 8–63 печатных ASCII-символа или ровно 64 шестнадцатеричных символа.
Базовое имя ограничено 21 **байтом UTF-8**, чтобы самый длинный суффикс помещался
в лимит SSID 32 байта. Например, русская буква занимает два байта.

## Какие сети создаются

На каждом уже включённом radio с `band=2g` или `band=5g` используются одинаковые
имена и пароль; клиент выбирает диапазон самостоятельно. Отключённые radio,
канал, страна и существующие SSID не меняются.

| SSID для базы `Общага` | Мост / IPv4 | Состояние |
| --- | --- | --- |
| `Общага` | `br-avpnd`, `192.168.29.1/24` | Прямой интернет через WAN |
| `Общага-VPN` | `br-avpn`, `192.168.30.1/24` | VPN, без прямого выхода при сбое |
| `Общага-ZAPRET` | `br-avpndz`, `192.168.31.1/24` | SSID и DHCP выключены, forwarding заблокирован |
| `Общага-VPN-ZAPRET` | `br-avpnz`, `192.168.32.1/24` | SSID и DHCP выключены, forwarding заблокирован |

Последние две сети — подготовленная конфигурация, **не реализованный zapret**.
Не включать их вручную: backend nfqws будет отдельным этапом.

Мосты не содержат физических LAN-портов. Создаются свои UCI interface, DHCPv4 и
firewall zone; RA/DHCPv6 отключены, IPv6 блокируется. Для AP включён `isolate=1`
(изоляция клиентов в пределах AP; это не гарантия полной L2-изоляции между radio).
Администрирование LuCI/SSH остаётся в старой управленческой LAN/SSID.
Прямая сеть допускает DHCP/DNS к роутеру и IPv4 forwarding в zone `wan`, но не в LAN.

## Применение и откат

1. Подключиться к роутеру проводом либо сохранить доступ через исходную management-сеть.
   Включить нужные radio и выключить software/hardware flow offload.
2. В Settings указать имя/пароль, выполнить **Save & Apply**. Пароль хранится в
   `/etc/config/autovpn` и после создания — в стандартном `/etc/config/wireless`;
   он не отправляется в AutoVPN API, не передаётся через argv и не возвращается в status RPC.
3. На странице **Networks** нажать **Create / update SSIDs**. Runtime VPN на время
   закрывается и останавливается. Перезапуск radio может кратко оборвать старый Wi-Fi.
4. Проверить новые сети. Вернуться в Networks через исходную management-сеть и
   нажать **Keep these networks** в течение трёх минут. Проверка VPN выполняется
   следующим шагом, здесь подтверждаются именно сети/доступность AP.
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

## Проверка на устройстве обязательна

Host-тесты исполняют planner, transaction и helper с моделью UCI/файловой системы.
Они не заменяют OpenWrt SDK build, native ucode, реальный netifd/fw4/hostapd и
испытания на WR3000S v1: WPA2 association на обоих radio, DHCP/DNS, запрет LAN/IPv6,
падение VPN, отмена подтверждения, reboot и loss-of-power между UCI writes.
Пакет пока нельзя считать проверенным готовым образом прошивки.

Форматы сверены с upstream [UCI cursor](https://ucode.mein.io/module-uci.cursor.html),
[netifd init](https://github.com/openwrt/openwrt/blob/openwrt-25.12/package/network/config/netifd/files/etc/init.d/network),
[hostapd](https://github.com/openwrt/openwrt/blob/openwrt-25.12/package/network/config/wifi-scripts/files/lib/netifd/hostapd.sh).
