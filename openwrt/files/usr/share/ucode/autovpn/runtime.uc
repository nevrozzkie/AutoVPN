'use strict';

/* Pure renderer. No server-supplied shell, routes, listeners or global config. */
function fail(code) { return { ok: false, code: code }; }
function copy(value) { return json(sprintf('%J', value)); }
function ipv4(value) {
	if (type(value) != 'string' || match(value, /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$/) == null)
		return false;
	let parts = split(value, '.');
	for (let i = 0; i < 4; i++)
		if (int(parts[i]) > 255 || (length(parts[i]) > 1 && substr(parts[i], 0, 1) == '0'))
			return false;
	return true;
}
function cidr(value) {
	if (type(value) != 'string') return false;
	let parts = split(value, '/');
	return length(parts) == 2 && ipv4(parts[0]) &&
		match(parts[1], /^(0|[1-9][0-9]?)$/) != null && int(parts[1]) <= 32;
}
function domain(value) {
	if (type(value) != 'string' || length(value) > 253 || length(value) == 0) return false;
	let labels = split(value, '.');
	for (let i = 0; i < length(labels); i++)
		if (match(labels[i], /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/) == null) return false;
	return !ipv4(value);
}
/* A WireGuard/AmneziaWG key is a 32-byte value encoded as canonical base64. */
function awgKey(value) {
	return type(value) == 'string' &&
		match(value, /^[A-Za-z0-9+\/]{43}=$/) != null;
}
function awgAddress(value) {
	if (!cidr(value)) return false;
	let parts = split(value, '/');
	/* Router snapshots deliberately issue one /32 per client. */
	if (parts[1] != '32') return false;
	let number = 0;
	let address = split(parts[0], '.');
	for (let i = 0; i < 4; i++) number = number * 256 + int(address[i]);
	/* Do not let a server profile overlap managed Wi-Fi or the sing-box TUN. */
	let forbidden = [
		[192 * 256 * 256 * 256 + 168 * 256 * 256 + 29 * 256, 256],
		[192 * 256 * 256 * 256 + 168 * 256 * 256 + 30 * 256, 256],
		[192 * 256 * 256 * 256 + 168 * 256 * 256 + 31 * 256, 256],
		[192 * 256 * 256 * 256 + 168 * 256 * 256 + 32 * 256, 256],
		[172 * 256 * 256 * 256 + 30 * 256 * 256 + 255 * 256, 8],
	];
	for (let i = 0; i < length(forbidden); i++)
		if (number >= forbidden[i][0] && number < forbidden[i][0] + forbidden[i][1]) return false;
	return true;
}
/* Accept old persisted policy bundles as-is. New helper output has all fields. */
function normalizedPolicy(policy) {
	/* Absent in persisted 0.11 policies: never silently enable it on restore. */
	let fields = copy(policy);
	delete fields.ru_bypass;
	let old = sort(['selection', 'wan_device', 'dns_server', 'direct_domains', 'direct_cidrs']);
	let current = sort(['selection', 'wan_device', 'dns_server', 'direct_domains', 'direct_cidrs',
		'hysteria_tls_mode', 'awg_available']);
	let names = sort(keys(fields));
	let withZapret = sort(['selection', 'wan_device', 'dns_server', 'direct_domains', 'direct_cidrs',
		'hysteria_tls_mode', 'awg_available', 'zapret']);
	if (join(',', names) != join(',', old) && join(',', names) != join(',', current) &&
		join(',', names) != join(',', withZapret)) return null;
	let result = copy(policy);
	/* Old 0.4 bundles used strict Hysteria validation. Do not reinterpret them. */
	if (result.hysteria_tls_mode == null) result.hysteria_tls_mode = 'strict';
	if (result.awg_available == null) result.awg_available = false;
	return result;
}
function validatePolicy(policy) {
	if (type(policy) != 'object') return fail('invalid_policy');
	let normalized = normalizedPolicy(policy);
	if (normalized == null) return fail('invalid_policy');
	if (index(keys(policy), 'ru_bypass') >= 0 && type(policy.ru_bypass) != 'bool')
		return fail('invalid_ru_bypass');
	if (normalized.zapret != null && !require('autovpn.zapret').validPolicy(normalized.zapret))
		return fail('invalid_zapret_policy');
	if (index(['auto', 'vless-reality', 'hysteria2', 'amneziawg'], normalized.selection) < 0)
		return fail('unsupported_selection');
	if (type(normalized.wan_device) != 'string' ||
		match(normalized.wan_device, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/) == null ||
		index(['lo', 'avpn0', 'avpn1', 'avpnwg0', 'avpnwg1', 'br-avpn', 'br-avpnz',
			'br-avpnd', 'br-avpndz', 'br-lan'], normalized.wan_device) >= 0)
		return fail('invalid_wan_device');
	if (!ipv4(normalized.dns_server)) return fail('invalid_dns_server');
	if (index(['subscription', 'strict'], normalized.hysteria_tls_mode) < 0 ||
		type(normalized.awg_available) != 'bool') return fail('invalid_runtime_mode');
	if (type(normalized.direct_domains) != 'array' || length(normalized.direct_domains) > 128 ||
		type(normalized.direct_cidrs) != 'array' || length(normalized.direct_cidrs) > 128)
		return fail('invalid_direct_rules');
	for (let i = 0; i < length(normalized.direct_domains); i++)
		if (!domain(normalized.direct_domains[i])) return fail('invalid_direct_domain');
	for (let i = 0; i < length(normalized.direct_cidrs); i++)
		if (!cidr(normalized.direct_cidrs[i])) return fail('invalid_direct_cidr');
	return { ok: true };
}

