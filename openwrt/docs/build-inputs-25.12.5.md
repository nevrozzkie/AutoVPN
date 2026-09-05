# OpenWrt 25.12.5 build inputs and AmneziaWG compatibility

This note records the inputs verified on 2026-09-05 for the Cudy WR3000S v1
release. The earlier patch failures below are retained as audit evidence.
The standalone replacement now has paired package recipes and a bounded
AWG1-on-UAPI2 renderer; this is not a claim of a successful target load or
handshake. SDK build evidence, when available, is recorded separately.

## Exact OpenWrt target and SDK

The Cudy WR3000S v1 is an OpenWrt `mediatek/filogic` target with package
architecture `aarch64_cortex-a53`. The installed firmware reports:

- OpenWrt `25.12.5`, build `r33051-f5dae5ece4`;
- `uname -r`: `6.12.94`;
- installed `kernel` package version:
  `6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1`.

Use only this SDK archive:

```text
URL:    https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/openwrt-sdk-25.12.5-mediatek-filogic_gcc-14.3.0_musl.Linux-x86_64.tar.zst
SHA256: ff4a38a397caa2cfe1c39e18f84ddede14878221b3593c3f2c4cfe24e3ec4c25
```

The corresponding official kernel package is:

```text
URL:    https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/packages/kernel-6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1.apk
SHA256: cfb2efbe0d2c54b32bf7e9c64d882fcdb231255f7b3b129b3cb920f892c58d80
```

Both hashes are present in the official
[`sha256sums`](https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/sha256sums).
The SDK is a Linux x86-64 archive; the current macOS host without a Linux VM or
container is not a substitute for this build environment.

The official `feeds.buildinfo` for this target pins these feed revisions:

```text
packages   5caa62e0bc9f7fb9b0c12a23267bceb7724214dd
luci       128a7812f4be233c5dd7f7466f534fd888785caf
routing    3d7d0dc7fa43d3eb09498417407e95a6552e5312
telephony  2618106d5846a4a542fdf5809f0d3ed228ce439b
video      094bf58da6682f895255a35a84349a79dab4bf95
```

The authoritative catalog inputs are
[`version.buildinfo`](https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/version.buildinfo)
and
[`feeds.buildinfo`](https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/feeds.buildinfo).
Do not replace the release SDK with a snapshot SDK or reuse a kmod from another
firmware build: the package must depend on exactly
`kernel=6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1`.

## Official `amneziawg-openwrt` state

