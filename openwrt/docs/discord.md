# Discord and WebRTC for direct zapret

These settings affect only the direct guarded Wi-Fi `-з` (`br-avpndz`). They
do not change the normal VPN `-в`, VPN+zapret `-вз`, VPN server transport, or
the existing per-protocol VPN zapret strategies.

## Settings

In **Services → AutoVPN → Settings → Direct zapret**:

- **Discord media discovery** enables the pinned official zapret2 upstream
  `50-discord-media` profile for media traffic on `-з`.
- **WebRTC STUN** enables the pinned official `50-stun4all` profile. STUN is also used by
  other WebRTC-capable applications, not only Discord.
- **Discord / STUN strategy** accepts only `fake` (the upstream zero-payload
  fake packets) or `fake_badsum` (an experimental fake packet with an invalid
  checksum).
- **Discord / STUN fake repeats** is restricted to integers 1–6; the default is 2.

There is no field for a command line, Lua, custom profile, host list, or an
arbitrary upstream asset. This is deliberately a small allowlist, not a
general-purpose zapret editor.

Fresh installations persist both feature flags as enabled. For an older
configuration where either flag is absent, compatibility mode keeps that
feature disabled; the UI initially shows it off. The strategy and repeat count
still default to `fake` and `2` when absent, but have no effect while both
media features are disabled.

Changes are saved in UCI. The independent direct-zapret tick observes them
within 30 seconds, temporarily closes `-з` forwarding, restarts its one
`nfqws2` process, and reopens forwarding only after its queue is ready. It does
not restart either VPN runtime. A failed validation or process start leaves
`-з` closed rather than allowing an unprocessed direct WAN path.

## Engine boundary and validation

The router installer pins zapret2 **v1.0.5** from its official upstream release.
The Discord media and STUN profiles run in the same direct-zapret `nfqws2`
process and NFQUEUE as the other direct `-з` processing; they do not create
separate background processes or alter the VPN-zapret queue.

This configuration only constrains local processing. It is not a guarantee that
Discord calls, voice, video, screen sharing, or WebRTC will bypass a particular
provider's DPI. Acceptance needs manual real-call testing on the target router;
no such hardware/network test is claimed here.

The upstream recipes are pinned, not copied from Windows batch files:
[Discord discovery](https://github.com/bol-van/zapret2/blob/v1.0.5/init.d/custom.d.examples.linux/50-discord-media)
and [STUN](https://github.com/bol-van/zapret2/blob/v1.0.5/init.d/custom.d.examples.linux/50-stun4all).
Only matching outbound IPv4 discovery/negotiation packets are queued. Discord
discovery uses UDP ports 50000–50099 and 19294–19344; STUN uses any UDP port.
The normal encrypted voice/video stream is not queued by these media rules.
Discord can advertise other media ports, so the fixed upstream discovery ranges
are not a guarantee of universal coverage. STUN on UDP/443 is not queued twice.

Before declaring a deployment working, verify the generated nft rules and
nfqws2 configuration on the target OpenWrt, then test native and browser Discord:
voice joining/rejoining, two-way audio, video, and Go Live/screen sharing. Repeat
after changing each toggle, and check an unrelated WebRTC app with STUN enabled.