function validateAwg(profile) {
	if (type(profile) != 'object' || type(profile.interface) != 'object' || type(profile.peer) != 'object' ||
		!awgKey(profile.interface.private_key) ||
		!awgAddress(profile.interface.address) || !awgKey(profile.peer.public_key) ||
		!awgKey(profile.peer.preshared_key)) return false;
	let small = ['Jc', 'Jmin', 'Jmax', 'S1', 'S2'];
	for (let i = 0; i < length(small); i++)
		if (profile.obfuscation[small[i]] > 65535) return false;
	if (profile.obfuscation.Jmin > profile.obfuscation.Jmax) return false;
	/* state.uc has already checked every key and every obfuscation integer. */
	return true;
}

function laneDescriptor(snapshot, lane) {
	if (lane == null) return null;
	let descriptor = require('autovpn.lanes').get(lane);
	if (descriptor == null) return false;
	if (descriptor.id == 'vpn_zapret' && snapshot.schema_version < 4)
		return { empty: true, id: descriptor.id };
	return descriptor;
}

/* Kept byte-for-byte compatible with version 1 bundle validation. */
function renderLegacy(snapshot, policy, machine) {
	if (!machine.validateSnapshot(snapshot).ok) return fail('snapshot_validation_failed');
	let validated = validatePolicy(policy);
	if (!validated.ok) return validated;
	let effective = normalizedPolicy(policy);
	/* Avoid WAN DNS bootstrap dependencies/cycles; current AutoVPN publishes IPs. */
	if (!ipv4(snapshot.server.endpoint)) return fail('endpoint_ipv4_required');
	let outbounds = [];
	let candidates = [];
	let capabilities = machine.normalizeCapabilities({});
	for (let i = 0; i < 2; i++) {
		let name = ['vless', 'hysteria2'][i];
		let slot = snapshot.protocols[name];
		if (!slot.enabled) continue;
		if (name == 'hysteria2' && slot.outbound.tls.insecure && effective.hysteria_tls_mode == 'strict') {
			if (effective.selection == 'hysteria2') return fail('hysteria_tls_unverified');
			continue;
		}
		let outbound = copy(slot.outbound);
		outbound.bind_interface = effective.wan_device;
		push(outbounds, outbound);
		push(candidates, outbound.tag);
		capabilities[name] = true;
	}
	let awg = null;
	let awgSlot = snapshot.protocols.amneziawg;
	if (awgSlot.enabled && effective.awg_available && validateAwg(awgSlot.profile) &&
		index(['auto', 'amneziawg'], effective.selection) >= 0) {
		awg = copy(awgSlot.profile);
		push(outbounds, { type: 'direct', tag: 'amneziawg', bind_interface: 'avpnwg0', routing_mark: 20193 });
		push(candidates, 'amneziawg');
		capabilities.amneziawg = true;
	}
	if (length(candidates) == 0) return fail('no_supported_vpn');
	if (effective.selection != 'auto' && index(candidates, effective.selection) < 0)
		return fail('selected_vpn_unavailable');
	let selected = effective.selection;
	if (selected == 'auto') {
		push(outbounds, {
			type: 'urltest', tag: 'auto', outbounds: candidates,
			url: 'https://www.gstatic.com/generate_204', interval: '1m',
			tolerance: 50, interrupt_exist_connections: false,
		});
	}
	push(outbounds, { type: 'direct', tag: 'direct', bind_interface: effective.wan_device });
	/* The health listener MUST bypass direct exceptions, including 0.0.0.0/0. */
	let rules = [
		{ inbound: ['health'], action: 'route', outbound: selected },
		{ port: 53, action: 'hijack-dns' },
		{ action: 'sniff', timeout: '300ms' },
		{ ip_version: 6, action: 'reject' },
		{ ip_is_private: true, action: 'reject' },
	];
	if (length(effective.direct_domains))
		push(rules, { domain_suffix: effective.direct_domains, action: 'route', outbound: 'direct' });
	if (length(effective.direct_cidrs))
		push(rules, { ip_cidr: effective.direct_cidrs, action: 'route', outbound: 'direct' });
	capabilities.policy_routing = true;
	return {
		ok: true, profile: selected, capabilities: capabilities, awg: awg,
		config: {
			log: { disabled: true },
			dns: {
				servers: [{ type: 'udp', tag: 'tunnel-dns', server: policy.dns_server, server_port: 53, detour: selected }],
				final: 'tunnel-dns', strategy: 'ipv4_only', reverse_mapping: true,
			},
			inbounds: [
				{ type: 'tproxy', tag: 'vpn-net', listen: '0.0.0.0', listen_port: 12080 },
				{ type: 'socks', tag: 'health', listen: '127.0.0.1', listen_port: 1088 },
			],
			outbounds: outbounds,
			route: { rules: rules, final: selected, default_domain_resolver: 'tunnel-dns' },
		},
	};
}

