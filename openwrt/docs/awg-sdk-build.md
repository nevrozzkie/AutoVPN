# AmneziaWG SDK build record

This is a package build for an already flashed Cudy WR3000S v1. It does not
build a sysupgrade image, change the bootloader, or alter flash partitions.
Installation and a dual-lane handshake canary require separate device approval.

## Verified result (2026-09-06)

The three APKs were built with the exact release SDK, signed, verified using
only the generated public key, and downloaded to the Mac. Downloaded SHA256s
match. The final build/inspection job exited successfully. Local controller
validation passed **312 tests**, zero failures/skips, plus the native upstream
AWG config-parser check. No router install, module load or handshake was run.

| Package | APK bytes | Installed regular-file bytes |
| --- | ---: | ---: |
| `autovpn-controller-0.14.0-r1.apk` | 93,474 | 362,572 |
| `amneziawg-tools-1.0.20250903-r1.apk` | 29,404 | 62,591 |
| `kmod-amneziawg-6.12.94.1.0.20251004-r1.apk` | 40,948 | 110,133 |

The AWG pair contributes 172,724 bytes (168.676 KiB), excluding dependencies.
The earlier read-only space audit recorded 42,888 KiB free in `/overlay` and
33,358,560 bytes of missing official dependencies, including AWG crypto/UDP
dependencies. `libgcc1` was already installed. With the current controller,
AWG pair, 464,304-byte minimal zapret bundle and 171,210-byte incremental RU
database, total logical payload is **34,529,370 bytes (32.930 MiB)**. That leaves
8.953 MiB before filesystem overhead, configuration and operational reserve.
This is not post-install `df`: UBIFS compression, metadata and future updates
can change physical consumption. No fixed lifetime free-space guarantee is made.

Observed SDK issues fixed during this build:

- explicit `libgcc` dependency for the actual tools ELF;
- APK v3 `noarch` support in release preparation and the manual controller
  updater, while AWG binaries remain restricted to the exact target arch;
- duplicate nested recipe avoidance, limited kmod packaging, complete real
  library-provider metadata, and disabled auto-removal for source inspection.

The patched tools source was rebuilt with auto-removal disabled. Its stripped
`awg` matched the previously signed package byte-for-byte; the real parser
accepted the fixed AWG1 configuration, and its header contained UAPI version 2.
The native module and tools are AArch64; module vermagic and musl interpreter
matched the expected values below. The production `prepare-release.py`
metadata validator also passed on these real APKs.

Artifacts are kept outside Git in `build-artifacts/wr3000s-25.12.5-20260906/`:
`public/` contains APKs, public key and SHA256SUMS; `sources/` contains pinned
source archives; `audit/` contains logs/metadata; `private/` contains the signing
key at mode 0600 inside a 0700 directory. **Never publish `private/` or the whole
build-artifacts directory.** No GitHub release or upload was made.

The original VPS AutoVPN service retained its PID throughout the work. Build
jobs are stopped/completed; the separate build directory remains approximately
6.1 GiB, including the 4 GiB temporary swap, for subsequent inspection/removal.

## Inputs

- AutoVPN source commit: `bf0a1f2` (`openwrt/` only, exported with `git archive`).
- Export SHA256:
  `abdb5043ab04070924c35fab42c09416bb1297d9dae67e329cb4030d01ab938d`.
- Exact SDK and upstream source hashes:
  [build-inputs-25.12.5.md](build-inputs-25.12.5.md).
- Build host: Ubuntu 24.04 x86-64. The host kernel is not the target kernel;
  compiling here does not test module loading or VPN handshakes.
- Target: `aarch64_cortex-a53`, `mediatek/filogic`, OpenWrt `25.12.5`.
- Required installed kernel package:
  `6.12.94~5a6c1f71be683ae9980b15d3ce73e24d-r1`.

## SDK layout and package-only build

Extract the checksum-verified SDK into a fresh, dedicated build directory as a
non-root user. Keep its pinned `feeds.conf.default`; do not update to branch
heads. Update `base`, `packages` and `luci` feeds, then install the recipes for
the controller dependencies. No Go compiler, sing-box or firmware rebuild is
needed for these three custom packages.

Copy only `openwrt/Makefile`, `openwrt/LICENSE` and `openwrt/files/` into
`package/autovpn-controller/`. Copy each of the two `openwrt/packages/` recipe
directories directly into `package/` alongside it. **Do not copy the complete
OpenWrt subtree under `package/autovpn-controller/`**: nested package recipes
are also discovered by the SDK and create duplicate package definitions.

Use this configuration seed before `make -j1 defconfig`:

```text
# CONFIG_ALL is not set
# CONFIG_ALL_NONSHARED is not set
# CONFIG_ALL_KMODS is not set
# CONFIG_AUTOREBUILD is not set
# CONFIG_AUTOREMOVE is not set
CONFIG_SIGNED_PACKAGES=y
CONFIG_PACKAGE_kmod-amneziawg=m
CONFIG_PACKAGE_amneziawg-tools=m
CONFIG_PACKAGE_autovpn-controller=m
```