The official [`amnezia-vpn/amneziawg-openwrt`](https://github.com/amnezia-vpn/amneziawg-openwrt)
repository was inspected at commit:

```text
56bf9fed93df48d2b747edd6e7a7c5fbe2b01afe
```

Relevant pinned files:

- [`kmod-amneziawg/Makefile`](https://github.com/amnezia-vpn/amneziawg-openwrt/blob/56bf9fed93df48d2b747edd6e7a7c5fbe2b01afe/kmod-amneziawg/Makefile)
  (SHA256 `105b9be3d6670ad2fcd04da46739ea845882b061a9f69fa4b0b837cf8e71b0d7`);
- [`kmod-amneziawg/files/000-initial-amneziawg.patch`](https://github.com/amnezia-vpn/amneziawg-openwrt/blob/56bf9fed93df48d2b747edd6e7a7c5fbe2b01afe/kmod-amneziawg/files/000-initial-amneziawg.patch)
  (SHA256 `3d3ae7afa91373b14c1e52aa30c120bf735c4eb5397f1c80e3029e4d76da6f0a`);
- [`amneziawg-tools/Makefile`](https://github.com/amnezia-vpn/amneziawg-openwrt/blob/56bf9fed93df48d2b747edd6e7a7c5fbe2b01afe/amneziawg-tools/Makefile)
  (SHA256 `b1b6f95f484830187c8eb8dd1b56d47fece825830b7c274e7d2c6c13caa3f9bd`).

That kmod recipe has no pinned external module source. It copies the WireGuard
sources from `$(LINUX_DIR)/drivers/net/wireguard`, applies its 2024 patch, and
builds the result. Its dependencies still exist in OpenWrt 25.12:
`kmod-udptunnel4`, `kmod-udptunnel6`,
`kmod-crypto-lib-chacha20poly1305`, and `kmod-crypto-lib-curve25519`.

The tools recipe pins official
[`amneziawg-tools` tag `v1.0.20240213`](https://github.com/amnezia-vpn/amneziawg-tools/tree/v1.0.20240213),
commit `6eb1abfa4fd52d91b7f3d0ad22b53cb3204fabf8`, with source hash
`4bde122630c9ddb1ec013c3e958f2c613b9eea56834674dda92fcb423c6f4d10`.

## Reproduced patch rejection on Linux 6.12.94

The feed's `000-initial-amneziawg.patch` was applied with the same
`patch -F3 -t -p0` options to the official Linux stable `v6.12.94` WireGuard
files. It did not apply cleanly:

```text
patching file device.c
1 out of 2 hunks failed--saving rejects to device.c.rej
patching file send.c
1 out of 9 hunks failed--saving rejects to send.c.rej
```

The exact rejected hunks were:

- `device.c.rej`, `@@ -471,3 +476,118 @@`: insertion of
  `wg_device_handle_post_config(struct net_device *dev, struct amnezia_config *asc)`;
- `send.c.rej`, `@@ -290,6 +332,7 @@`: insertion of
  `struct wg_device *wg` in the multicore worker.

This is evidence that the official frozen feed recipe is not a clean patch
against vanilla Linux 6.12.94. OpenWrt may carry target kernel patches, so only
building against the exact SDK can establish the final result. Until that build
succeeds, the upstream feed Makefile must not be treated as compatible.

## Pinned AWG1 source candidates are also not yet a build result

The last inspected official module tag before the later standalone/UAPI-v2
series is:

```text
Repository: https://github.com/amnezia-vpn/amneziawg-linux-kernel-module
Tag:        v1.0.20241112
Commit:     7596c5c27855a50aa79c0cc923f73f650b32470b
Tar URL:    https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/archive/refs/tags/v1.0.20241112.tar.gz
Tar SHA256: 3c70dce9aec00c217021037b475fbcb792ab701d72bfd696c866f8740b96218d
```

The corresponding AWG1 userspace candidate is:

```text
Repository: https://github.com/amnezia-vpn/amneziawg-tools
Tag:        v1.0.20241018
Commit:     c0b400c6dfc046f5cae8f3051b14cb61686fcf55
Tar URL:    https://github.com/amnezia-vpn/amneziawg-tools/archive/refs/tags/v1.0.20241018.tar.gz
Tar SHA256: 60f1cec1774fb871a2d8dc24e4f731625516d90f663d6e0d2c77d9247222f2f9
```

Their generic-netlink UAPI is version 1 and the controller-used interface
attributes `Jc`, `Jmin`, `Jmax`, `S1`, `S2`, and `H1` through `H4` have matching
numbers. This makes them bounded source candidates, not verified OpenWrt
packages.

The module tag's own modern-kernel preparation was also tested against the
Linux stable `v6.12.94` WireGuard files. The following rejects were observed:

```text
patches/000-initial-amneziawg.patch:
  device.c: 1 of 2 hunks failed
  send.c:   1 of 9 hunks failed
patches/001-legacy-clients-support.patch:
  device.c: 2 of 2 hunks failed
patches/003-fix-for-non-linear-skb.patch:
  receive.c: 1 of 2 hunks failed
patches/005-bogus-endpoints-parameter.patch:
  netlink.c: 1 of 2 hunks failed
patches/006-bogus-endpoints-prefixes.patch:
  netlink.c: 1 of 3 hunks failed
```

Later failures may cascade from the first partially applied patch. There is an
additional trap: the upstream `src/Makefile` runs patches in a shell loop
without fail-fast handling and writes `generated/.patches.stamp` after the loop,
so the preparation target can appear successful while `.rej` files exist.
Every future SDK recipe must fail if any patch command fails or any `*.rej`
file is present.

## Why newer standalone tags are not a drop-in replacement

Starting with the inspected official module tag
[`v1.0.20251004`](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/tree/v1.0.20251004)
(commit `8a55ceb761b55a064dac38e9446cbedf17c82b00`, tar SHA256
`c60393ed591c87abebcdd6f7bd87e3faef747902a4f76a6b41bf4ff189220e75`),
the tagged source is a standalone external module rather than the earlier
patch-on-current-WireGuard layout. However, it uses generic-netlink UAPI version
2, adds `S3`, `S4`, `I1` through `I5`, and makes advanced security a peer-level
setting.

The original AutoVPN AWG renderer wrote AWG1 interface fields only and did not
emit the newer peer `AdvancedSecurity` setting. It was not compatible with a
silent replacement by these tags. The paired recipes under `openwrt/packages/`
now address this explicitly: a package-owned receipt selects `S3 = 0`, `S4 = 0`
and peer `AdvancedSecurity = on`, with no I-fields. Existing installations
without the receipt retain the old syntax. See [amnezia.md](amnezia.md).

The tools source is pinned to tag `v1.0.20250903`, commit
`5c6ffd6168f7c69199200a91803fa02e1b8c4152`, SHA256
`d729a6f54aafcd55b2cbb7324f09ca8f0d2536772970652bf822a271d0c907d7`.
Its checked-in generic-netlink header is patched from UAPI 1 to UAPI 2 to match
the module. The native parser test verifies configuration fields, not IPC or
kernel interoperability; the SDK must apply that patch and build the complete
binary before it can be considered an install candidate.

## Reproducible release gate

The only established controller build target is
`make package/autovpn-controller/compile V=s` after adding `openwrt/` to the
exact SDK and installing the SDK's pinned feeds. A future Linux build script
must take all of the following as explicit, logged inputs:

1. the exact SDK URL and SHA256 above;
2. an immutable AutoVPN source commit;
3. immutable AmneziaWG module/tools commits and verified source hashes;
4. the custom APK signing key supplied outside the repository, with only its
   public key passed to `prepare-release.py`;
5. a new output directory and measured router `/overlay` and `/tmp` budgets.

The recipes are custom AutoVPN packaging of pinned official sources, not
upstream-issued binary packages. Before an AWG-capable public release is
approved, a Linux x86-64 build using this exact SDK must:

1. build both the kmod and matching `amneziawg-tools` from pinned source without
   ignored patch failures;
2. produce APK v3 packages signed by the intended release key;
3. show `kmod-amneziawg` metadata depending on exactly
   `kernel=6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1`;
4. pass SDK-host `apk verify` and `apk adbdump` checks;
5. pass a target canary: module load, interface creation, `awg setconf`, and an
   actual AWG1 handshake/traffic test on matching OpenWrt 25.12.5 hardware.

Until those gates pass, publish a controller-only VLESS/Hysteria2 release or
leave AWG unavailable. Do not publish an untested kmod, use `--allow-untrusted`,
or weaken the exact kernel-package check.
