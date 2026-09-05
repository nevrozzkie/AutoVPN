#!/bin/sh

autovpn_credential_valid() {
	local token remainder credential_id credential_secret
	token="$1"
	case "$token" in
		avrt_*.*) ;;
		*) return 1 ;;
	esac
	remainder="${token#avrt_}"
	credential_id="${remainder%%.*}"
	credential_secret="${remainder#*.}"
	case "$credential_id" in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac
	case "$credential_secret" in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac
	[ "${#credential_id}" -ge 8 ] &&
		[ "${#credential_id}" -le 64 ] &&
		[ "${#credential_secret}" -ge 43 ] &&
		[ "${#credential_secret}" -le 128 ] &&
		[ "${#token}" -le 256 ]
}

autovpn_router_id_valid() {
	local router_id
	router_id="$1"
	case "$router_id" in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac
	[ "${#router_id}" -ge 8 ] && [ "${#router_id}" -le 64 ]
}

autovpn_persistent_path_valid() {
	local leaf
	case "$1" in
		/etc/autovpn/*) leaf="${1#/etc/autovpn/}" ;;
		*) return 1 ;;
	esac
	case "$leaf" in ''|.|..|*/*|*[!A-Za-z0-9_.-]*) return 1 ;; esac
	return 0
}
