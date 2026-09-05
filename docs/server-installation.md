# Установка и обновление server control-plane

Этот документ относится к корневому `install.sh`: он разворачивает веб-панель
AutoVPN на сервере control-plane. Это **не** установщик OpenWrt и не скрипт
установки VPN на выходной VPS.

## Что где работает

Control-plane сервер хранит SQLite, учётные данные администратора, клиентов,
подписки, Router credentials и конфигурацию доступа к VPN VPS. Он обслуживает
панель и публичные ссылки. При нажатии в панели «Установить /
синхронизировать VPN» он подключается к отдельному VPN VPS по SSH и применяет
VPN-конфигурацию там. Корневой installer сам по себе не устанавливает VPN на
удалённом VPS и не выполняет эту синхронизацию.

По умолчанию installer рассчитан на Linux с `apt`, systemd и nginx. Он создаёт
сервис `autovpn`, размещает код в `/opt/autovpn`, данные — в
`/var/lib/autovpn`, а переменные окружения — в `/etc/autovpn.env`. Веб-процесс
слушает только `127.0.0.1:8000`; наружу его должен отдавать nginx или другой
TLS reverse proxy.

## Новая установка

На control-plane сервере сначала получите **конкретную ревизию**, которую
хотите запускать, затем запустите installer из корня checkout:

```sh
git clone https://github.com/nevrozzkie/AutoVPN.git
cd AutoVPN
git checkout --detach <проверенный_commit_или_tag>
sudo bash install.sh \
  --panel-domain panel.example.org \
  --tls-mode domain
```

`install.sh` требует root. Если его запускают из checkout (рядом есть
`pyproject.toml` и `app/`), он использует этот checkout как source; поэтому
commit/tag выше действительно определяет код установки. Installer запросит
admin password на TTY, если не передан `--admin-password`. Данные VPN VPS можно
ввести затем в `/admin/setup`, либо передать documented параметры `--eu-host`,
`--eu-user`, `--eu-port`, `--eu-password` **или** `--eu-key-path`.

Не передавайте пароль в shell history без необходимости. Для неинтерактивного
запуска installer также читает одноимённые переменные окружения, например
`ADMIN_PASSWORD`, `EU_SSH_HOST` и `EU_SSH_KEY_PATH`.

После запуска проверьте локальный сервис и готовность приложения:

```sh
sudo systemctl status autovpn --no-pager
curl -fsS https://panel.example.org/healthz
curl -fsS https://panel.example.org/readyz
```

`/healthz` подтверждает HTTP-ответ. `/readyz` дополнительно read-only проверяет
SQLite и набор применённых миграций; пока приложение не готово, он отвечает
`503`. Эти проверки не проверяют доступность VPN VPS или работоспособность
отдельных VPN-протоколов.

### Выбор source: важное ограничение

У текущего root installer **нет** флагов `--branch`, `--ref` или `--repo`.
Не добавляйте их к его команде: это будет ошибка неизвестного аргумента.
Переменная `AUTOVPN_REPO_URL` существует, но используется только когда скрипт
запущен вне checkout и сам делает `git clone`; она не закрепляет ref/commit.
Для воспроизводимой установки используйте локальный checkout, как в примере
выше. Не подменяйте это недоказанной командой вида `curl | bash` для выбранной
ветки или commit.

## HTTPS обязательно для Router API

Router API использует Bearer credentials, поэтому внешний URL панели и
`/api/v2/router/*` должны быть доступны роутеру только по HTTPS с проверяемым
сертификатом. Рекомендуемый режим — `--panel-domain` вместе с
`--tls-mode domain`: installer создаёт nginx proxy и пытается получить
сертификат через certbot с редиректом на HTTPS. До запуска домен должен
указывать на control-plane сервер, а порт 80 должен быть доступен для проверки
certbot.

`--tls-mode none`, пустой domain или `CONFIGURE_NGINX=0` не подходят для
публичного Router API: в первом случае installer оставляет plain HTTP, а в
последнем nginx вообще не настраивается. Режим `--tls-mode ip` пытается
получить короткоживущий IP certificate и зависит от поддержки этой возможности
у установленного certbot; это не равнозначная замена проверенному domain
certificate.

