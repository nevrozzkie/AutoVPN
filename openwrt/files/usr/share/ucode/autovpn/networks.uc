'use strict';

/* A bounded UCI plan. Never import server data or alter the management LAN. */
const OWNER = 'ssid-v1';
const CONFIGS = ['network', 'wireless', 'dhcp', 'firewall'];
const MODES = [
	{ id: 'direct', bridge: 'br-avpnd', octet: 29, suffix: '', enabled: true },
	{ id: 'vpn', bridge: 'br-avpn', octet: 30, suffix: '-VPN', enabled: true },
	{ id: 'zapret', bridge: 'br-avpndz', octet: 31, suffix: '-ZAPRET', enabled: false },
	{ id: 'vpn_zapret', bridge: 'br-avpnz', octet: 32, suffix: '-VPN-ZAPRET', enabled: false },
];
function fail(code) { return { ok: false, code: code }; }
function list(value) { return type(value) == 'array' ? value : type(value) == 'string' ? split(value, ' ') : []; }
function validSettings(settings) {
	if (type(settings) != 'object' || type(settings.base_ssid) != 'string' ||
		length(settings.base_ssid) == 0 || length(settings.base_ssid) > 21 ||
		index(settings.base_ssid, '\x00') >= 0 ||
		match(settings.base_ssid, /[\x01-\x1f\x7f]/) != null) return fail('invalid_base_ssid');
	/* WPA2 passphrase: printable ASCII 8..63, or an exact 256-bit hexadecimal PSK. */
	if (type(settings.password) != 'string' ||
		(match(settings.password, /^[\x20-\x7e]{8,63}$/) == null &&
		 match(settings.password, /^[0-9a-fA-F]{64}$/) == null)) return fail('invalid_wifi_password');
	return { ok: true };
}
function ipNumber(value) {
	if (type(value) != 'string' || match(value, /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) == null) return null;
	let parts = split(value, '.');
	let number = 0;
	for (let i = 0; i < 4; i++) {
		if (int(parts[i]) > 255 || (length(parts[i]) > 1 && substr(parts[i], 0, 1) == '0')) return null;
		number = number * 256 + int(parts[i]);
	}
	return number;
}
function range(value, mask) {
	let parts = split(value, '/');
	let address = ipNumber(parts[0]);
	if (address == null || length(parts) > 2) return null;
	let bits = 32;
	if (length(parts) == 2) {
		if (match(parts[1], /^(0|[1-9][0-9]?)$/) == null || int(parts[1]) > 32) return null;
		bits = int(parts[1]);
	}
	else if (mask != null) {
		let number = ipNumber(mask);
		if (number == null) return null;
		let host = 4294967295 - number;
		bits = 32;
		while (host > 0 && host % 2 == 1) { bits--; host = int(host / 2); }
		if (host != 0) return null;
	}
	let size = 1;
	for (let i = bits; i < 32; i++) size *= 2;
	let start = address - address % size;
	return { start: start, end: start + size - 1 };
}
function overlaps(value, mask) {
	let target = range(value, mask);
	if (target == null) return true; /* Unknown static config requires manual review. */
	for (let i = 0; i < length(MODES); i++) {
		let own = range('192.168.' + MODES[i].octet + '.0/24', null);
		if (target.start <= own.end && own.start <= target.end) return true;
	}
	let tun = range('172.30.255.0/30', null);
	return target.start <= tun.end && tun.start <= target.end;
}
function add(plan, config, name, sectionType, values) {
	values.autovpn_owner = OWNER;
	push(plan.sections, { config: config, name: name, section_type: sectionType, values: values });
}
function plan(settings, configs, routes) {
	let checked = validSettings(settings);
	if (!checked.ok) return checked;
	let radios = [];
	let ownedBridges = [];
	let wanZones = 0;
	let result = { ok: true, sections: [], remove: [], ssids: [], radios: [] };
	for (let c = 0; c < length(CONFIGS); c++) {
		let name = CONFIGS[c];
		if (type(configs[name]) != 'array') return fail('network_config_unavailable');
		for (let i = 0; i < length(configs[name]); i++) {
			let section = configs[name][i];
			let owned = section.autovpn_owner == OWNER;
			if (owned) {
				if (match(section['.name'], /^avpn_[A-Za-z0-9_]+$/) == null) return fail('network_ownership_conflict');
				push(result.remove, { config: name, name: section['.name'] });
				if (name == 'network' && section['.type'] == 'device') push(ownedBridges, section.name);
				continue;
			}
			if (match(section['.name'], /^avpn_/) != null) return fail('network_ownership_conflict');
			if (name == 'wireless' && section['.type'] == 'wifi-device' && section.disabled != '1' &&
				index(['2g', '5g'], section.band) >= 0) {
				if (match(section['.name'], /^[A-Za-z0-9_]{1,32}$/) == null) return fail('invalid_radio_name');
				push(radios, section['.name']);
			}
			if (name == 'network') {
				for (let m = 0; m < length(MODES); m++)
					if (section.name == MODES[m].bridge || section.device == MODES[m].bridge ||
						index(list(section.ports), MODES[m].bridge) >= 0) return fail('network_ownership_conflict');
				let addresses = list(section.ipaddr);
				for (let a = 0; a < length(addresses); a++)
					if (overlaps(addresses[a], section.netmask)) return fail('network_subnet_conflict');
			}
			if (name == 'firewall') {
				if (section['.type'] == 'defaults' && (section.flow_offloading == '1' || section.flow_offloading_hw == '1'))
					return fail('disable_flow_offloading');
				if (section['.type'] == 'zone' && section.name == 'wan' && section.masq == '1') wanZones++;
				if (section['.type'] == 'zone' && match(section.name || '', /^avpn_/) != null)
					return fail('network_ownership_conflict');
			}
			/* Never steal an interface already referenced by an unrelated section. */
			let references = list(section.network);
			for (let r = 0; r < length(references); r++)
				if (match(references[r], /^avpn_/) != null) return fail('network_ownership_conflict');
		}
	}
	if (length(radios) == 0 || length(radios) > 4) return fail('enabled_wifi_radio_required');
	if (wanZones != 1) return fail('masquerading_wan_zone_required');
	if (type(routes) != 'array') return fail('network_routes_unavailable');
	for (let i = 0; i < length(routes); i++) {
		let route = routes[i];
		if (route.dst == 'default' || route.dst == '0.0.0.0/0') continue;
		if (index(ownedBridges, route.dev) >= 0 &&
			index(['192.168.29.0/24', '192.168.30.0/24', '192.168.31.0/24', '192.168.32.0/24'], route.dst) >= 0) continue;
		if (route.dev == 'avpn0' && route.dst == '172.30.255.0/30') continue;
		if (type(route.dst) != 'string' || overlaps(route.dst, null)) return fail('network_subnet_conflict');
	}
	for (let m = 0; m < length(MODES); m++) {
		let mode = MODES[m];
		let name = 'avpn_' + mode.id;
		let ssid = settings.base_ssid + mode.suffix;
		push(result.ssids, { ssid: ssid, enabled: mode.enabled, mode: mode.id });
		add(result, 'network', name + '_bridge', 'device', { name: mode.bridge, type: 'bridge', bridge_empty: '1', ipv6: '0' });
		add(result, 'network', name, 'interface', { device: mode.bridge, proto: 'static',
			ipaddr: '192.168.' + mode.octet + '.1', netmask: '255.255.255.0', delegate: '0' });
		add(result, 'dhcp', name, 'dhcp', { interface: name, start: '100', limit: '100', leasetime: '12h',
			ra: 'disabled', dhcpv6: 'disabled', ndp: 'disabled', ignore: mode.enabled ? '0' : '1' });
		add(result, 'firewall', name, 'zone', { name: name, network: [name], input: 'REJECT', output: 'ACCEPT', forward: 'REJECT' });
		if (mode.enabled)
			add(result, 'firewall', name + '_dhcp', 'rule', { name: name + '-DHCP', src: name,
				proto: 'udp', src_port: '68', dest_port: '67', family: 'ipv4', target: 'ACCEPT' });
		if (mode.id == 'direct') {
			add(result, 'firewall', name + '_dns', 'rule', { name: name + '-DNS', src: name,
				proto: ['tcp', 'udp'], dest_port: '53', family: 'ipv4', target: 'ACCEPT' });
			add(result, 'firewall', name + '_wan', 'forwarding', { src: name, dest: 'wan', family: 'ipv4' });
		}
		for (let r = 0; r < length(radios); r++) {
			for (let w = 0; w < length(configs.wireless); w++)
				if (configs.wireless[w].autovpn_owner != OWNER && configs.wireless[w].ssid == ssid)
					return fail('ssid_already_exists');
			add(result, 'wireless', name + '_' + radios[r], 'wifi-iface', { device: radios[r], mode: 'ap',
				network: [name], ssid: ssid, encryption: 'psk2+ccmp', key: settings.password,
				isolate: '1', disabled: mode.enabled ? '0' : '1' });
		}
	}
	result.radios = radios;
	return result;
}

return { plan: plan, validSettings: validSettings, configs: CONFIGS };