The SDK's `scripts/config` is a directory containing Kconfig programs, not a
Linux-style command accepting `--enable` or `--module`.

Build the SDK metadata for its prepared kernel modules before the external
module. In particular, the dependency checker needs the real `udp_tunnel.ko`
and `ip6_udp_tunnel.ko` provider records; do not invent `.provides` files or
disable that check.

```sh
make -j1 package/kernel/linux/compile V=s NO_DEPS=1
make -j1 package/toolchain/compile V=s NO_DEPS=1
make -j1 package/amneziawg/compile V=s NO_DEPS=1
make -j1 package/amneziawg-tools/compile V=s NO_DEPS=1
make -j1 package/autovpn-controller/compile V=s NO_DEPS=1
```

`NO_DEPS=1` here avoids rebuilding the controller's unchanged runtime packages,
which the installer obtains from official OpenWrt repositories. It does not
remove APK dependencies or bypass the kernel module's symbol/dependency checks.
It is appropriate only for this verified SDK and these recipes: the kernel is
already configured with its real `Module.symvers`; `awg` needs the bundled
musl toolchain; the controller packages interpreted sources and static assets.
The toolchain packaging step also supplies real libc/libgcc provider metadata.
The ARM64 `awg` ELF links `libgcc_s.so.1`, so the custom tools recipe explicitly
depends on `libgcc` (resolved as `libgcc1` by this SDK).

The fixed SDK configuration selects over a thousand kmod packages. To avoid
packing all of them on a small host, the recorded build passed empty
`CONFIG_PACKAGE_kmod-...=` command-line overrides for other selected kmods to
the `package/kernel/linux/compile` invocation, keeping only `udptunnel4`,
`udptunnel6`, `crypto-lib-chacha20`, `crypto-lib-poly1305`,
`crypto-lib-chacha20poly1305` and `crypto-lib-curve25519` enabled. This changes
which prepared modules are packaged, not their kernel configuration. The
prepared kernel `.config` SHA256 was checked unchanged before/after that step.
Keep `CONFIG_AUTOREMOVE` disabled for inspection: otherwise the SDK removes
compiled sources after packaging. Compare the stripped binary under `ipkg-*`
with the extracted APK, not the earlier unstripped `.pkgdir` binary.

## Signing and validation

Use a private P-256 APK signing key outside Git and never include it in public
artifacts. `CONFIG_SIGNED_PACKAGES=y` alone did not add a signature to these
individual SDK `mkpkg` outputs. Sign copies explicitly with SDK-host `apk`:

```sh
apk adbsign --allow-untrusted --reset-signatures --sign-key /private/path/private-key.pem package.apk
apk verify --keys-dir /directory/containing/only/public-key package.apk
apk adbdump --format json package.apk
```

Here `apk` means the verified SDK's `staging_dir/host/bin/apk`, not Ubuntu's
package manager. `--allow-untrusted` is used only as input permission when
signing our own initially unsigned SDK output; it is never used for verification
or installation. Sign a fresh copy: this SDK's `adbsign` can log an error yet
return zero and leave a partial output. The following strict `apk verify` is
therefore mandatory and must succeed independently.
Verify all three names, architecture (`noarch` for the controller), controller
version, and the kmod's exact
kernel dependency. Extract with `apk extract` into a new inspection directory;
do not install the package or execute its scripts on the build host.

Check both ELF files are AArch64, the tools use musl, and the module reports
`vermagic=6.12.94 SMP mod_unload aarch64`. Confirm the built tools header says
`WG_GENL_VERSION 2`, no patch rejects exist, and run the native parser check
against the SDK's actually patched tools source. These checks still do not
replace target `awg setconf`, handshake, routing or fail-closed tests.

## Build-host isolation and removal

The VPS build uses a dedicated `autovpn-build` system account, no sudo, a
separate `/srv/autovpn-build-20260905` directory and transient systemd units.
The units have one build thread, `CPUQuota=60%`, `MemoryHigh=320M`,
`MemoryMax=448M`, `MemorySwapMax=3G`, `Nice=19`, `IOWeight=10`, and a four-hour
timeout. `ProtectSystem=strict`, `ProtectHome=yes`, `NoNewPrivileges=yes` and
an inaccessible site directory limit accidental access. This is process/file
isolation, not a VM or a hard security boundary against a malicious compiler.

A separate 4 GiB swap file was enabled without modifying `/etc/fstab`.
To remove the environment later: first stop any exact remaining build units,
save verified artifacts and the private signing key, and inspect RAM/swap use.
Only disable this exact swap file when memory pressure permits it. Then remove
the dedicated build account and this exact build directory. Do not run
`swapoff -a`, delete the site account, or recursively target a workspace/home.
Host dependencies were installed without upgrades; package inventory was saved
before and after so any later removal can be limited to the newly added tools.

Nothing in this workflow authorizes GitHub publication or router changes.
