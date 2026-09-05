# Runtime 0.5: sing-box, AmneziaWG и отдельная VPN-сеть

Реализованы VLESS REALITY, Hysteria2 и опциональный kernel AmneziaWG. Режим `auto` использует
sing-box URLTest: HTTPS-проверки раз в минуту, порог переключения 50 мс, без
переключения на direct при недоступности VPN. Это пока встроенная логика sing-box;
отдельная state machine с 3 ошибками / 2 успехами и cooldown ещё не подключена к
периодическому выбору. Детали AmneziaWG — [отдельно](amnezia.md); nfqws пока не запускается.

## Сеть и владение ресурсами

В этой версии одна VPN-сеть обслуживает оба диапазона Wi-Fi. Она создаётся
автоматически на странице LuCI Networks: [первый запуск SSID](networks.md).
Фиксированные ресурсы пакета:

| Ресурс | Значение |
| --- | --- |
| VPN bridge / подсеть | `br-avpn`, `192.168.30.0/24` |
| TUN | `avpn0`, `172.30.255.1/30` |
| DNS interception | TCP/UDP 53 → `172.30.255.2` → sing-box DNS через VPN |
| Таблица маршрутов | `20191`, default через `avpn0` |
| `ip rule` | priority `20191`: iif br-avpn → table 20191; `20192`: unreachable |
| nft table | `inet autovpn`, marker chain `ownership_autovpn_v1` |
| SOCKS для проверки | только `127.0.0.1:1088` |
| Зарезервированная VPN+zapret-сеть | `br-avpnz`, полностью заблокирована до реализации |

Маршрутизация по входному интерфейсу не меняет default route роутера. Соединения
VLESS/Hysteria2 и direct-исключений привязаны к указанному WAN device. DNS, API и обычная LAN продолжают
использовать свои маршруты. Пользовательский DNS из VPN-сети перехватывается, включая
запросы к адресу самого роутера. DNS внутри sing-box имеет явный VPN detour.

Из `br-avpn` разрешён только IPv4 с адресом источника из `192.168.30.0/24` в TUN.
IPv6 и пересылка в WAN/LAN блокируются. Доступ к самому роутеру ограничен DHCPv4;
администрировать устройство нужно через существующую LAN. Прямые исключения
исполняются внутри sing-box, а не дополнительными kernel-маршрутами в WAN.
Доменные исключения используют DNS reverse mapping и sniff; ECH, сторонний DoH и
сайты на общих IP ограничивают точность сопоставления. Суффикс `ru` не равен полному
списку российских сайтов. Частные адреса блокируются перед исключениями.

Статические fw4 includes сохраняют ограничения при firewall reload. Отдельный nft
guard закрывает сеть во время активации и при сбоях. При исчезновении TUN правило
unreachable препятствует переходу в основную таблицу. Flow offloading должен быть
выключен: runtime отказывается запускаться при включённом software/hardware offload.
Пакет не меняет чужие правила/таблицы и отказывает при коллизии своих идентификаторов.

## Настройки через LuCI

`Services → AutoVPN → Settings` сохраняет несекретные параметры в UCI:

```uci
config runtime 'runtime'
	option selection 'auto'
	option wan_device 'pppoe-wan'
	option dns_server '1.1.1.1'
	list direct_domains 'ru'
	list direct_domains 'xn--p1ai'
	list direct_domains 'example.org'
	list direct_cidrs '203.0.113.0/24'
```

`wan_device` — фактический Linux l3_device WAN, не обязательно имя UCI-интерфейса
`wan`. Его можно прочитать командой `ubus call network.interface.wan status`.
Пример выше не следует копировать с `pppoe-wan`, если у провайдера DHCP на другом
устройстве. Endpoint VPN в текущем runtime должен быть IPv4-адресом.

После сохранения нажать **Apply saved VPN settings** на Overview либо выполнить
`autovpnctl apply-policy`. Это отдельная транзакция даже при неизменном ETag подписки.
Сбой проверки восстанавливает предыдущую конфигурацию вместе с её исключениями;
сохранённые UCI-поля остаются желаемыми настройками, поэтому их можно исправить и
повторить применение. Обычный refresh восстанавливает последнюю применённую policy.

