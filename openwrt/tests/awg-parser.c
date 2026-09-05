/*
 * Native parser regression for the fixed AWG1-on-UAPI2 config fragment.
 * Build this only against the pinned amneziawg-tools source supplied by the
 * release workflow; it neither opens a netlink socket nor needs a device.
 */
#include <stdio.h>

#include "config.h"
#include "containers.h"

static int fail(const char *message)
{
	fprintf(stderr, "awg parser regression: %s\n", message);
	return 1;
}

int main(void)
{
	static const char *const fixture[] = {
		"[Interface]",
		"PrivateKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		"Jc = 3",
		"Jmin = 20",
		"Jmax = 700",
		"S1 = 30",
		"S2 = 30",
		"S3 = 0",
		"S4 = 0",
		"H1 = 1",
		"H2 = 2",
		"H3 = 3",
		"H4 = 4",
		"",
		"[Peer]",
		"PublicKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		"PresharedKey = AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
		"AdvancedSecurity = on",
		"AllowedIPs = 0.0.0.0/0",
		"Endpoint = 203.0.113.9:51820",
		"PersistentKeepalive = 25",
	};
	struct config_ctx context;
	struct wgdevice *device;
	struct wgpeer *peer;
	size_t index;

	if (!config_read_init(&context, false))
		return fail("config initialization failed");
	for (index = 0; index < sizeof(fixture) / sizeof(fixture[0]); ++index) {
		if (!config_read_line(&context, fixture[index]))
			return fail("fixed config was rejected");
	}
	device = config_read_finish(&context);
	if (device == NULL)
		return fail("fixed config did not finish");
	peer = device->first_peer;
	if ((device->flags & WGDEVICE_HAS_S3) == 0 ||
		(device->flags & WGDEVICE_HAS_S4) == 0 ||
		device->cookie_reply_packet_junk_size != 0 ||
		device->transport_packet_junk_size != 0)
		return fail("S3/S4 zero fields were not retained");
	if ((device->flags & (WGDEVICE_HAS_I1 | WGDEVICE_HAS_I2 | WGDEVICE_HAS_I3 |
		WGDEVICE_HAS_I4 | WGDEVICE_HAS_I5)) != 0 || device->i1 != NULL ||
		device->i2 != NULL || device->i3 != NULL || device->i4 != NULL ||
		device->i5 != NULL)
		return fail("I fields appeared in the fixed config");
	if (peer == NULL || peer->next_peer != NULL ||
		(peer->flags & WGPEER_HAS_AWG) == 0 || !peer->awg) {
		free_wgdevice(device);
		return fail("AdvancedSecurity was not enabled for the peer");
	}
	free_wgdevice(device);
	return 0;
}
