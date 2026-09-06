'use strict';

/* The direct lane is deliberately local and fixed: no API-controlled Lua or ports. */
const ENGINE = '/usr/lib/autovpn-zapret';
const BRIDGE = 'br-avpndz';
const SUBNET = '192.168.31.0/24';
const QUEUE = 20196;
const DESYNC_MARK = 1073741824;
const FLOW_MARK = 20221;
const RAW_MARK = DESYNC_MARK + FLOW_MARK;
const DISCORD_MARK = 20222;
const STUN_MARK = 20223;
const DISCORD_PORTS = '50000-50099,19294-19344';

function exact(value, names) {
	return type(value) == 'object' && join(',', sort(keys(value))) == join(',', sort(names));
}

function validWan(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,14}$/) != null &&
		index(['lo', 'br-lan', 'avpn0', 'avpn1', 'avpnwg0', 'avpnwg1',
			'br-avpn', 'br-avpnz', 'br-avpnd', BRIDGE], value) < 0;
}

function validMedia(value) {
	return type(value.discord_media) == 'bool' && type(value.stun) == 'bool' &&
		index(['fake', 'fake_badsum'], value.media_strategy) >= 0 &&
		type(value.media_repeats) == 'int' && value.media_repeats >= 1 && value.media_repeats <= 6;
}

function validPlan(value) {
	if (exact(value, ['version', 'wan_device']) && value.version == 1)
		return validWan(value.wan_device);
	return exact(value, ['version', 'wan_device', 'discord_media', 'stun', 'media_strategy', 'media_repeats']) &&
		value.version == 2 && validWan(value.wan_device) && validMedia(value);
}

function plan(wan, enabled, media) {
	if (enabled === false) return null;
	let value = { version: 1, wan_device: wan };
	if (media != null) {
		if (!exact(media, ['discord_media', 'stun', 'media_strategy', 'media_repeats']) || !validMedia(media))
			return false;
		value = { version: 2, wan_device: wan, discord_media: media.discord_media,
			stun: media.stun, media_strategy: media.media_strategy, media_repeats: media.media_repeats };
	}
	return validPlan(value) ? value : false;
}

function mediaConfig(value, protocol, ports, payload) {
	return '\n--new\n--filter-l3=ipv4\n--filter-udp=' + ports + '\n--filter-l7=' + protocol +
		'\n--in-range=x\n--out-range=a\n--payload=' + payload +
		'\n--lua-desync=fake:blob=0x00000000000000000000000000000000' +
		(value.media_strategy == 'fake_badsum' ? ':badsum' : '') + ':repeats=' + value.media_repeats;
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
	]) + (value.discord_media ? mediaConfig(value, 'discord', DISCORD_PORTS, 'discord_ip_discovery') : '') +
		(value.stun ? mediaConfig(value, 'stun', '1-65535', 'stun') : '') + '\n';
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
	/* Upstream v1.0.5 50-discord-media / 50-stun4all signatures, at UDP payload (@ih).
	 * Queue only discovery/negotiation, including late STUN; never the full media stream. */
	let media = '';
	let mediaRaw = '';
	if (value.discord_media) {
		media += ' ' + scope + unmarked + ' udp dport { ' + DISCORD_PORTS + ' } udp length == 82' +
			' @ih,0,32 0x00010046 @ih,64,128 0x00000000000000000000000000000000' +
			' @ih,192,128 0x00000000000000000000000000000000' +
			' @ih,320,128 0x00000000000000000000000000000000' +
			' @ih,448,128 0x00000000000000000000000000000000' +
			' meta mark set ' + DISCORD_MARK + ' queue num ' + QUEUE + '\n';
		mediaRaw += ' oifname "' + wan + '" meta mark ' + (DESYNC_MARK + DISCORD_MARK) +
			' udp dport { ' + DISCORD_PORTS + ' } notrack\n';
	}
	if (value.stun) {
		media += ' ' + scope + unmarked + ' meta l4proto udp udp length >= 28' +
			' @ih,32,32 0x2112A442 @ih,0,2 0 @ih,30,2 0' +
			' meta mark set ' + STUN_MARK + ' queue num ' + QUEUE + '\n';
		mediaRaw += ' oifname "' + wan + '" meta mark ' + (DESYNC_MARK + STUN_MARK) +
			' meta l4proto udp notrack\n';
	}
	/* A queue verdict can resume rule traversal: do not queue STUN/443 again as web. */
	let webUnmarked = unmarked + ((value.discord_media || value.stun) ?
		' meta mark != { ' + DISCORD_MARK + ', ' + STUN_MARK + ' }' : '');
	return header() +
		' chain guard {\n type filter hook forward priority -10; policy accept;\n' +
		' iifname "' + BRIDGE + '" ip saddr ' + SUBNET + ' oifname "' + wan + '" accept\n' +
		' iifname "' + wan + '" oifname "' + BRIDGE + '" ip daddr ' + SUBNET + ' ct state established,related accept\n' +
		' iifname "' + BRIDGE + '" drop\n' +
		' oifname "' + BRIDGE + '" drop\n }\n' +
		' chain post {\n type filter hook postrouting priority 101; policy accept;\n' +
		media +
		' ' + scope + webUnmarked + ' udp dport 443 ct original packets 1-12' + queue + '\n' +
		' ' + scope + unmarked + ' tcp dport { 80, 443 } ct original packets 1-12' + queue + '\n' +
		' ' + scope + unmarked + ' tcp dport { 80, 443 } tcp flags & (fin | rst) != 0' + queue + '\n }\n' +
		' chain raw {\n type filter hook output priority -401; policy accept;\n' +
		mediaRaw +
		' ' + raw + ' udp dport 443 notrack\n' +
		' ' + raw + ' tcp dport { 80, 443 } notrack\n }\n}\n';
}

return { validPlan: validPlan, plan: plan, config: config, nft: nft, closedNft: closedNft };
