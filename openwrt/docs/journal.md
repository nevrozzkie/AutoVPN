# Transaction journal

`/etc/autovpn/state/journal.json` — единственный authoritative state controller’а.
Он root-only, потому что embedded snapshots содержат VPN credentials.

Основные поля:

- `desired`: валидированный snapshot, ожидающий или проходящий apply;
- `applied`: последний snapshot, для которого завершились activate и HTTPS verify;
- `last_good`: предыдущее значение `applied` после следующего успешного commit;
- `rejected`: metadata последнего отклонённого snapshot без копирования secret
  values в diagnostics;
- `pending_report`: точные idempotency key, snapshot ETag и apply-result body;
- `phase`: durable write-ahead фаза;
- `sequence`: монотонный локальный номер, входящий в idempotency key попытки.

## Переходы

```text
IDLE --200+valid--> READY --> PREPARING --> PREPARED --> ACTIVATING --> VERIFYING
  ^                    |            apply/verify failure             |
  |                    +-----------------> ROLLING_BACK <------------+
  |                                          |       |
  +-------------- rollback ok ---------------+       +--> FAIL_CLOSED
  |
  +------------------------- verified/commit ------------------------+
```

Каждая фаза сохраняется atomic replace до соответствующего внешнего действия.
`304` и уже применённый ETag не меняют sequence или snapshot slots. Validation
failure не меняет `applied`.

После любого внешнего side effect (`prepare`, `activate`, `verify`) ошибка
следующей journal-записи немедленно запускает идемпотентный rollback. Rollback
читает последний durable pre-commit state: в частности, неуспешный commit после
`verify` оставляет на диске `VERIFYING`, где `applied` всё ещё старый, а новый
snapshot всё ещё `desired`. Если filesystem продолжает отказывать, эта recoverable
фаза не затирается и следующий запуск повторяет rollback. Подтверждённый rollback
не превращает исходный apply в успех. Неподтверждённый rollback немедленно вызывает
journal-independent `fail-closed` adapter action.

Успешный verify делает `last_good = applied`, `applied = desired`, очищает desired и
создаёт pending `APPLIED`. Ошибка вызывает adapter rollback; при подтверждённом
rollback applied остаётся прежним, desired переносится в rejected metadata и
создаётся pending `FAILED`. Если rollback не подтверждён, phase остаётся
`FAIL_CLOSED`, active profile очищается, а desired сохраняется для повторного
recovery.

Pending result очищается только после HTTP adapter success. Transport failure не
пересоздаёт key/body. Повторная попытка того же snapshot после уже сообщённого
`FAILED` получает новый suffix `-s<sequence>` и потому не конфликтует с прежним
idempotency key.

При загрузке journal применяется exact-key allowlist и полная проверка типов и
инвариантов для всех top-level полей, snapshot entries, rejected metadata,
capabilities/profile/error и pending report. Snapshot каждого slot повторно
проверяется тем же v3 validator. Неизвестная phase, неизвестное поле, неполный
object или phase/desired mismatch дают `journal_invalid`; controller не продолжает
refresh, не печатает state и вызывает journal-independent `fail-closed`. Размер
журнала при загрузке ограничен 256 KiB.
