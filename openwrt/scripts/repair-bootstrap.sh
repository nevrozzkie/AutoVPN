#!/bin/sh
# Narrow recovery template for an unconfigured AutoVPN 0.14.0/0.14.1 install.
# prepare-release.py pins the same public, non-secret release values as install.sh.
set -eu
umask 077
export LC_ALL=C

RELEASE_BASE='@AUTOVPN_RELEASE_BASE@'
PUBLIC_KEY_SHA256='@AUTOVPN_SIGNING_KEY_SHA256@'
MANIFEST_SHA256='@AUTOVPN_MANIFEST_SHA256@'
TARGET_CONTROLLER_VERSION='0.14.2-r1'
TRUST_ROOT=/etc/autovpn
RELEASE_RECEIPT="$TRUST_ROOT/release.json"
RELEASE_KEY="$TRUST_ROOT/release-signing.pem"
CREDENTIAL_FILE="$TRUST_ROOT/credentials"
CONTROLLER_JOURNAL="$TRUST_ROOT/state/journal.json"
NETWORK_JOURNAL="$TRUST_ROOT/networks/journal.json"
MAINTENANCE_GATE="$TRUST_ROOT/state/maintenance.lock"
RESUME_MAINTENANCE_GATE="$TRUST_ROOT/state/.resume-maintenance.lock"
UPDATE_GATE="$TRUST_ROOT/state/update.lock"
UPDATE_LOCK=/var/lock/autovpn-update.lock
CONTROLLER_LOCK=/var/lock/autovpn-controller.lock
WIFI_HELPER=/usr/libexec/autovpn/install-wifi
WORK=''
HAVE_UPDATE_LOCK=0
HAVE_CONTROLLER_LOCK=0

fail() { printf 'AutoVPN repair: %s\n' "$*" >&2; exit 1; }
say() { printf 'AutoVPN repair: %s\n' "$*"; }
release_locks() {
	if [ "$HAVE_CONTROLLER_LOCK" = 1 ]; then
		exec 9>&-
		HAVE_CONTROLLER_LOCK=0
	fi
	if [ "$HAVE_UPDATE_LOCK" = 1 ]; then
		exec 8>&-
		HAVE_UPDATE_LOCK=0
	fi
}
cleanup() {
	release_locks
	case "$WORK" in /tmp/autovpn-repair.*) rm -rf -- "$WORK" ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 129' HUP
trap 'exit 143' TERM

case "${1-}" in
	'') ;;
	--help)
		printf '%s\n' 'Usage: sh repair-bootstrap.sh' \
			'Repairs only an unconfigured AutoVPN 0.14.0/0.14.1 controller install.'
		exit 0
		;;
	*) fail 'Unknown option; do not pass site URLs, router IDs or credentials.' ;;
esac
[ "$#" -eq 0 ] || fail 'Unexpected arguments.'
printf '%s\n' "$RELEASE_BASE" | grep -Eq '^https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.-]+$' ||
	fail 'This is an unpublished source template. Use repair-bootstrap.sh from the prepared recovery release.'
for value in "$PUBLIC_KEY_SHA256" "$MANIFEST_SHA256"; do
	printf '%s\n' "$value" | grep -Eq '^[0-9a-f]{64}$' || fail 'Release pins are missing.'
done
[ "$(id -u)" = 0 ] || fail 'Run as root on the OpenWrt router.'
for tool in apk curl ubus jsonfilter sha256sum df awk grep wc mktemp uname tr cp chmod mkdir mv rm flock uci sync; do
	command -v "$tool" >/dev/null 2>&1 || fail "Missing $tool."
done

WORK=$(mktemp -d /tmp/autovpn-repair.XXXXXX) || fail 'Cannot create temporary directory.'
chmod 0700 "$WORK"
exec 8>>"$UPDATE_LOCK" || fail 'Cannot open the AutoVPN update lock.'
if ! flock -n 8; then exec 8>&-; fail 'Another AutoVPN update is running.'; fi
printf '0\n' >"$UPDATE_LOCK" || { exec 8>&-; fail 'Cannot replace the legacy AutoVPN update lock owner.'; }
HAVE_UPDATE_LOCK=1
exec 9>>"$CONTROLLER_LOCK" || fail 'Cannot open the AutoVPN controller lock.'
if ! flock -n 9; then exec 9>&-; fail 'The AutoVPN controller is busy.'; fi
printf '0\n' >"$CONTROLLER_LOCK" || { exec 9>&-; fail 'Cannot replace the legacy AutoVPN controller lock owner.'; }
HAVE_CONTROLLER_LOCK=1