function legacyBundle(entry, policy, machine) {
	if (entry == null) return fail('snapshot_not_applied');
	let result = renderLegacy(entry.snapshot, policy, machine);
	if (!result.ok) return result;
	let value = { version: 1, router_id: entry.snapshot.router_id, etag: entry.etag, attempt: entry.attempt,
		policy: copy(policy), config: result.config, profile: result.profile, capabilities: result.capabilities };
	if (result.awg != null) value.awg = result.awg;
	if (length(sprintf('%J', value)) >= 65536) return fail('runtime_bundle_too_large');
	return { ok: true, value: value };
}

function render(snapshot, policy, machine, preferredProfile, lane) {
	if (!machine.validateSnapshot(snapshot).ok) return fail('snapshot_validation_failed');
	let descriptor = laneDescriptor(snapshot, lane);
	if (descriptor === false) return fail('invalid_lane');
	if (descriptor != null && descriptor.empty === true)
		return { ok: true, empty: true, lane: descriptor.id };
	/* Schema 3 and calls without a lane retain the exact single-runtime contract. */
	let managedLane = descriptor != null && snapshot.schema_version >= 4;
	let validated = validatePolicy(policy);
	if (!validated.ok) return validated;
	let effective = normalizedPolicy(policy);
	/* Avoid WAN DNS bootstrap dependencies/cycles; current AutoVPN publishes IPs. */
	if (!ipv4(snapshot.server.endpoint)) return fail('endpoint_ipv4_required');
	let outbounds = [];
	let candidates = [];
	let capabilities = machine.normalizeCapabilities({});
	for (let i = 0; i < 2; i++) {
		let name = ['vless', 'hysteria2'][i];
		let slot = snapshot.protocols[name];
		if (!slot.enabled) continue;
		if (name == 'hysteria2' && slot.outbound.tls.insecure && effective.hysteria_tls_mode == 'strict') {
			if (effective.selection == 'hysteria2') return fail('hysteria_tls_unverified');
			continue;
		}
		let outbound = copy(slot.outbound);
		outbound.bind_interface = effective.wan_device;
		push(outbounds, outbound);
		push(candidates, outbound.tag);
		capabilities[name] = true;
	}
	let awg = null;
	let awgSlot = managedLane && descriptor.id == 'vpn_zapret'
		? snapshot.protocols.amneziawg_aux : snapshot.protocols.amneziawg;
	/* Keep AWG prepared for diagnostics even while another manual profile is active. */
	if (awgSlot.enabled && effective.awg_available && validateAwg(awgSlot.profile)) {
		awg = copy(awgSlot.profile);
		push(outbounds, { type: 'direct', tag: 'amneziawg',
			bind_interface: managedLane ? descriptor.awg.device : 'avpnwg0',
			routing_mark: managedLane ? descriptor.awg.inner_mark : 20193,
			domain_resolver: 'awg-dns' });
		push(candidates, 'amneziawg');
		capabilities.amneziawg = true;
	}
	if (length(candidates) == 0) return fail('no_supported_vpn');
	if (effective.selection != 'auto' && index(candidates, effective.selection) < 0)
		return fail('selected_vpn_unavailable');
	let selected = effective.selection;
	if (selected == 'auto')
		selected = index(candidates, preferredProfile) >= 0 ? preferredProfile : candidates[0];
	let zapret = null;
	/* In schema 4 preprocessing belongs only to vpn_zapret. Legacy calls stay unchanged. */
	if (!managedLane || descriptor.id == 'vpn_zapret')
		zapret = require('autovpn.zapret').plan(snapshot, candidates, effective.wan_device,
			effective.zapret, managedLane ? descriptor.id : null);
	if (zapret === false) return fail('invalid_zapret_plan');
	if (zapret != null) {
		for (let i = 0; i < length(zapret.flows); i++) {
			let flow = zapret.flows[i];
			for (let n = 0; n < length(outbounds); n++)
				if (outbounds[n].tag == flow.profile && flow.profile != 'amneziawg')
					outbounds[n].routing_mark = flow.mark;
			if (flow.profile == selected) capabilities.zapret = true;
		}
	}
	push(outbounds, { type: 'direct', tag: 'direct', bind_interface: effective.wan_device });

	/* Forced probe routes precede DNS interception and every user direct exception. */
	let rules = [{ inbound: ['health'], action: 'route', outbound: selected }];
	let healthPort = managedLane ? descriptor.health_port : 1088;
	let transparent = managedLane ? descriptor.transparent
		: { listen_port: 12080, mark: 20180, route_table: 20180, priority: 20180 };
	let inbounds = [
		{ type: 'tproxy', tag: 'vpn-net', listen: '0.0.0.0', listen_port: transparent.listen_port },
		{ type: 'socks', tag: 'health', listen: '127.0.0.1', listen_port: healthPort },
	];
	let probePorts = managedLane ? descriptor.probe_ports
		: { 'vless-reality': 1089, hysteria2: 1090, amneziawg: 1091 };
	for (let i = 0; i < length(candidates); i++) {
		let candidate = candidates[i];
		let inbound = 'probe-' + candidate;
		push(inbounds, { type: 'socks', tag: inbound, listen: '127.0.0.1', listen_port: probePorts[candidate] });
		push(rules, { inbound: [inbound], action: 'route', outbound: candidate });
	}
	push(rules, { port: 53, action: 'hijack-dns' });
	push(rules, { action: 'sniff', timeout: '300ms' });
	push(rules, { ip_version: 6, action: 'reject' });
	push(rules, { ip_is_private: true, action: 'reject' });
	if (length(effective.direct_domains))
		push(rules, { domain_suffix: effective.direct_domains, action: 'route', outbound: 'direct' });
	if (length(effective.direct_cidrs))
		push(rules, { ip_cidr: effective.direct_cidrs, action: 'route', outbound: 'direct' });

	if (effective.ru_bypass === true)
		push(rules, { rule_set: ['autovpn-ru'], action: 'route', outbound: 'direct' });
	let route = { rules: rules, final: selected, default_domain_resolver: 'tunnel-dns' };
	if (effective.ru_bypass === true)
		route.rule_set = [{ type: 'local', tag: 'autovpn-ru', format: 'source',
			path: '/etc/autovpn/routing/ru.json' }];
	let dnsServers = [{ type: 'udp', tag: 'tunnel-dns', server: effective.dns_server, server_port: 53, detour: selected }];
	if (awg != null)
		push(dnsServers, { type: 'udp', tag: 'awg-dns', server: effective.dns_server, server_port: 53, detour: 'amneziawg' });
	capabilities.policy_routing = true;
	return {
		ok: true, profile: selected, candidates: candidates, capabilities: capabilities, awg: awg, zapret: zapret,
		config: {
			log: { disabled: true },
			dns: { servers: dnsServers, final: 'tunnel-dns', strategy: 'ipv4_only', reverse_mapping: true },
			inbounds: inbounds,
			outbounds: outbounds,
			route: route,
		},
	};
}

