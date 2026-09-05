#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
	echo "Usage: $0 /absolute/path/to/pinned-amneziawg-tools-source" >&2
	exit 64
fi

TOOLS_SOURCE=$1
TEST_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for source in config.c encoding.c; do
	[ -f "$TOOLS_SOURCE/src/$source" ] || {
		echo "Missing pinned tools source: $TOOLS_SOURCE/src/$source" >&2
		exit 66
	}
done
[ -f "$TOOLS_SOURCE/src/uapi/linux/linux/wireguard.h" ] || {
	echo "Missing pinned tools UAPI header" >&2
	exit 66
}

WORK=$(mktemp -d "${TMPDIR:-/tmp}/autovpn-awg-parser.XXXXXX")
cleanup() { rm -rf -- "$WORK"; }
trap cleanup EXIT HUP INT TERM

"${CC:-cc}" -std=gnu11 -D_GNU_SOURCE -Wall -Wextra -Werror \
	-I"$TOOLS_SOURCE/src" -I"$TOOLS_SOURCE/src/uapi/linux" \
	"$TEST_DIR/awg-parser.c" "$TOOLS_SOURCE/src/config.c" \
	"$TOOLS_SOURCE/src/encoding.c" -o "$WORK/awg-parser"
"$WORK/awg-parser"
