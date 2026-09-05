'use strict';

/* The direct lane is deliberately local and fixed: no API-controlled Lua or ports. */
const ENGINE = '/usr/lib/autovpn-zapret';
const BRIDGE = 'br-avpndz';
const SUBNET = '192.168.31.0/24';
const QUEUE = 20196;
const DESYNC_MARK = 1073741824;
const FLOW_MARK = 20221;
const RAW_MARK = DESYNC_MARK + FLOW_MARK;

function exact(value, names) {
	return type(value) == 'object' && join(',', sort(keys(value))) == join(',', sort(names));
}

function validWan(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/) != null &&
		index(['lo', 'br-lan', 'avpn0', 'avpn1', 'avpnwg0', 'avpnwg1',
			'br-avpn', 'br-avpnz', 'br-avpnd', BRIDGE], value) < 0;
}

function validPlan(value) {
	return exact(value, ['version', 'wan_device']) && value.version == 1 && validWan(value.wan_device);
}

function plan(wan, enabled) {
	if (enabled === false) return null;
	let value = { version: 1, wan_device: wan };
	return validPlan(value) ? value : false;
}

function config(value) {
	if (!validPlan(value)) return null;
	return join('\n', [
		'--qnum=' + QUEUE,
		'--fwmark=0x40000000',
		'--bind-fix4',
		'--lua-init=@' + ENGINE + '/zapret-lib.lua',
		'--lua-init=@' + ENGINE + '/zapret-antidpi.lua',
		'--filter-l3=ipv4',
		'--filter-tcp=80,443',
		'--filter-l7=http,tls',
		'--in-range=x',
		'--out-range=-n12',
		'--payload=tls_client_hello,http_req',
		'--lua-desync=multisplit:pos=1,midsld',
		'--new',
		'--filter-l3=ipv4',
		'--filter-udp=443',
		'--filter-l7=quic',
		'--in-range=x',
		'--out-range=-n12',
		'--payload=quic_initial',
		'--lua-desync=fake:blob=fake_default_quic:badsum:repeats=2',
	]) + '\n';
}

function header() {
	return 'table inet autovpn_direct_zapret\n' +
		'flush table inet autovpn_direct_zapret\n' +
		'table inet autovpn_direct_zapret {\n' +
		' chain ownership_autovpn_direct_zapret_v1 {}\n';
}

function closedNft() {
	return header() +
		' chain guard {\n type filter hook forward priority -10; policy accept;\n' +
		' iifname "' + BRIDGE + '" drop\n' +
		' oifname "' + BRIDGE + '" drop\n }\n}\n';
}

function nft(value) {
	if (!validPlan(value)) return null;
	let wan = value.wan_device;
	let scope = 'iifname "' + BRIDGE + '" oifname "' + wan + '" ct original ip saddr ' + SUBNET;
	let unmarked = ' meta mark & ' + DESYNC_MARK + ' == 0';
	let queue = ' meta mark set ' + FLOW_MARK + ' queue num ' + QUEUE;
	let raw = 'oifname "' + wan + '" meta mark ' + RAW_MARK;
	return header() +
		' chain guard {\n type filter hook forward priority -10; policy accept;\n' +
		' iifname "' + BRIDGE + '" ip saddr ' + SUBNET + ' oifname "' + wan + '" accept\n' +
		' iifname "' + wan + '" oifname "' + BRIDGE + '" ip daddr ' + SUBNET + ' ct state established,related accept\n' +
		' iifname "' + BRIDGE + '" drop\n' +
		' oifname "' + BRIDGE + '" drop\n }\n' +
		' chain post {\n type filter hook postrouting priority 101; policy accept;\n' +
		' ' + scope + unmarked + ' udp dport 443 ct original packets 1-12' + queue + '\n' +
		' ' + scope + unmarked + ' tcp dport { 80, 443 } ct original packets 1-12' + queue + '\n' +
		' ' + scope + unmarked + ' tcp dport { 80, 443 } tcp flags & (fin | rst) != 0' + queue + '\n }\n' +
		' chain raw {\n type filter hook output priority -401; policy accept;\n' +
		' ' + raw + ' udp dport 443 notrack\n' +
		' ' + raw + ' tcp dport { 80, 443 } notrack\n }\n}\n';
}

return { validPlan: validPlan, plan: plan, config: config, nft: nft, closedNft: closedNft };