field() { jsonfilter -i "$1" -e "$2" 2>/dev/null; }
regular_absent() { [ ! -e "$1" ] && [ ! -L "$1" ]; }
valid_hash() { printf '%s\n' "$1" | grep -Eq '^[0-9a-f]{64}$'; }
valid_release_base() {
	printf '%s\n' "$1" | grep -Eq '^https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9._-]+$'
}
release_repo() {
	value=${1#https://github.com/}
	printf '%s\n' "${value%%/releases/download/*}"
}
download() {
	file=$1 limit=$2
	case "$file" in *[!A-Za-z0-9._+~-]*|'') return 1 ;; esac
	(ulimit -f "$((limit * 2))"; curl --disable --fail --silent --show-error --location \
		--proto '=https' --proto-redir '=https' --connect-timeout 10 --max-time 120 \
		--output "$WORK/$file" "$RELEASE_BASE/$file") || return 1
	[ "$(wc -c <"$WORK/$file")" -le "$((limit * 1024))" ]
}
check_hash() {
	actual=$(sha256sum "$WORK/$1") || return 1
	[ "${actual%% *}" = "$2" ]
}

# This recovery must never inherit a configured router identity or runtime.
[ "$(uci -q get autovpn.main.enabled 2>/dev/null || true)" = 0 ] ||
	fail 'Controller is not in the disabled factory state.'
setup_prepared=$(uci -q get autovpn.main.setup_prepared 2>/dev/null || true)
case "$setup_prepared" in ''|0) ;; *) fail 'Controller setup has already been prepared.' ;; esac
[ -z "$(uci -q get autovpn.main.base_url 2>/dev/null || true)" ] ||
	fail 'Site URL is already configured.'
[ -z "$(uci -q get autovpn.main.router_id 2>/dev/null || true)" ] ||
	fail 'Router ID is already configured.'
configured_credential=$(uci -q get autovpn.main.credential_file 2>/dev/null || true)
case "$configured_credential" in ''|/etc/autovpn/credentials) ;; *)
	fail 'A custom credential path is configured.' ;;
esac
bootstrap_completed=$(uci -q get autovpn.wifi.bootstrap_completed 2>/dev/null || true)
case "$bootstrap_completed" in ''|0) ;; *) fail 'Wi-Fi bootstrap has already completed.' ;; esac
for config in autovpn network wireless dhcp firewall; do
	[ -z "$(uci -q changes "$config" 2>/dev/null || true)" ] ||
		fail 'Uncommitted UCI changes must be resolved manually.'
done
for path in "$CREDENTIAL_FILE" "$CONTROLLER_JOURNAL" "$NETWORK_JOURNAL" "$MAINTENANCE_GATE" "$RESUME_MAINTENANCE_GATE" "$UPDATE_GATE"; do
	regular_absent "$path" || fail 'Credentials, runtime journals or maintenance state already exist.'
done

# Reuse only the already-installed trust root. The release tag may advance,
# but both scripts must pin the same key and the same GitHub owner/repository.
[ ! -L "$TRUST_ROOT" ] && [ -f "$RELEASE_RECEIPT" ] && [ ! -L "$RELEASE_RECEIPT" ] && \
	[ -f "$RELEASE_KEY" ] && [ ! -L "$RELEASE_KEY" ] ||
	fail 'Existing AutoVPN release trust is incomplete or unsafe.'
[ "$(field "$RELEASE_RECEIPT" '@.schema_version')" = 1 ] || fail 'Invalid release receipt.'
receipt_base=$(field "$RELEASE_RECEIPT" '@.release_base') || fail 'Release receipt has no base URL.'
receipt_key_hash=$(field "$RELEASE_RECEIPT" '@.signing_key_sha256') || fail 'Release receipt has no key pin.'
receipt_version=$(field "$RELEASE_RECEIPT" '@.installed_version') || fail 'Release receipt has no installed version.'
valid_release_base "$receipt_base" || fail 'Release receipt has an unsafe base URL.'
valid_hash "$receipt_key_hash" || fail 'Release receipt has an invalid key pin.'
[ "$receipt_key_hash" = "$PUBLIC_KEY_SHA256" ] || fail 'Recovery release uses a different signing key.'
[ "$(release_repo "$receipt_base")" = "$(release_repo "$RELEASE_BASE")" ] ||
	fail 'Recovery release belongs to a different GitHub repository.'
key_actual=$(sha256sum "$RELEASE_KEY") || fail 'Cannot hash the installed signing key.'
[ "${key_actual%% *}" = "$PUBLIC_KEY_SHA256" ] || fail 'Installed signing key does not match the pinned key.'
case "$receipt_version" in 0.14.0-r1|0.14.0-r3|0.14.1-r1|"$TARGET_CONTROLLER_VERSION") ;; *)
	fail 'Release receipt is outside this recovery path.' ;;
