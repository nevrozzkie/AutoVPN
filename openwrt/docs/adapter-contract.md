# Adapter contract

Controller запускает adapter только массивом argv, без shell interpolation. Ни
credential, ни snapshot/apply-result body в argv или environment не передаются.
stdout содержит один JSON object с безопасной метаинформацией размером не более
4096 bytes; stderr не содержит URL, headers, body или секретов. Packaged adapter
подавляет transport/helper stderr и выдаёт только allowlisted `code`.

## HTTP adapter

Путь задаётся UCI option `autovpn.main.http_adapter`.

Production transport — официальный OpenWrt package `curl` (`+curl`, который
подтягивает `libcurl`) и базовые BusyBox `head`/`mkfifo` (`+busybox`); CA roots
задаются явной зависимостью `+ca-bundle`. `uclient-fetch` не выбран: Bearer пришлось бы
передавать custom-header CLI-аргументом. Curl запускается только как
`curl --disable --config <private-file>`: credential и headers находятся в
root-only temporary config, а PUT payload — в отдельном root-only body file.
Все temporary files имеют mode `0600`, directory — `0700`, и удаляются trap на
success, validation error, transport error и signal.

Body и header dump curl направляются в разные private FIFO. Два reader-процесса
BusyBox `head -c 16385` одновременно копируют их в bounded files. После 16385-го
byte reader закрывает FIFO; следующая запись chunked/unknown-length ответа получает
broken pipe, а adapter отвергает capture. Поэтому `max-filesize` остаётся только
defense in depth: ни body, ни header file физически не могут вырасти больше 16385
bytes. Dummy FIFO writers закрываются сразу после curl и гарантируют EOF без
deadlock при DNS/TLS error, `304` или отсутствующем body. Trap завершает curl и оба
reader-процесса и удаляет FIFO/files на signal.

`base_url` принимается только в форме `https://authority[/prefix]`: без userinfo,
query, fragment, whitespace, percent escapes, empty/dot path segments или doubled
slash. Trailing slash удаляются, затем строго добавляется `/api/v2/router`.
DNS/IPv4 authority и bracketed IPv6 могут иметь port 1..65535. TLS peer/hostname
verification включена, CA bundle зафиксирован как
`/etc/ssl/certs/ca-certificates.crt`, redirects выключены (`no-location`), proxy
environment игнорируется (`noproxy = "*"`), protocol allowlist равен `https`.

### Fetch

```text
http-adapter fetch <base_url> <credential_file> <state_dir> <applied_etag-or-empty> <connect_timeout> <request_timeout>
```

Adapter обязан:

- прочитать Bearer credential непосредственно из root-only файла;
- выполнить `GET <base_url>/api/v2/router/snapshot`;
- передать `Authorization: Bearer ...` и `Accept: application/json`; заголовок
  `If-None-Match` отправлять только если `<applied_etag-or-empty>` содержит
  непустой валидный strong ETag;
- иметь connect timeout 1..60 s, total timeout 2..120 s (`total >= connect`),
  response body hard limit 16384 bytes и accepted header dump limit 16384 bytes;
- не следовать никаким redirects;
- не кэшировать body и не писать его/headers в stdout, stderr или log;
- вернуть один из объектов:

```json
{"ok":true,"status":304,"etag":"\"0123...cdef\""}
```

```json
{"ok":true,"status":200,"etag":"\"0123...cdef\"","response_file":"/etc/autovpn/state/http-response.1234"}
```

`200` без единственного корректного strong ETag или JSON object является ошибкой.
Validated raw body передаётся через уникальный root-only handoff в `state_dir`.
Controller принимает только собственный basename `http-response.<pid>`, читает
максимум 16385 bytes, всегда удаляет файл и лишь затем запускает полный snapshot
validator. При `304` body обязан быть пустым, response ETag обязан точно совпасть с
непустым request ETag, handoff не создаётся и journal не меняется. Поэтому
unsolicited `304` на первом запросе отвергается.

### Apply result

```text
http-adapter put-result <base_url> <credential_file> <idempotency_key> <etag> <journal_file> <connect_timeout> <request_timeout>
```

