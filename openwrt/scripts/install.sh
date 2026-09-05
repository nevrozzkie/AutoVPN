#!/bin/sh
# Release template. prepare-release.py pins these public, non-secret values.
set -eu
umask 077
export LC_ALL=C

RELEASE_BASE='@AUTOVPN_RELEASE_BASE@'
PUBLIC_KEY_SHA256='@AUTOVPN_SIGNING_KEY_SHA256@'
MANIFEST_SHA256='@AUTOVPN_MANIFEST_SHA256@'
CHECK_ONLY=0
WORK=''

fail() { printf 'AutoVPN: %s\n' "$*" >&2; exit 1; }
say() { printf 'AutoVPN: %s\n' "$*"; }
cleanup() {
	# Only the private directory created by this invocation, never a caller path.
	case "$WORK" in /tmp/autovpn-install.*) rm -rf -- "$WORK" ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

case "${1-}" in
	'') ;;
	--check) CHECK_ONLY=1; shift ;;
	--help) printf '%s\n' 'Usage: sh install.sh [--check]' 'Configure the site URL and token in LuCI after installation.'; exit 0 ;;
	*) fail 'Unknown option. Use --check or no arguments; do not pass credentials.' ;;
esac
[ "$#" -eq 0 ] || fail 'Unexpected arguments.'
printf '%s\n' "$RELEASE_BASE" | grep -Eq '^https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.-]+$' ||
	fail 'This is an unpublished source template. Use install.sh from a prepared GitHub release.'
for value in "$PUBLIC_KEY_SHA256" "$MANIFEST_SHA256"; do
	printf '%s\n' "$value" | grep -Eq '^[0-9a-f]{64}$' || fail 'Release pins are missing.'
done
[ "$(id -u)" = 0 ] || fail 'Run as root on the OpenWrt router.'
for tool in apk ubus jsonfilter sha256sum df awk grep sort wc mktemp uname tr cp chmod; do
	command -v "$tool" >/dev/null 2>&1 || fail "Missing $tool. Requires official OpenWrt 25.12 with APK; no firmware will be flashed."
done
if command -v curl >/dev/null 2>&1; then
	FETCH=curl
elif command -v uclient-fetch >/dev/null 2>&1; then
	FETCH=uclient-fetch
else
	fail 'An HTTPS downloader (curl or uclient-fetch) is required.'
fi

WORK=$(mktemp -d /tmp/autovpn-install.XXXXXX) || fail 'Cannot create temporary directory.'
chmod 0700 "$WORK"
ubus call system board >"$WORK/board.json" || fail 'Cannot identify OpenWrt.'
field() { jsonfilter -i "$1" -e "$2" 2>/dev/null; }
release=$(field "$WORK/board.json" '@.release.version') || fail 'Missing OpenWrt release.'
distribution=$(field "$WORK/board.json" '@.release.distribution') || fail 'Missing distribution.'
target=$(field "$WORK/board.json" '@.release.target') || fail 'Missing OpenWrt target.'
architecture=$(apk --print-arch) || fail 'Cannot identify APK architecture.'
[ "$distribution" = OpenWrt ] || fail 'Only official OpenWrt is supported.'
printf '%s\n' "$release" | grep -Eq '^25\.12\.[0-9]+$' || fail 'Only stable OpenWrt 25.12.x is supported; no opkg or snapshot conversion.'
printf '%s\n' "$target" | grep -Eq '^[a-z0-9_]+/[a-z0-9_-]+$' || fail 'Invalid target.'
printf '%s\n' "$architecture" | grep -Eq '^[a-z0-9_-]+$' || fail 'Invalid architecture.'
apk query --installed --match name --fields name,version --format json kernel >"$WORK/kernel.json" || fail 'Cannot read installed kernel ABI.'
[ "$(field "$WORK/kernel.json" '@[0].name')" = kernel ] || fail 'Missing installed kernel package.'
kernel_package=$(field "$WORK/kernel.json" '@[0].version') || fail 'Missing kernel package version.'
kernel_release=$(uname -r)
printf '%s\n' "$kernel_package" | grep -Eq '^[A-Za-z0-9_.+~-]+$' || fail 'Invalid kernel package version.'