esac

ubus call system board >"$WORK/board.json" || fail 'Cannot identify OpenWrt.'
release=$(field "$WORK/board.json" '@.release.version') || fail 'Missing OpenWrt release.'
distribution=$(field "$WORK/board.json" '@.release.distribution') || fail 'Missing distribution.'
target=$(field "$WORK/board.json" '@.release.target') || fail 'Missing OpenWrt target.'
arch_file=/etc/apk/arch
[ -f "$arch_file" ] || arch_file=/lib/apk/arch
architecture=$(awk 'NF { if (NF != 1 || ++n != 1) exit 1; value=$1 } END { if (n != 1) exit 1; print value }' "$arch_file") ||
	fail 'Cannot read a single configured APK architecture.'
[ "$distribution" = OpenWrt ] || fail 'Only official OpenWrt is supported.'
printf '%s\n' "$release" | grep -Eq '^25\.12\.[0-9]+$' || fail 'Only stable OpenWrt 25.12.x is supported.'
printf '%s\n' "$target" | grep -Eq '^[a-z0-9_]+/[a-z0-9_-]+$' || fail 'Invalid target.'
printf '%s\n' "$architecture" | grep -Eq '^[a-z0-9_-]+$' || fail 'Invalid architecture.'
kernel_release=$(uname -r) || fail 'Cannot read the running kernel release.'
apk query --installed --match name --fields name,version,arch --format json kernel >"$WORK/kernel.json" ||
	fail 'Cannot read installed kernel ABI.'
[ "$(field "$WORK/kernel.json" '@[0].name')" = kernel ] || fail 'Missing installed kernel package.'
[ "$(field "$WORK/kernel.json" '@[0].arch')" = "$architecture" ] ||
	fail 'Configured APK architecture differs from the installed kernel package.'
kernel_package=$(field "$WORK/kernel.json" '@[0].version') || fail 'Missing kernel package version.'
printf '%s\n' "$kernel_package" | grep -Eq '^[A-Za-z0-9_.+~-]+$' || fail 'Invalid kernel package version.'
free_overlay=$(df -Pk /overlay | awk 'NR == 2 {print $4}') || fail 'Cannot inspect writable overlay.'
free_tmp=$(df -Pk /tmp | awk 'NR == 2 {print $4}') || fail 'Cannot inspect temporary storage.'
case "$free_overlay:$free_tmp" in *[!0-9:]*|:*|*:) fail 'Cannot measure recovery free space.' ;; esac
[ "$free_overlay" -ge 2048 ] || fail 'Recovery requires at least 2048 KiB of writable overlay.'
[ "$free_tmp" -ge 20480 ] || fail 'Recovery requires at least 20480 KiB of temporary storage.'

manifest="manifest-$release-$(printf '%s' "$target" | tr / -)-$architecture.json"
say "Checking pinned recovery for $release / $target / $architecture."
download "$manifest" 64 || fail 'Manifest download failed.'
check_hash "$manifest" "$MANIFEST_SHA256" || fail 'Manifest checksum mismatch.'
MANIFEST="$WORK/$manifest"
[ "$(field "$MANIFEST" '@.schema_version')" = 1 ] || fail 'Unsupported release manifest.'
[ "$(field "$MANIFEST" '@.release')" = "$release" ] || fail 'OpenWrt release mismatch.'
[ "$(field "$MANIFEST" '@.target')" = "$target" ] || fail 'OpenWrt target mismatch.'
[ "$(field "$MANIFEST" '@.architecture')" = "$architecture" ] || fail 'OpenWrt architecture mismatch.'
[ "$(field "$MANIFEST" '@.kernel_release')" = "$kernel_release" ] || fail 'Kernel release mismatch.'
[ "$(field "$MANIFEST" '@.kernel_package')" = "$kernel_package" ] || fail 'Kernel package ABI mismatch.'
[ "$(field "$MANIFEST" '@.signing_key_sha256')" = "$PUBLIC_KEY_SHA256" ] || fail 'Manifest signing key mismatch.'

controller_count=0
controller_filename=''
controller_sha=''
controller_version=''
index=0
while [ "$index" -lt 4 ]; do
	name=$(field "$MANIFEST" "@.packages[$index].name" || true)
	[ -n "$name" ] || break
	case "$name" in autovpn-controller|kmod-amneziawg|amneziawg-tools) ;; *)
		fail 'Unexpected package in release manifest.' ;;
	esac
	if [ "$name" = autovpn-controller ]; then
		controller_count=$((controller_count + 1))
		controller_filename=$(field "$MANIFEST" "@.packages[$index].filename") || fail 'Missing controller filename.'
		controller_sha=$(field "$MANIFEST" "@.packages[$index].sha256") || fail 'Missing controller checksum.'
		controller_version=$(field "$MANIFEST" "@.packages[$index].version") || fail 'Missing controller version.'
	fi
	index=$((index + 1))