Текущий AutoVPN может отдавать Hysteria2 с `tls.insecure=true`. В режиме
`hysteria_tls_mode=subscription` (по умолчанию) runtime сохраняет эту настройку.
Шифрование TLS остаётся, но сертификат VPN-сервера не проверяется. Режим `strict`
исключает такой кандидат из auto и возвращает `hysteria_tls_unverified` при ручном выборе.
Старые сохранённые bundles 0.4 сохраняют прежнюю строгую политику до явного apply.

## Транзакция и перезагрузка

`prepare` проверяет snapshot/policy, пишет prepared bundle и запускает
`sing-box check`. `activate` сохраняет previous bundle, закрывает guard, переключает
current, запускает отдельный procd-сервис и ставит маршруты. `verify` требует два
успешных HTTPS 204 через SOCKS-вход, всегда направленный в VPN, и лишь затем открывает
forward в TUN. Проверка через SOCKS подтверждает сам VPN, но не заменяет проверку
прохождения пакетов клиента через Linux TUN/firewall.

Bundle содержит identity роутера, ETag, номер попытки, policy и сгенерированную
конфигурацию. Перед использованием конфигурация заново рендерится и сравнивается с
bundle. `rollback` выбирает current/previous, совпадающий с applied journal, даже
если power loss случился после переключения файлов и до commit журнала.
Отсутствующий/повреждённый backup приводит к fail-closed.

В `/etc/autovpn/runtime` хранятся current, previous, prepared и файлы для запуска;
каталог имеет `0700`, файлы `0600`. Идентичные generated JSON не переписываются.
Полные конфиги, секреты и сообщения sing-box не попадают в логи или RPC.
Procd поднимает упавший процесс, а poll восстанавливает committed runtime **до**
обращения к сайту. HTTP 304 и недоступный сайт не должны оставлять роутер без
восстановленного туннеля. `autovpnctl stop` закрывает guard и останавливает процесс;
при включённом polling следующий цикл восстановит VPN. Для постоянной остановки
нужно остановить/отключить сервис `autovpn`.

## Первый запуск на устройстве

1. Собрать пакет в OpenWrt SDK, проверить ucode и установить вместе с зависимостями.
2. Проверить, что перечисленные выше имена, подсети, порт и таблица свободны.
3. Выключить software/hardware flow offloading. Включить нужные radio в Network → Wireless,
   сохранив административную LAN/Wi-Fi отдельно. В AutoVPN → Settings задать базовый
   SSID и пароль, Save & Apply; в Networks создать и подтвердить сети по [инструкции](networks.md).
4. Проверить `nft list chain inet fw4 forward`: должны присутствовать правила `br-avpn`.
5. На странице AutoVPN создать отдельного VPN-клиента и роутер, синхронизировать
   клиента на VPS. Записать Router ID и token командами из основного README.
6. В LuCI задать URL сайта, Router ID и WAN device. Выполнить ручной refresh.
   При успешной проверке включить automatic refresh и сервис `autovpn`.
7. С клиента VPN-сети проверить внешний IP, direct-исключения, DNS и блокировку IPv6.
   Остановить sing-box, перезапустить firewall и роутер: прямого выхода в WAN быть
   не должно, административная LAN должна оставаться доступной.

## Проверки разработки

```sh
sh scripts/check.sh
AUTOVPN_SING_BOX=/path/to/sing-box sh scripts/check.sh
```

Второй вариант дополнительно проверяет три сгенерированные конфигурации настоящим
sing-box. Проверено на host-бинарнике **1.13.18**. Shell-тесты запускают настоящий
adapter с подменёнными системными командами; helper-тесты исполняют его исходник с
моделью файловой системы и uci. Они проверяют порядок закрытия/активации, отказ при
ошибке HTTPS и коллизиях, а также восстановление прежней policy после сбоя.
Это не исполнение nft/ip/procd на Linux. До установки всё ещё нужны `ucode -c`,
SDK build, проверка kernel rules и packet-level тесты на WR3000S v1.

Формат конфигурации сверялся с upstream: [TUN](https://sing-box.sagernet.org/configuration/inbound/tun/),
[URLTest](https://sing-box.sagernet.org/configuration/outbound/urltest/),
[route actions](https://sing-box.sagernet.org/configuration/route/rule_action/),
[OpenWrt package](https://github.com/openwrt/packages/blob/openwrt-25.12/net/sing-box/Makefile).
