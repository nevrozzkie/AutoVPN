# Сборка локального release для GitHub

Роутерные исходники входят в `openwrt/` единого репозитория AutoVPN и поставляются
как часть AutoVPN 2.0. Команды подготовки ниже выполняются из корня AutoVPN.
APK сохраняет собственную техническую версию `0.10.0` независимо от тега всего проекта.

Этот проект не скачивает свои пакеты с AutoVPN-сервера. Роутер получает
установочный `install.sh` из конкретного GitHub Release, а обычные зависимости
(`sing-box`, LuCI, `ucode`, firewall и т.д.) — только из уже настроенных
официальных репозиториев OpenWrt. Скрипт установки не добавляет постоянный
сторонний feed и не меняет ключи доверия OpenWrt.

`prepare-release.py` не собирает, не подписывает и не публикует ничего. Он
принимает уже собранные APK от **того же OpenWrt SDK**, проверяет их APK v3 CLI,
записывает публичный ключ, manifest и готовый однокомандный installer в новый
локальный каталог. Загрузить его в GitHub Release — отдельное ручное действие.
Никаких `git push` или GitHub API этот инструмент не вызывает.

## Неподвижные границы совместимости

Для WR3000S v1 нужен SDK ровно того релиза и target, с которым загружен
роутер. Для первого релиза выбрана **25.12.5**, `mediatek/filogic`,
`aarch64_cortex-a53`. Версию ядра нельзя подставлять из памяти: на роутере это
`uname -r`, а точную строку зависимости берут из установленного пакета
`kernel` (в manifest передаётся только поле `version`, без префикса `kernel=`):

```sh
apk query --installed --match name --fields name,version --format json kernel
```

Скрипт установки сопоставляет эти значения с manifest и прерывает всю установку
при любом расхождении. Отдельный совместимый release без двух AWG-пакетов можно
подготовить для VLESS/Hysteria2, но installer сам не подбирает другой модуль.

Собирай SDK на Linux (не в образе работающего роутера) и зафиксируй в журнале
сборки URL релиза OpenWrt, SHA256 архива SDK и commit каждого добавленного feed:

Скачай точный SDK по ссылке из официального каталога выбранного target, проверь
его SHA256 по `sha256sums` и распакуй. Не используй `*` внутри URL: HTTP-клиент
не подбирает имя SDK по маске. Далее из каталога SDK:

```sh
./scripts/feeds update -a
./scripts/feeds install -a
make defconfig
make package/autovpn-controller/compile V=s
```

Последняя команда предполагает, что каталог `AutoVPN/openwrt/` добавлен в SDK как
package `autovpn-controller`. Команды сборки стороннего kmod намеренно здесь не
придуманы: они зависят от выбранного feed и SDK. Его Makefile обязан зависеть
от точного `kernel=...` из этого SDK.

