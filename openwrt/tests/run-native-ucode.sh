#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
UCODE=${AUTOVPN_UCODE_BINARY:?Set AUTOVPN_UCODE_BINARY to the real OpenWrt SDK host ucode}
MODULES=${AUTOVPN_UCODE_MODULES:?Set AUTOVPN_UCODE_MODULES to the SDK host lib/ucode directory}
FILES=${1:-"$ROOT/files"}
WORK=$(mktemp -d /tmp/autovpn-native-ucode.XXXXXX)
trap 'rm -f "$WORK/compiled.uc"; rmdir "$WORK"' EXIT
"$UCODE" -L "$MODULES" -L "$FILES/usr/share/ucode" \
	"$ROOT/tests/native-modules.uc" "$FILES/usr/share/ucode/autovpn"
for helper in "$FILES"/usr/libexec/autovpn/*.uc "$FILES"/usr/share/rpcd/ucode/luci.autovpn; do
	# Host ucode lacks ubus/UCI. Keep these as dynamic imports while compiling
	# every real helper; do not execute network-affecting entry points here.
	"$UCODE" -L "$MODULES" -L "$FILES/usr/share/ucode" \
		-c,dynlink=uci,dynlink=ubus -o "$WORK/compiled.uc" "$helper"
done
printf '%s\n' 'All packaged ucode entry points compiled successfully.'