done
[ "$controller_count" = 1 ] || fail 'Manifest must contain exactly one controller package.'
printf '%s\n' "$controller_filename" | grep -Eq '^autovpn-controller-[A-Za-z0-9._+~-]+\.apk$' ||
	fail 'Unsafe controller filename.'
valid_hash "$controller_sha" || fail 'Invalid controller checksum.'
[ "$controller_version" = "$TARGET_CONTROLLER_VERSION" ] || fail 'Unexpected recovery controller version.'

# Download and authenticate only the controller APK. Native/kernel packages in
# the release manifest are deliberately neither downloaded nor passed to APK.
download "$controller_filename" 16384 || fail 'Controller download failed.'
check_hash "$controller_filename" "$controller_sha" || fail 'Controller checksum mismatch.'
mkdir "$WORK/keys" "$WORK/empty-cache"
chmod 0700 "$WORK/keys" "$WORK/empty-cache"
cp "$RELEASE_KEY" "$WORK/keys/autovpn-signing.pem"
chmod 0600 "$WORK/keys/autovpn-signing.pem"
apk --keys-dir "$WORK/keys" verify "$WORK/$controller_filename" >/dev/null 2>&1 ||
	fail 'Controller package signature is invalid.'
apk adbdump --format json "$WORK/$controller_filename" >"$WORK/controller.json" ||
	fail 'Cannot read controller package metadata.'
[ "$(field "$WORK/controller.json" '@.info.name')" = autovpn-controller ] || fail 'Controller package name mismatch.'
[ "$(field "$WORK/controller.json" '@.info.version')" = "$controller_version" ] || fail 'Controller package version mismatch.'
controller_arch=$(field "$WORK/controller.json" '@.info.arch') || fail 'Controller package architecture is missing.'
case "$controller_arch" in noarch|all|"$architecture") ;; *) fail 'Controller package architecture mismatch.' ;; esac

apk query --installed --match name --fields name,version --format json autovpn-controller >"$WORK/current.json" ||
	fail 'Installed controller could not be read.'
[ "$(field "$WORK/current.json" '@[0].name')" = autovpn-controller ] || fail 'Controller is not installed.'
current_version=$(field "$WORK/current.json" '@[0].version') || fail 'Installed controller version is missing.'
case "$current_version" in
	0.14.0-r1|0.14.0-r3|0.14.1-r1)
		plan=$(apk --no-network --cache-dir "$WORK/empty-cache" --keys-dir "$WORK/keys" \
			add --simulate "$WORK/$controller_filename") || fail 'Controller upgrade plan failed.'
		plan_lines=$(printf '%s\n' "$plan" | grep -E '^\([[:space:]]*[0-9]+/[0-9]+\) ' || true)
		[ "$(printf '%s\n' "$plan_lines" | grep -c '^(')" = 1 ] &&
			printf '%s\n' "$plan_lines" | grep -Eq '^\(1/1\) Upgrading autovpn-controller ' ||
			fail 'APK plan contains a change other than the controller upgrade.'
		say "Upgrading controller $current_version to $controller_version."
		apk --no-network --cache-dir "$WORK/empty-cache" --keys-dir "$WORK/keys" \
			add "$WORK/$controller_filename" || fail 'Controller upgrade failed.'
		;;
	"$TARGET_CONTROLLER_VERSION")
		say 'Controller is already repaired; continuing the interrupted Wi-Fi bootstrap.'
		;;
	*) fail 'Installed controller version is outside this recovery path.' ;;
esac
apk query --installed --match name --fields version --format json autovpn-controller >"$WORK/installed.json" ||
	fail 'Installed controller version could not be verified.'
[ "$(field "$WORK/installed.json" '@[0].version')" = "$TARGET_CONTROLLER_VERSION" ] ||
	fail 'Installed controller version differs from the pinned recovery release.'
sync

[ -x "$WIFI_HELPER" ] || fail 'The repaired Wi-Fi bootstrap helper is missing.'
release_locks
say 'Controller repaired. Starting the installed Wi-Fi bootstrap.'
"$WIFI_HELPER" || fail 'Controller is repaired, but Wi-Fi setup did not complete; rerun this repair only if no recovery journal was created.'
say 'Controller repair and primary Wi-Fi bootstrap completed.'
