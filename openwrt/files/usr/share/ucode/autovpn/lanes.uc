'use strict';

/* Pure, fixed resource registry shared by runtime, probes and validation. */
const IDS = ['vpn', 'vpn_zapret'];
const PROFILES = ['vless-reality', 'hysteria2', 'amneziawg'];
const DESCRIPTORS = [
	{
		id: 'vpn',
		root: '/etc/autovpn/runtime',
		health_dir: '/var/run/autovpn-health',
		tun: 'avpn0',
		bridge: 'br-avpn',
		address: '172.30.255.1/30',
		dns: '172.30.255.2',
		health_port: 1088,
		transparent: { listen_port: 12080, mark: 20180, route_table: 20180, priority: 20180 },
		probe_ports: { 'vless-reality': 1089, hysteria2: 1090, amneziawg: 1091 },
		route_table: 20191,
		priorities: { route: 20191, unreachable: 20192 },
		awg: { device: 'avpnwg0', inner_mark: 20193, outer_mark: 20194 },
		profiles: PROFILES,
	},
	{
		id: 'vpn_zapret',
		root: '/etc/autovpn/runtime-zapret',
		health_dir: '/var/run/autovpn-health-zapret',
		tun: 'avpn1',
		bridge: 'br-avpnz',
		address: '172.30.255.5/30',
		dns: '172.30.255.6',
		health_port: 1108,
		transparent: { listen_port: 12081, mark: 20200, route_table: 20200, priority: 20200 },
		probe_ports: { 'vless-reality': 1109, hysteria2: 1110, amneziawg: 1111 },
		route_table: 20211,
		priorities: { route: 20211, unreachable: 20212 },
		awg: { device: 'avpnwg1', inner_mark: 20213, outer_mark: 20214 },
		profiles: PROFILES,
	},
];

function copy(value) { return json(sprintf('%J', value)); }

function get(id) {
	let position = index(IDS, id);
	return position < 0 ? null : copy(DESCRIPTORS[position]);
}

function key(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9+\/]{43}=$/) != null;
}

function address(value) {
	if (type(value) != 'string' || match(value, /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/32$/) == null)
		return false;
	let parts = split(split(value, '/')[0], '.');
	for (let i = 0; i < 4; i++)
		if (int(parts[i]) > 255 || (length(parts[i]) > 1 && substr(parts[i], 0, 1) == '0'))
			return false;
	return true;
}

function identity(profile) {
	if (profile == null) return null;
	if (type(profile) != 'object' || type(profile.interface) != 'object' ||
		!key(profile.interface.private_key) || !address(profile.interface.address))
		return false;
	return { private_key: profile.interface.private_key, address: profile.interface.address };
}

function validatePair(primaryAwg, secondaryAwg) {
	let primary = identity(primaryAwg);
	let secondary = identity(secondaryAwg);
	if (primary === false || secondary === false) return { ok: false, code: 'invalid_awg_lane_profile' };
	if (primary == null || secondary == null) return { ok: true };
	if (primary.private_key == secondary.private_key) return { ok: false, code: 'shared_awg_private_key' };
	if (primary.address == secondary.address) return { ok: false, code: 'shared_awg_address' };
	return { ok: true };
}

return { ids: copy(IDS), get: get, validatePair: validatePair };