download() {
	# No secrets, arbitrary URLs, shell evaluation or TLS verification bypasses.
	# A file-size limit bounds even a chunked or endless response. Hash pins also
	# protect the uclient-fetch fallback if a server redirects to another scheme.
	file=$1
	limit=$2
	if [ "$FETCH" = curl ]; then
		(ulimit -f "$((limit * 2))"; curl --disable --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --connect-timeout 15 --max-time 180 --output "$WORK/$file" "$RELEASE_BASE/$file") || fail "Download failed: $file"
	else
		(ulimit -f "$((limit * 2))"; uclient-fetch -q -T 60 -O "$WORK/$file" "$RELEASE_BASE/$file") || fail "Download failed: $file (check router time, HTTPS certificates and Internet access)."
	fi
	[ "$(wc -c <"$WORK/$file")" -le "$((limit * 1024))" ] || fail "Oversized download: $file"
}
check_hash() {
	actual=$(sha256sum "$WORK/$1") || fail 'Cannot calculate checksum.'
	[ "${actual%% *}" = "$2" ] || fail "Checksum mismatch: $1"
}
manifest="manifest-$release-$(printf '%s' "$target" | tr / -)-$architecture.json"
say "Checking $release / $target / $architecture."
download "$manifest" 64
check_hash "$manifest" "$MANIFEST_SHA256"
MANIFEST="$WORK/$manifest"
[ "$(field "$MANIFEST" '@.schema_version')" = 1 ] || fail 'Unsupported release manifest.'
[ "$(field "$MANIFEST" '@.release')" = "$release" ] || fail 'Release mismatch.'
[ "$(field "$MANIFEST" '@.target')" = "$target" ] || fail 'Target mismatch.'
[ "$(field "$MANIFEST" '@.architecture')" = "$architecture" ] || fail 'Architecture mismatch.'
[ "$(field "$MANIFEST" '@.kernel_release')" = "$kernel_release" ] || fail 'Kernel release mismatch. No kernel or bootloader changes will be made.'
[ "$(field "$MANIFEST" '@.kernel_package')" = "$kernel_package" ] || fail 'Kernel package ABI mismatch. No force-install is permitted.'
[ "$(field "$MANIFEST" '@.signing_key')" = autovpn-signing.pem ] || fail 'Unexpected signing key filename.'
[ "$(field "$MANIFEST" '@.signing_key_sha256')" = "$PUBLIC_KEY_SHA256" ] || fail 'Signing key pin mismatch.'
min_free=$(field "$MANIFEST" '@.min_free_kib') || fail 'Missing flash budget.'
min_tmp=$(field "$MANIFEST" '@.min_tmp_kib') || fail 'Missing temporary-space budget.'
for value in "$min_free" "$min_tmp"; do
	printf '%s\n' "$value" | grep -Eq '^[1-9][0-9]{3,6}$' || fail 'Invalid measured space budget.'
done
check_space() {
	free_flash=$(df -Pk /overlay | awk 'NR == 2 {print $4}') || fail 'Cannot inspect writable overlay.'
	free_tmp=$(df -Pk /tmp | awk 'NR == 2 {print $4}') || fail 'Cannot inspect temporary storage.'
	case "$free_flash:$free_tmp" in *[!0-9:]*|:*|*:) fail 'Cannot measure free space.' ;; esac
	[ "$free_flash" -ge "$min_free" ] || fail "Not enough writable flash: $free_flash KiB free, release requires $min_free KiB. No repartitioning will be attempted."
	if [ "${1-}" != flash ]; then
		[ "$free_tmp" -ge "$min_tmp" ] || fail "Not enough temporary space: $free_tmp KiB free, release requires $min_tmp KiB."
	fi
}
check_space