function bundle(entry, policy, machine, preferredProfile, lane) {
	if (entry == null) return fail('snapshot_not_applied');
	let result = render(entry.snapshot, policy, machine, preferredProfile, lane);
	if (!result.ok) return result;
	if (result.empty === true) return result;
	let version = lane != null && entry.snapshot.schema_version >= 4 ? 4 : 2;
	let value = { version: 2, router_id: entry.snapshot.router_id, etag: entry.etag, attempt: entry.attempt,
		policy: copy(policy), config: result.config, profile: result.profile, candidates: result.candidates,
		capabilities: result.capabilities };
	value.version = version;
	if (version >= 3) value.lane = lane;
	if (result.awg != null) value.awg = result.awg;
	if (result.zapret != null) value.zapret = result.zapret;
	if (length(sprintf('%J', value)) >= 65536) return fail('runtime_bundle_too_large');
	return { ok: true, value: value };
}

/* Re-render before using persistent generated files; reject tampering/corruption. */
function matchesBundle(value, entry, machine, lane) {
	if (type(value) != 'object' || entry == null || value.router_id != entry.snapshot.router_id ||
		value.etag != entry.etag || value.attempt != entry.attempt || index([1, 2, 3, 4], value.version) < 0)
		return false;
	if (lane == 'vpn_zapret' && value.version < 3) return false;
	if (value.version >= 3 && (type(value.lane) != 'string' ||
		(lane != null && value.lane != lane))) return false;
	let expected = value.version == 1 ? legacyBundle(entry, value.policy, machine)
		: value.version == 2 ? bundle(entry, value.policy, machine, value.profile)
		: bundle(entry, value.policy, machine, value.profile, value.lane);
	return expected.ok && sprintf('%J', expected.value) == sprintf('%J', value);
}

/* Version 3 used a manually-routed TUN that cannot carry forwarded LAN TCP.
 * Only bounded identity and policy survive the migration; generated runtime,
 * keys and capabilities are rebuilt from the signed applied snapshot. */
function migratableBundle(value, entry, machine, lane) {
	if (type(value) != 'object' || entry == null || value.version != 3 ||
		value.lane != lane || value.router_id != entry.snapshot.router_id ||
		value.etag != entry.etag || !validatePolicy(value.policy).ok ||
		index(['vless-reality', 'hysteria2', 'amneziawg'], value.profile) < 0)
		return false;
	let rebuilt = bundle(entry, value.policy, machine, value.profile, lane);
	return rebuilt.ok && rebuilt.value.version == 4 && rebuilt.value.profile == value.profile;
}

return { validatePolicy: validatePolicy, render: render, bundle: bundle,
	matchesBundle: matchesBundle, migratableBundle: migratableBundle };