Для AmneziaWG используй исходники из официальной организации
[AmneziaVPN](https://github.com/amnezia-vpn), зафиксируй конкретный commit и
лицензию в вашем package Makefile. Исходник и kmod должны быть собраны одним
SDK с тем же `LINUX_VERSION`, `LINUX_RELEASE`, target и ABI. Нельзя подменять
это готовым `ipk`, обычным WireGuard или модулем с другого nightly/release.

Перед release нужны ровно эти custom APK:

- `autovpn-controller` (обязателен);
- `kmod-amneziawg` и `amneziawg-tools` — строго парой, если включается AWG.

Все остальные пакеты берутся из official feeds на устройстве. Сборщик отклонит
лишний custom APK, симлинки, приватный ключ, неверную архитектуру, неверное имя
из метаданных и kmod без точной kernel-зависимости. Для метаданных используется
`apk adbdump --format json`, а целостность/подпись каждого входного APK —
`apk verify --keys-dir`; проверяй именно SDK-шным `staging_dir/host/bin/apk`
версии APK v3.

## Бюджет диска и сборка release-каталога

На чистом роутере той же модели сначала измерь реальную установку в overlay и
`/tmp`. В `--min-free-kib` положи размер **полной** установки с запасом для
конфигурации/лога; в `--min-tmp-kib` — скачивание всех APK и временные файлы с
запасом. Оба значения обязаны быть от 4096 до 9999999 KiB. Это не числа по
умолчанию: их нельзя переносить между образами и наборами official dependencies.

Публичный ключ APK — это один PEM-блок `BEGIN PUBLIC KEY`, который будет
поставлен рядом с release. Никогда не передавай private key в этот скрипт или
не добавляй его в GitHub asset.

```sh
python3 openwrt/scripts/prepare-release.py \
  --apk /path/to/sdk/staging_dir/host/bin/apk \
  --release 25.12.5 \
  --target mediatek/filogic \
  --architecture aarch64_cortex-a53 \
  --kernel-release '6.6.99' \
  --kernel-package '6.6.99~example_abcdef' \
  --release-base https://github.com/nevrozzkie/AutoVPN/releases/download/v2.0.0 \
  --signing-key /safe/path/autovpn-signing.pem \
  --package /path/to/autovpn-controller-0.10.0-r1.apk \
  --package /path/to/kmod-amneziawg-0-r1.apk \
  --package /path/to/amneziawg-tools-0-r1.apk \
  --min-free-kib 32768 \
  --min-tmp-kib 65536 \
  --output /safe/path/autovpn-router-v0.10.0
```

Числа ядра и размеров в примере — условные, а не готовая конфигурация WR3000S.
Замени их измеренными значениями. `v2.0.0` — пример тега будущей публикации
в репозитории AutoVPN, не обещание существующего рабочего адреса. Перед публикацией
проверь выбранный тег и отдельное разрешение владельца; скрипт сам ничего не пушит.
Один подготовленный набор рассчитан на один
точный release/target/architecture/kernel, и несколько одинаковых роутеров
могут пользоваться одним набором. Для другой прошивки нужен отдельный набор/тег.

`--output` обязан отсутствовать: этим исключено молчаливое изменение уже
опубликованного release. Внутри получатся `install.sh`, публичный
`autovpn-signing.pem`, APK и файл вида
`manifest-25.12.5-mediatek-filogic-aarch64_cortex-a53.json`. Installer содержит
SHA256 manifest и ключа; manifest содержит SHA256 каждого APK. Поэтому GitHub
asset нельзя подменить редиректом или «latest» без остановки установки.

Создай один неизменяемый GitHub Release с ровно этими файлами и тегом из
`--release-base`. Затем пользователь сможет скачать только `install.sh` одной
командой из этого конкретного release. До реальной сборки и ручной публикации
такую команду не объявляем рабочей. Шаблон команды для опубликованного набора:

```sh
(autovpn_bootstrap="$(mktemp /tmp/autovpn-bootstrap.XXXXXX)" && trap 'rm -f "$autovpn_bootstrap"' EXIT && wget -O "$autovpn_bootstrap" 'https://github.com/nevrozzkie/AutoVPN/releases/download/TAG/install.sh' && sh "$autovpn_bootstrap")
```

Сначала скачивается весь файл с успешным HTTP-статусом, затем начинается
выполнение. Начальное доверие — HTTPS GitHub и выбранный publisher; хеши и
подпись APK не защищают от злонамеренного publisher самого установщика.
Токен, URL личного сайта и Wi-Fi-пароль не нужны в команде: они вводятся в LuCI.
В этой команде `install.sh` — **asset роутерного релиза**, не корневой серверный
`install.sh` из исходников AutoVPN. Не подменяй адрес ссылкой `raw/.../main/install.sh`.
Не включай `set -x` при работе с секретами. Существующие custom feeds на роутере
не удаляются, но при этой установке не используются. APK фиксирует установленное
ядро точным world-ограничением; обновление прошивки — отдельный sysupgrade,
а не `apk upgrade` из установщика.

Первый установщик не обещает атомарного rollback всей установки при обрыве
питания/ошибке пакетного менеджера. Собственные сетевые настройки он не меняет;
создание SSID — отдельная подтверждаемая операция мастера.

Проверка CLI основана на [APK add](https://github.com/alpinelinux/apk-tools/blob/b5a31c0d865342ad80be10d68f1bb3d3ad9b0866/doc/apk-add.8.scd),
[verify](https://github.com/alpinelinux/apk-tools/blob/b5a31c0d865342ad80be10d68f1bb3d3ad9b0866/doc/apk-verify.8.scd)
и [глобальных параметрах APK 3](https://github.com/alpinelinux/apk-tools/blob/b5a31c0d865342ad80be10d68f1bb3d3ad9b0866/doc/apk.8.scd).