# Use only existing official OpenWrt URLs, without editing the user's feeds.
# Drop custom feeds, tags and executable/config directives. Keep the exact
# release's paths (including kernel ABI subdirectory), not a guessed latest URL.
: >"$WORK/repositories"
for repos in /etc/apk/repositories /etc/apk/repositories.d/*.list /lib/apk/repositories.d/*.list; do
	[ -f "$repos" ] || continue
	awk -v rel="$release" '
		NF == 1 && $1 ~ /^https:\/\/downloads\.openwrt\.org\/releases\/[A-Za-z0-9_.\/+~-]+$/ {
			if (index($1, "https://downloads.openwrt.org/releases/" rel "/") == 1 ||
			    index($1, "https://downloads.openwrt.org/releases/packages-25.12/") == 1) print $1
		}' "$repos" >>"$WORK/repositories"
done
sort -u "$WORK/repositories" >"$WORK/repositories.sorted"
[ -s "$WORK/repositories.sorted" ] || fail 'No official HTTPS OpenWrt feeds found. Restore official feeds in LuCI/System/Software.'
mkdir "$WORK/keys" "$WORK/publisher-key" "$WORK/cache"
for key in /lib/apk/keys/* /etc/apk/keys/*; do
	[ -f "$key" ] || continue
	cp "$key" "$WORK/keys/"
done
download autovpn-signing.pem 16
check_hash autovpn-signing.pem "$PUBLIC_KEY_SHA256"
cp "$WORK/autovpn-signing.pem" "$WORK/publisher-key/autovpn-signing.pem"
[ ! -e "$WORK/keys/autovpn-signing.pem" ] || fail 'Signing key filename collides with an existing system key.'
cp "$WORK/autovpn-signing.pem" "$WORK/keys/autovpn-signing.pem"

count=0
names=' '
files=' '
set --
while [ "$count" -lt 4 ]; do
	name=$(field "$MANIFEST" "@.packages[$count].name" || true)
	[ -n "$name" ] || break
	[ "$count" -lt 3 ] || fail 'Too many release packages.'
	case "$name" in autovpn-controller|kmod-amneziawg|amneziawg-tools) ;; *) fail 'Unexpected package name.' ;; esac
	case "$names" in *" $name "*) fail 'Duplicate package name.' ;; esac
	names="$names$name "
	filename=$(field "$MANIFEST" "@.packages[$count].filename") || fail 'Missing package filename.'
	printf '%s\n' "$filename" | grep -Eq '^[A-Za-z0-9][A-Za-z0-9_.+~-]*\.apk$' || fail 'Unsafe package filename.'
	case "$files" in *" $filename "*) fail 'Duplicate package filename.' ;; esac
	files="$files$filename "
	hash=$(field "$MANIFEST" "@.packages[$count].sha256") || fail 'Missing package checksum.'
	printf '%s\n' "$hash" | grep -Eq '^[0-9a-f]{64}$' || fail 'Invalid package checksum.'
	download "$filename" 16384
	check_hash "$filename" "$hash"
	apk --keys-dir "$WORK/publisher-key" verify "$WORK/$filename" || fail "Invalid package signature: $name"
	set -- "$@" "$WORK/$filename"
	count=$((count + 1))
done
case "$names" in *' autovpn-controller '*) ;; *) fail 'Missing controller package.' ;; esac
awg=$(field "$MANIFEST" '@.capabilities.amneziawg') || fail 'Missing AmneziaWG capability.'
case "$awg:$count" in
	true:3) ;;
	false:1) say 'This release has VLESS/Hysteria2 only. Kernel-matched AmneziaWG packages are not included.' ;;
	*) fail 'Inconsistent AmneziaWG package set.' ;;
esac

# Check-only downloads into RAM and does not commit packages or UCI settings.
apk --keys-dir "$WORK/keys" --repositories-file "$WORK/repositories.sorted" --cache-dir "$WORK/cache" update || fail 'Official feed refresh failed.'
apk --keys-dir "$WORK/keys" --repositories-file "$WORK/repositories.sorted" --cache-dir "$WORK/cache" add --simulate "kernel=$kernel_package" "$@" || fail 'Package dependency/ABI check failed.'
if [ "$CHECK_ONLY" = 1 ]; then
	say 'Release signatures, compatibility, space budget and dependency plan checked; nothing installed.'
	exit 0
fi
# Recheck overlay before the commit. APK performs its own locked transaction.
check_space flash
say 'Installing signed packages; dependencies come from official OpenWrt feeds.'
apk --keys-dir "$WORK/keys" --repositories-file "$WORK/repositories.sorted" --cache-dir "$WORK/cache" --cache-predownload add "kernel=$kernel_package" "$@" ||
	fail 'Package installation failed. Existing network configuration was not changed by this installer; inspect APK errors before retrying.'
say 'Installed. Open LuCI → Services → AutoVPN → Setup.'
say 'Enter the site URL, router ID/token, Wi-Fi name and WPA2 password there. LAN/SSID settings are not changed by this installer.'
say 'Zapret transport is not implemented in this release; its prepared SSIDs remain disabled.'
