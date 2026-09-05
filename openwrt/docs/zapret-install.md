# Pinned zapret2 engine installation

The released one-command AutoVPN installer queues the pinned zapret2 engine after
the signed controller APK and official OpenWrt dependencies have installed. The
engine is fetched directly from the official upstream release, not from the
AutoVPN site, and no upstream install script is executed. The LuCI button can
manually check or re-run that same bounded installation; it never discovers a
new version or enables automatic updates.

Queueing returns immediately; a private copy of the helper performs the bounded
download in the background. A failed engine installation must leave guarded
`x-з` and `x-вз` traffic closed rather than turning it into an unprocessed WAN
bypass.

The helper is intentionally pinned to the official
[`bol-van/zapret2` v1.0.5 release](https://github.com/bol-van/zapret2/releases/tag/v1.0.5):

- asset: `zapret2-v1.0.5-openwrt-embedded.tar.gz`;
- asset SHA-256: `40040fef1747012a68f2dd5892b9a0bece91846e9bce37b59e35b641fdcb2a4e`;
- architecture: Linux `aarch64` only;
- selected members: `binaries/linux-arm64/nfqws2`,
  `lua/zapret-lib.lua.gz`, and `lua/zapret-antidpi.lua.gz`;
- license: `docs/LICENSE.txt` from the exact `v1.0.5` tag, independently pinned
  by SHA-256.

The full archive SHA-256 is checked before extraction. Only the three named
members are extracted, the Lua files are decompressed, and every resulting file
is checked against its own embedded SHA-256. Upstream installation scripts are
never extracted or executed. Both source URLs are fixed HTTPS allowlist entries;
there is no URL, version, path, or checksum override in the CLI or environment.

Before downloading, the helper requires root, `aarch64`, at least 7 MiB free in
`/tmp`, 2 MiB free on the target filesystem, and 16 MiB reported as available
memory. Downloads are capped at 5 MiB for the archive and 16 KiB for the
license, with respective 120 and 30 second deadlines. Redirects are restricted
to HTTPS.

The completed files appear atomically in `/usr/lib/autovpn-zapret`:

- `nfqws2` (mode 0755);
- `zapret-lib.lua` and `zapret-antidpi.lua` (mode 0644);
- `LICENSE.txt` and a non-secret `receipt.json` (mode 0644).

An existing directory is accepted only when its receipt and all four payload
hashes exactly match this pinned bundle. A foreign directory, symlink, partial
installation, or modified file is rejected and never replaced. Download,
checksum, extraction, and versioned staging failures leave the prior state
untouched.

The CLI is deliberately small and writes one sanitized JSON object to stdout:

```text
/usr/libexec/autovpn/zapret-install status
/usr/libexec/autovpn/zapret-install queue
/usr/libexec/autovpn/zapret-install install
```

`install` is the synchronous first-installer action: it holds the same exclusive
job lock and returns only after verification and atomic publication. It is
idempotent for the exact receipt-verified bundle.

`queue` is the RPC-facing action. It atomically claims a root-only job directory
under `/tmp`, records `queued`, then the copied worker records `running` and
finally `ready` or a bounded failure code. `status` reconstructs JSON only from
validated state tokens and bundle hashes; it never reflects raw file contents.
The completed status is retained in RAM while the worker and its lock directory
are removed. A second queue request cannot start a concurrent job. The internal
`worker` action accepts only the root-owned helper copy at the exact claimed job
path. Neither action changes a running service, and installation is rejected
while either AutoVPN zapret service reports itself active (an already verified
bundle remains a read-only no-op).

There is no update command or background check. Moving to another zapret2
version requires review and a new AutoVPN release with updated pins and tests.
