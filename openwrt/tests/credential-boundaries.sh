#!/bin/sh

set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
. "$ROOT/files/usr/libexec/autovpn/credential.sh"

repeat_character() {
	character="$1"
	count="$2"
	value=
	while [ "$count" -gt 0 ]; do
		value="${value}${character}"
		count=$((count - 1))
	done
	printf '%s' "$value"
}

expect_valid() {
	autovpn_credential_valid "$1" || exit 1
}

expect_invalid() {
	if autovpn_credential_valid "$1"; then
		exit 1
	fi
}

id8="$(repeat_character i 8)"
id64="$(repeat_character I 64)"
secret43="$(repeat_character s 43)"
secret128="$(repeat_character S 128)"

expect_valid "avrt_${id8}.${secret43}"
expect_valid "avrt_${id64}.${secret128}"
expect_valid "avrt_ab_cd-12.${secret43}"
expect_invalid "avrt_$(repeat_character i 7).${secret43}"
expect_invalid "avrt_$(repeat_character i 65).${secret43}"
expect_invalid "avrt_${id8}.$(repeat_character s 42)"
expect_invalid "avrt_${id8}.$(repeat_character s 129)"
expect_invalid "avrt_${id8}.invalid.secret"

autovpn_router_id_valid router_123
autovpn_router_id_valid "$(repeat_character r 64)"
if autovpn_router_id_valid short7 ||
	autovpn_router_id_valid "$(repeat_character r 65)" ||
	autovpn_router_id_valid 'router.bad'; then
	exit 1
fi

autovpn_persistent_path_valid /etc/autovpn/credentials
autovpn_persistent_path_valid /etc/autovpn/custom-state
if autovpn_persistent_path_valid /etc/autovpn/../shadow ||
	autovpn_persistent_path_valid /etc/autovpn/nested/credentials ||
	autovpn_persistent_path_valid /credentials; then
	exit 1
fi

printf '%s\n' 'credential boundaries passed'