Adapter читает `pending_report.body` из root-only journal и выполняет:

```text
PUT <base_url>/api/v2/router/apply-results/<idempotency_key>
Authorization: Bearer <credential>
Content-Type: application/json
If-Match: <etag>
```

Helper перечитывает `pending_report`, требует exact keys
`idempotency_key`/`etag`/`body`, сверяет оба argv значения и валидирует body по
server schema v1 до создания request. Journal читается отдельным bounded parser с
лимитом 256 KiB; сетевые request/response body сохраняют строгий предел 16 KiB.
Body сериализуется без добавления полей.
При transport/5xx failure запись остаётся в journal и следующий вызов повторяет
тот же key, ETag и body.

Текущий server contract возвращает только HTTP 200 и JSON object с exact fields
`schema_version`, `result_id`, `idempotency_key`, `revision`, `snapshot_sha256`,
`outcome`, `active_profile`, `accepted_at`, `replayed`. Adapter сверяет identity
поля ответа с отправленным body, key и ETag. Успех:

```json
{"ok":true,"status":200,"replayed":false}
```

Ответ с `replayed=true` также успех. `201`, `409`, `412` и malformed response нельзя
маскировать как успех.

Curl exit codes преобразуются только в фиксированные `dns_failed`,
`connect_failed`, `request_timeout`, `tls_failed`, `tls_verification_failed`,
`response_too_large` или `transport_failed`. Известные HTTP errors преобразуются
в `http_<status>` только для allowlist; остальные — `unexpected_http_status`.

## Runtime adapter

Путь задаётся UCI option `autovpn.main.runtime_adapter`. Реализованный adapter владеет
generated sing-box, своими nft/ip rules и отдельным procd-сервисом. netifd/AWG/nfqws
пока не реализованы. Полный контракт и ограничения: [runtime.md](runtime.md).

```text
runtime-adapter capabilities
runtime-adapter prepare <journal_file>
runtime-adapter activate <journal_file>
runtime-adapter verify <journal_file>
runtime-adapter rollback <journal_file>
runtime-adapter fail-closed
runtime-adapter restore <journal_file>
runtime-adapter status <journal_file>
```

- `prepare` читает `desired.snapshot`, рендерит в staging и выполняет offline
  validators. Active runtime не меняется.
- `activate` переключает staged runtime. Перед вызовом journal уже содержит фазу
  `ACTIVATING`.
- `verify` выполняет реальные bounded HTTPS-запросы через выбранный outbound, не
  ICMP ping. Успех возвращает `active_profile` и фактически подтверждённые
  capabilities.
- `rollback` восстанавливает `applied` (либо гарантированный fail-closed state,
  если applied отсутствует). Операция обязана быть идемпотентной.
- `fail-closed` **не получает journal path и не читает journal**. Он обязан быть
  идемпотентным и, опираясь только на статически принадлежащие adapter'у имена,
  остановить managed tunnel/process state и гарантированно заблокировать forward
  из всех managed VPN zones. Controller вызывает его после любого
  неподтверждённого rollback и при invalid journal. Успех можно вернуть только
  после фактической установки блокировки.

Минимальный успешный ответ:

```json
{
  "ok": true,
  "active_profile": "vless-reality",
  "capabilities": {
    "vless": true,
    "hysteria2": false,
    "amneziawg": false,
    "zapret": false,
    "policy_routing": true
  }
}
```

Ошибка возвращает только allowlisted code `[a-z0-9_]{1,64}` без command line,
ключей и адресов. Controller заменяет значение вне allowlist на `adapter_failed`:

```json
{"ok":false,"code":"sing_box_check_failed"}
```

`restore` восстанавливает applied bundle до обращения к сайту, включая запуск после
перезагрузки; это не применение новых локальных настроек. `status` возвращает только
безопасные метаданные текущего процесса. `capabilities` до проверки конфигурации
возвращает false; `verify`/`rollback`/`restore` возвращают возможности выбранного
renderer и активный профиль. Для auto профиль обозначается как `auto`, без заявления,
что известен текущий внутренний выбор URLTest.
