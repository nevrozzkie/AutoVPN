# AmneziaWG runtime

`autovpn-controller` consumes only snapshot profile version 1. It uses the
kernel AmneziaWG implementation; it never translates the profile to ordinary
WireGuard and never falls back to a plaintext/direct route.

## Required target packages

Install a kernel module and tools which provide both `amneziawg` link type and
the `awg` binary for the exact OpenWrt kernel ABI. On an ImmortalWrt-style feed
these are normally `kmod-amneziawg` and `amneziawg-tools`. They are deliberately
optional: without them, VLESS and Hysteria2 continue to work and AWG is simply
not offered to automatic selection.

The package Makefile installs `awg-helper.uc` as an executable. A target
image should also include `kmod-amneziawg` and `amneziawg-tools` when AWG is
wanted; they must not be unconditional dependencies of the base controller.

## Routing and lifecycle

When the local helper finds executable `awg` and a loadable kernel module, an enabled snapshot AWG profile can join
`auto`, or be selected manually as `amneziawg`.

| item | value |
| --- | --- |
| kernel device | `avpnwg0` |
| MTU | `1380` |
| sing-box transport | `direct`, bound to `avpnwg0`, mark `20193` |
| AWG encrypted UDP socket mark | `20194` |
| AWG policy table / rule | `20193` / priority `20193` |
| no-fallback rule | priority `20194`, `unreachable` |

Thus sing-box sends plaintext into `avpnwg0`; the AWG kernel socket has a
different mark and exits via the normal WAN routing table. This avoids a route
loop while retaining the existing `avpn0` TUN, DNS interception and client
kill switch.

When a later profile does not use AWG, the controller deliberately leaves its
exact owned `fwmark 20193` lookup and unreachable rules plus table route in
place. Nothing is marked `20193` without the AWG outbound, while retaining the
unreachable fallback prevents an accidental marked packet from falling back to
the WAN. A future AWG activation validates those owned entries before reuse and
refuses a collision instead of flushing a shared routing table.

The controller owns this kernel interface directly; it does not create a second
netifd AWG configuration. Profiles requiring AWG2 I-fields are not accepted by the
current v1 snapshot contract. An available AWG candidate is prepared even with a
manually selected VLESS/Hysteria profile, so Ping all can inspect it without
switching the client network. A failed unused AWG startup removes that optional
candidate from the runtime; it does not prevent VLESS/Hysteria startup. Steady
runtime reuse only requires AWG health when AWG is actually selected.

Before changing a profile the adapter closes forwarding and stops the TUN.
`awg-helper.uc` writes `/etc/autovpn/runtime/awg.conf` at mode `0600`, applies
it with `awg setconf`. An ownership intent is recorded before creating the link
with its alias in the same netlink request. It only removes an
`avpnwg0` it created itself. A failure to create/configure AWG leaves the
managed Wi-Fi fail-closed; it does not select `direct` instead. The generated
AWG JSON/config are root-only files and keys are never command-line arguments
or log output.

## Hysteria2 note

Hysteria2 remains QUIC over TLS. The `subscription` TLS mode preserves the
server-provided `tls.insecure=true`, meaning certificate verification is
disabled for that subscription; `strict` rejects such Hysteria profiles. This
is a trust decision, not a TLS-free mode.

## Validation boundary

Host tests exercise rendering and command/secret/failure boundaries. They do not
execute the AmneziaWG kernel, nftables or real packet routing. The real macOS
sing-box check covers VLESS/Hysteria configuration, not Linux-only `routing_mark`.
Before deployment, build the exact OpenWrt image and test AWG handshakes, DNS,
fail-closed, reboot and rollback on the device.

Primary sources: [AWG config parser](https://github.com/amnezia-vpn/amneziawg-tools/blob/master/src/config.c),
[kernel device](https://github.com/amnezia-vpn/amneziawg-linux-kernel-module/blob/master/src/device.c),
[iproute2 link parser](https://github.com/iproute2/iproute2/blob/main/ip/iplink.c),
[Hysteria TLS](https://v2.hysteria.network/docs/advanced/Full-Client-Config/#tls).
