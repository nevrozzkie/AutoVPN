# Russian bypass database sources

This note records the source and runtime checks performed on 2026-09-05. The
goal is an automatically refreshed Russian domain and IPv4 bypass database for
sing-box without making VPN startup depend on a remote download.

## Selected upstream files

Use these fixed HTTPS endpoints from the official SagerNet repositories:

```text
https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-category-ru.srs
https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-ru.srs
```

`https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-ru.srs`
returned HTTP 404 and must not be used. The valid domain set is
`geosite-category-ru.srs`.

The endpoints intentionally follow the moving `rule-set` branches. They must
not have a compiled-in content hash: doing so would turn automatic updates into
manual releases. The updater should instead use HTTPS, an exact URL allowlist,
bounded downloads, strict decoded-schema validation, and a last-known-good
local file. Runtime diagnostics currently record update time and rule counts;
the hashes below are audit observations, not update pins.

Observed contents on 2026-09-05:

| Set | Branch commit and time | SRS size | SRS SHA256 | Decoded source JSON |
| --- | --- | ---: | --- | --- |
| `geosite-category-ru` | `c7817dba8767fd779c201142a4861a91a1db6a19`, 2026-09-04 03:45:14 UTC | 8,584 bytes | `c36e157adf86edf7b722b51f3acb93bbb2a7f8083932dae29b4b5ef2c1ced870` | 28,093 bytes; version 1; one rule; 222 `domain` and 870 `domain_suffix` strings |
| `geoip-ru` | `b9c5e675b4d5359d4b47f4434fa7ae77e9991306`, 2026-08-12 09:12:44 UTC | 50,089 bytes | `1a8115af741918ff24b37b87d3c6da21eccabc58f1eec059e461dca8bac16ff7` | 288,749 bytes; version 1; one rule; 10,859 `ip_cidr` strings |

Both files were successfully decompiled by the official sing-box 1.13.18
binary at upstream commit `45ca32dcb966f07f97fc888fe8586e359dbe8405`.
The domain JSON contained no operators other than `domain` and
`domain_suffix`; the GeoIP JSON contained only `ip_cidr`. Of the 10,859 GeoIP
prefixes, 8,681 are IPv4 and 2,178 are IPv6. The IPv4 subset had no overlap with
RFC1918, CGNAT, loopback, link-local, multicast, documentation, benchmarking,
`0.0.0.0/8`, or `240.0.0.0/4` ranges at the time of inspection.

The current domain strings are lowercase ASCII without wildcard, underscore,
whitespace, Unicode, or a trailing dot. Twelve `domain_suffix` values use one
leading dot; validators must accept or normalize that representation rather
than reject the official set.

## Coverage and freshness

