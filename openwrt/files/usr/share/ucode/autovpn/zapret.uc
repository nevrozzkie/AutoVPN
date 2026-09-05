'use strict';

/* Local, typed policy only. No subscription-provided Lua, argv or host lists. */
const PROFILES = ['vless-reality', 'hysteria2', 'amneziawg'];
const MARKS = [20201, 20202, 20194];
const ENGINE = '/usr/lib/autovpn-zapret';
function exact(value, names) {
	return type(value) == 'object' && join(',', sort(keys(value))) == join(',', sort(names));
}
function ip(value) {
	if (type(value) != 'string' || match(value, /^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$/) == null) return false;
	let parts = split(value, '.');
	for (let i = 0; i < 4; i++) if (int(parts[i]) > 255) return false;
	return true;
}
function validPolicy(value) {
	return exact(value, ['vless', 'hysteria2', 'amneziawg', 'repeats']) &&
		index(['off', 'split'], value.vless) >= 0 &&
		index(['off', 'fake'], value.hysteria2) >= 0 &&
		index(['off', 'fake'], value.amneziawg) >= 0 &&
		type(value.repeats) == 'int' && value.repeats >= 1 && value.repeats <= 6;
}
function validPlan(value) {
	if (value == null) return true;
	let fields = ['version', 'wan_device', 'flows', 'repeats'];
	if (value.version == 2) push(fields, 'lane');
	if (!exact(value, fields) || (value.version != 1 && value.version != 2) ||
		(value.version == 2 && value.lane != 'vpn_zapret') ||
		type(value.wan_device) != 'string' || match(value.wan_device, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/) == null ||
		index(['lo', 'br-lan', 'avpn0', 'avpn1', 'avpnwg0', 'avpnwg1', 'br-avpn', 'br-avpnz', 'br-avpnd', 'br-avpndz'], value.wan_device) >= 0 ||
		type(value.repeats) != 'int' || value.repeats < 1 || value.repeats > 6 ||
		type(value.flows) != 'array' || length(value.flows) < 1 || length(value.flows) > 3) return false;
	let previous = -1;
	for (let i = 0; i < length(value.flows); i++) {
		let flow = value.flows[i];
		if (!exact(flow, ['profile', 'ip', 'port', 'transport', 'mark', 'strategy'])) return false;
		let position = index(PROFILES, flow.profile);
		if (position <= previous || !ip(flow.ip) || type(flow.port) != 'int' || flow.port < 1 || flow.port > 65535 ||
			flow.mark != (value.version == 2 && position == 2 ? 20214 : MARKS[position]) || flow.transport != (position == 0 ? 'tcp' : 'udp') ||
			flow.strategy != (position == 0 ? 'split' : 'fake')) return false;
		previous = position;
	}
	return true;
}
function plan(snapshot, candidates, wan, policy, lane) {
	if (policy == null) return null;
	if (lane != null && lane != 'vpn_zapret') return false;
	if (!validPolicy(policy)) return false;
	let value = { version: 1, wan_device: wan, flows: [], repeats: policy.repeats };
	if (lane == 'vpn_zapret') { value.version = 2; value.lane = lane; }
	for (let i = 0; i < 3; i++) {
		let key = ['vless', 'hysteria2', 'amneziawg'][i];
		if (index(candidates, PROFILES[i]) < 0 || policy[key] == 'off') continue;
		let slot = snapshot.protocols[lane == 'vpn_zapret' && key == 'amneziawg' ? 'amneziawg_aux' : key];
		let endpoint = i == 2 ? slot.profile.peer.endpoint : slot.outbound;
		push(value.flows, { profile: PROFILES[i], ip: i == 2 ? endpoint.host : endpoint.server,
			port: i == 2 ? endpoint.port : endpoint.server_port,
			transport: i == 0 ? 'tcp' : 'udp', mark: lane == 'vpn_zapret' && i == 2 ? 20214 : MARKS[i], strategy: policy[key] });
	}
	if (length(value.flows) == 0) return null;
	return validPlan(value) ? value : false;
}
function config(value) {
	if (value == null || !validPlan(value)) return null;
	let args = ['--qnum=20195', '--fwmark=0x40000000', '--bind-fix4', '--lua-init=@' + ENGINE + '/zapret-lib.lua',
		'--lua-init=@' + ENGINE + '/zapret-antidpi.lua'];
	for (let i = 0; i < length(value.flows); i++) {
		let flow = value.flows[i];
		if (i) push(args, '--new');
		push(args, '--filter-l3=ipv4');
		push(args, '--filter-' + flow.transport + '=' + flow.port);
		push(args, '--filter-mark=' + flow.mark + '/0xffffffff');
		push(args, '--in-range=x');
		push(args, '--out-range=-n12');
		if (flow.strategy == 'split') {
			push(args, '--payload=tls_client_hello');
			push(args, '--lua-desync=multisplit:pos=1,midsld');
		}
		else {
			/* AWG payloads may deliberately no longer match the WireGuard signature. */
			push(args, '--payload=all');
			push(args, '--lua-desync=fake:payload=all:blob=fake_default_quic:badsum:repeats=' + value.repeats);
		}
	}
	return join('\n', args) + '\n';
}
function nft(value) {
	if (value == null || !validPlan(value)) return null;
	let output = 'table inet autovpn_zapret\nflush table inet autovpn_zapret\ntable inet autovpn_zapret {\n' +
		' chain ownership_autovpn_zapret_v1 {}\n chain post {\n type filter hook postrouting priority 101; policy accept;\n';
	for (let i = 0; i < length(value.flows); i++) {
		let flow = value.flows[i];
		let scoped = ' oifname "' + value.wan_device + '" meta mark ' + flow.mark +
			' ip daddr ' + flow.ip + ' ' + flow.transport + ' dport ' + flow.port;
		/* No bypass: if the listener dies, new scoped handshakes cannot skip it. */
		output += scoped + ' ct original packets 1-12 queue num 20195\n';
		if (flow.transport == 'tcp') output += scoped + ' tcp flags & (fin | rst) != 0 queue num 20195\n';
	}
	output += ' }\n chain raw {\n type filter hook output priority -401; policy accept;\n';
	for (let i = 0; i < length(value.flows); i++) {
		let flow = value.flows[i];
		/* Exact low mark, endpoint and WAN scope; never untrack somebody else's packets. */
		output += ' oifname "' + value.wan_device + '" meta mark ' + (1073741824 + flow.mark) +
			' ip daddr ' + flow.ip + ' ' + flow.transport + ' dport ' + flow.port + ' notrack\n';
	}
	return output + ' }\n}\n';
}

return { validPolicy: validPolicy, validPlan: validPlan, plan: plan, config: config, nft: nft };
