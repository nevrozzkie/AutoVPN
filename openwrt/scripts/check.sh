#!/bin/sh

set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

node --test "$ROOT/tests/state.test.cjs" "$ROOT/tests/http.test.cjs" "$ROOT/tests/syntax.test.cjs" "$ROOT/tests/runtime.test.cjs" "$ROOT/tests/awg.test.cjs" "$ROOT/tests/networks.test.cjs" "$ROOT/tests/network-helper.test.cjs" "$ROOT/tests/setup.test.cjs" "$ROOT/tests/installer.test.cjs" "$ROOT/tests/release.test.cjs" "$ROOT/tests/process.test.cjs" "$ROOT/tests/update.test.cjs" "$ROOT/tests/maintenance.test.cjs" "$ROOT/tests/maintenance-rpc.test.cjs"
sh "$ROOT/tests/credential-boundaries.sh"
node --test "$ROOT/tests/probes.test.cjs" "$ROOT/tests/ping-ui.test.cjs"
node --test "$ROOT/tests/zapret.test.cjs" "$ROOT/tests/zapret-lifecycle.test.cjs" "$ROOT/tests/zapret-install.test.cjs"
node --test "$ROOT/tests/update-behavior.test.cjs"

for script in \
	"$ROOT/scripts/install.sh" \
	"$ROOT/files/etc/init.d/autovpn" \
	"$ROOT/files/etc/init.d/autovpn-tunnel" \
	"$ROOT/files/etc/init.d/autovpn-zapret" \
	"$ROOT/files/usr/libexec/autovpn/zapret-install" \
	"$ROOT/files/etc/init.d/autovpn-networks" \
	"$ROOT/files/usr/libexec/autovpn/network-watchdog" \
	"$ROOT/files/usr/libexec/autovpn/loop" \
	"$ROOT/files/usr/libexec/autovpn/http-adapter" \
	"$ROOT/files/usr/libexec/autovpn/runtime-adapter" \
	"$ROOT/files/usr/libexec/autovpn/credential.sh" \
	"$ROOT/files/usr/libexec/autovpn/update-helper" \
	"$ROOT/files/usr/sbin/autovpnctl"
do
	sh -n "$script"
done

python3 -m json.tool "$ROOT/files/usr/share/autovpn/snapshot.schema.json" >/dev/null
python3 -m json.tool "$ROOT/files/usr/share/rpcd/acl.d/luci-app-autovpn.json" >/dev/null
python3 -m json.tool "$ROOT/files/usr/share/luci/menu.d/luci-app-autovpn.json" >/dev/null
node --input-type=commonjs --check <"$ROOT/files/www/luci-static/resources/view/autovpn/overview.js"
node --input-type=commonjs --check <"$ROOT/files/www/luci-static/resources/view/autovpn/settings.js"
node --input-type=commonjs --check <"$ROOT/files/www/luci-static/resources/view/autovpn/networks.js"
node --input-type=commonjs --check <"$ROOT/files/www/luci-static/resources/view/autovpn/setup.js"
node --input-type=commonjs --check <"$ROOT/files/www/luci-static/resources/view/autovpn/maintenance.js"

for source in $(grep -oE '\./files/[^[:space:]\\]+' "$ROOT/Makefile" | sort -u); do
	[ -e "$ROOT/${source#./}" ] || {
		printf 'missing Makefile source: %s\n' "$source" >&2
		exit 1
	}
done

if grep -R -n '[[:blank:]]$' "$ROOT" --exclude-dir=.git; then
	printf '%s\n' 'trailing whitespace found' >&2
	exit 1
fi

printf '%s\n' 'all host-side checks passed'