В `/admin/setup` Router API включён по умолчанию для новой установки, но его
можно выключить: тогда `/api/v2/router/*` отвечает `404`. Новый OpenWrt
controller запрашивает `GET /api/v2/router/snapshot/dual` и не делает fallback
после `404` на legacy `/snapshot`. Поэтому сначала обновляют control-plane до
AutoVPN 2.0 и добиваются HTTPS, а уже затем устанавливают или обновляют
controller на роутере.

## Обновление существующей установки без смены ссылок

Публичные client/subscription URLs и токены являются данными SQLite, а не
частью исходников. При нормальном обновлении их не пересоздают и не меняют.
Сначала зафиксируйте известный из процесса прошлой поставки commit/tag и
состояние сервиса:

```sh
sudo systemctl status autovpn --no-pager
sudo ls -la /var/lib/autovpn/backups
sudo grep '^DATABASE_PATH=' /etc/autovpn.env
```

Обычный installer исключает `.git` при синхронизации в `/opt/autovpn`, поэтому
`git rev-parse HEAD` в этом каталоге не является надёжной командой для
определения уже установленной версии.

Затем подготовьте checkout требуемого AutoVPN 2.0 commit/tag отдельно и
запустите его installer с root:

```sh
git clone https://github.com/nevrozzkie/AutoVPN.git AutoVPN-2.0
cd AutoVPN-2.0
git checkout --detach <проверенный_commit_или_tag>
sudo bash install.sh
```

При повторном запуске installer:

- перед заменой исходников вызывает `tools/prepare_data.py`, который делает
  согласованный SQLite backup через SQLite backup API и проверяет его
  `PRAGMA integrity_check`;
- хранит backup в `DATA_DIR/backups` (по умолчанию
  `/var/lib/autovpn/backups`) с правами `0600`, а каталоги данных — `0700`;
- при первом переходе переносит legacy БД
  `/opt/autovpn/data/autovpn.sqlite3` в persistent
  `/var/lib/autovpn/autovpn.sqlite3` через тот же backup API и оставляет
  исходную legacy БД на месте;
- сохраняет копию существующего `/etc/autovpn.env` в backup-каталоге и меняет
  в рабочем env только `DATABASE_PATH`;
- синхронизирует code в `/opt/autovpn` через `rsync --delete`, но исключает
  `.git`, `.venv`, `data` и `.env`.

Это сохраняет существующую базу и ссылки при успешном обновлении, но не делает
deploy атомарным: после preflight installer может уже заменить часть кода или
зависимостей. У него нет автоматического rollback кода, VPN VPS или действий
Aeza.

После обновления дождитесь запуска `autovpn`, проверьте `/healthz` и `/readyz`,
войдите в `/admin`, затем отдельно выполните «Установить / синхронизировать
VPN». Последнее нужно, когда новая версия/миграция меняет VPN desired state;
backup SQLite не откатывает уже выполненные SSH/Aeza действия.

## Восстановление при неудачном обновлении

Не копируйте работающую WAL SQLite-базу обычным `cp`. Используйте уже созданный
проверенный файл из `DATA_DIR/backups` и сохраняйте неудавшуюся текущую БД для
разбора. Для ручного восстановления нужны одновременно:

1. остановить `autovpn`;
2. вернуть проверенный SQLite backup в путь из `DATABASE_PATH`, с правами
   каталога `0700`, файла `0600` и владельцем service user;
3. вернуть совместимый с этой БД исходный code/venv;
4. запустить сервис и проверить `/readyz`.

Не откатывайте только БД под более новым кодом без проверки границы миграций.
Это инструкция по восстановлению control-plane; состояние VPN VPS проверяется
и восстанавливается отдельным процессом. Transactional VPN apply хранит свои
versioned backups на VPN VPS, но его rollback не является доказанным
универсальным rollback для всех дистрибутивов и не отменяет firewall changes.

## Границы проверки

Документ описывает поведение исходников `install.sh`, `tools/prepare_data.py`
и текущего Router API. Он не является результатом запуска installer на
production-сервере, выпуска TLS-сертификата, SSH-deploy на VPN VPS или
проверки OpenWrt hardware. Перед production обязательно проверьте выбранный
commit на отдельном control-plane и VPN VPS.