[`SagerNet/sing-geosite`](https://github.com/SagerNet/sing-geosite) generates
its releases from the latest
[`v2fly/domain-list-community`](https://github.com/v2fly/domain-list-community)
release. `category-ru` includes `tld-ru` and curated Russian companies and
services, including services on non-Russian TLDs. It therefore has better
coverage for a Russian direct-routing policy than a literal `.ru` test alone.
The source release used for the observed branch was published on 2026-09-04.

The small built-in seed should remain independent of that download. The
upstream `data/tld-ru` list contains these 11 suffixes:

```text
moscow
ru
su
tatar
xn--80adxhks
xn--80asehdb
xn--80aswg
xn--c1avg
xn--d1acj3b
xn--p1acf
xn--p1ai
```

[`SagerNet/sing-geoip`](https://github.com/SagerNet/sing-geoip) generates its
database from the latest monthly
[`Dreamacro/maxmind-geoip`](https://github.com/Dreamacro/maxmind-geoip)
release. The observed SagerNet branch date matches the upstream 2026-08-12
release, so the age is upstream cadence rather than an unprocessed SagerNet
update. It is suitable for a country-allocation supplement but is not proof
that a service is legally, physically, or operationally Russian. Domain and IP
sets should be combined; neither is a perfect substitute for the other.

No more current first-party sing-box RU country set was found. RKN/blocklist
community databases answer a different question and should not silently replace
the general RU domain/IP policy.

## Licensing boundary

The `SagerNet/sing-geosite` and `SagerNet/sing-geoip` repository `LICENSE`
files state GPL-3.0-or-later for their generator code. The underlying
`v2fly/domain-list-community` domain source is MIT licensed. SagerNet does not
state a separate license for every generated SRS artifact, so a redistributed
copy should preserve the relevant SagerNet and upstream notices rather than
assuming that only one license applies.

The GeoIP source is different: `Dreamacro/maxmind-geoip` states that it includes
MaxMind GeoLite2 data. GeoLite2 use and redistribution are governed by the
[MaxMind GeoLite End User License Agreement](https://support.maxmind.com/knowledge-base/articles/who-is-covered-by-the-geolite-end-user-license-agreement),
including attribution and update/deletion obligations for redistributed data.
Do not package a full GeoIP-derived seed without completing that license review.
Fetching the current data on the user's router avoids shipping a stale database,
but does not remove the user's applicable GeoLite terms.

The recommended packaged fallback is therefore only the small MIT-source
`tld-ru` suffix seed, with attribution. The full SagerNet domain and GeoIP sets
are runtime update inputs.

## sing-box 1.13.18 package provenance

There are two different, both true, OpenWrt version observations:

- the OpenWrt 25.12.5 image's immutable `feeds.buildinfo` pins packages commit
  `5caa62e0bc9f7fb9b0c12a23267bceb7724214dd`, whose sing-box recipe was
  version 1.12.17;
- the official 25.12 package repository is rolling. On 2026-09-05 it serves
  [`sing-box-tiny-1.13.18-r1.apk`](https://downloads.openwrt.org/releases/25.12.5/packages/aarch64_cortex-a53/packages/sing-box-tiny-1.13.18-r1.apk),
  10,989,599 bytes, last modified 2026-09-04 09:36:21 UTC.

The `openwrt-25.12` packages branch recipe for 1.13.18 is commit
`cf82ed0ac66dcaa585bed8c30f8ed69bae778f37`; it pins upstream source hash
`e41ed9d7adecd7597c1d5cc91818366a9538d94b41c244225ac40ac948c643f5`.
The tiny variant includes `with_clash_api`, `with_quic`, `with_utls`,
`badlinkname`, and `tfogo_checklinkname0`, plus `with_gvisor` unless
`CONFIG_SMALL_FLASH` is selected.

`experimental.cache_file` is initialized independently from the Clash API in
sing-box 1.13.18, so remote rule-set caching does not require that build tag.
The current tiny package includes it anyway. Release reproducibility must record
the actually resolved APK version and official repository metadata; the older
image `feeds.buildinfo` alone does not describe today's rolling package set.

## Why native remote rule-sets are unsafe for bootstrap

The following behavior was verified in sing-box 1.13.18 source at commit
`45ca32dcb966f07f97fc888fe8586e359dbe8405`:

- `update_interval` defaults to 24 hours;
- a valid cached set is loaded synchronously and permits startup; if stale, an
  immediate background refresh is attempted and failure keeps the cached set;
- with no valid cache, the initial HTTP fetch is synchronous, and DNS,
  connection, HTTP-status, read, or parse failure aborts sing-box startup;
- a later update failure is logged and the previous in-memory set remains;
- HTTP 200 content is parsed and swapped before it is saved to the cache;
- HTTP 304 advances the last-update time;
- the response body is read with unbounded `io.ReadAll`;
- cache identity is the rule-set tag, not the URL and format. Changing URL or
  format while keeping a tag can restore unrelated or incompatible cached
  bytes; this is tracked upstream in
  [#4136](https://github.com/SagerNet/sing-box/issues/4136) and
  [#4435](https://github.com/SagerNet/sing-box/issues/4435);
- 1.13.18 has no `initial_path` remote fallback.

Without `download_detour`, 1.13.18 uses the outbound manager's default outbound,
which is `route.final` when configured. AutoVPN sets `route.final` to the
selected VPN profile and resolves service hostnames through `tunnel-dns`, also
detoured through that selected profile. Explicitly naming the selected outbound
does not remove this dependency; naming a direct outbound instead uses WAN and
may fail where GitHub raw content is inaccessible. An empty-cache remote set can
therefore prevent the very VPN needed to download it.

## Recommended local update model

Render one local source-format rule-set per lane and point sing-box at it with
`type: "local"`, `format: "source"`. Seed it before sing-box starts. A bounded
controller update may then:

1. fetch only the two exact HTTPS URLs above, directly first and through fixed
   local VPN SOCKS listeners as fallback when available;
2. enforce a timeout, redirect policy, and a conservative maximum SRS size;
3. decompile with the installed sing-box binary into a private temporary path;
4. accept only version 1 and the exact expected arrays, with item, string,
   decoded-size, domain, and CIDR bounds;
5. discard IPv6 and special-use IPv4, deduplicate and deterministically sort;
6. atomically replace the shared local JSON in its own routing directory only after
   the complete candidate validates;
7. retain the prior file and record a failed update without breaking the lane.

The 1.13.18 local rule-set implementation loads the file synchronously at
construction and installs an `fswatch` watcher. A file event is parsed into new
rules before the in-memory set is replaced; invalid replacement content is
logged and the old in-memory rules remain active. This makes a same-directory
atomic rename appropriate.

There are two limitations. A missing or invalid file at initial construction is
fatal, so the seed must exist first. Also, failure to start the watcher is only
logged and does not fail sing-box startup. The controller should expose the
accepted update time and use its normal lane health/reload path rather than
claim that a file write alone proves the new database is active.
