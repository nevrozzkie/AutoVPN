#!/usr/bin/ucode
'use strict';

/*
 * The profile file is generated from the HTTPS-delivered, validated snapshot and is
 * mode 0600. This helper never puts a private key in an argv vector.
 */
import { readfile, writefile, chmod, rename, unlink, access } from 'fs';

const processRunner = require('autovpn.process');
const lanes = require('autovpn.lanes');

const LANE = lanes.get(ARGV[2] == null ? 'vpn' : ARGV[2]);
const ROOT = LANE != null ? LANE.root : '';
const DEVICE = LANE != null ? LANE.awg.device : '';
const OWNED = ROOT + '/awg-owned';
/* Preserve primary ownership metadata byte-for-byte; secondary is disjoint. */
const ALIAS = LANE != null && LANE.id == 'vpn_zapret' ? 'autovpn-awg-zapret-v1' : 'autovpn-awg-v1';
/*
 * Keep a durable intent before creating the device. iproute2 puts the alias
 * and link type in the same RTM_NEWLINK request, so a post-crash cleanup can
 * require marker + alias + type without ever claiming an arbitrary avpnwg0.
 */
const MARKER = ALIAS + '\n';
const IP = '/sbin/ip';
const MODPROBE = '/sbin/modprobe';

function awgBin() {
	if (access('/usr/bin/awg', 'x') === true) return '/usr/bin/awg';
	if (access('/sbin/awg', 'x') === true) return '/sbin/awg';
	return null;
}

function command(argv) {
	let pipe = processRunner.popen(argv, 'r');
	if (pipe == null) return false;
	pipe.read(1);
	return pipe.close() == 0;
}
function output(argv, limit) {
	let pipe = processRunner.popen(argv, 'r');
	if (pipe == null) return null;
	let value = pipe.read(limit + 1) || '';
	return pipe.close() == 0 && length(value) <= limit ? value : null;
}
function hasExactKeys(value, expected) {
	if (type(value) != 'object') return false;
	let actual = sort(keys(value));
	let wanted = sort(expected);
	return join(',', actual) == join(',', wanted);
}
function key(value) { return type(value) == 'string' && match(value, /^[A-Za-z0-9+\/]{43}=$/) != null; }
function ipv4Cidr(value) {
	if (type(value) != 'string' || match(value, /^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\/32$/) == null) return false;
	let parts = split(split(value, '/')[0], '.');
	for (let i = 0; i < 4; i++) if (int(parts[i]) > 255 || (length(parts[i]) > 1 && substr(parts[i], 0, 1) == '0')) return false;
	return true;
}
function valid(profile) {
	let expected = LANE != null && LANE.id == 'vpn'
		? ['protocol_version', 'capabilities', 'interface', 'peer', 'obfuscation',
			'route_allowed_ips', 'install_routes', 'legacy_amnezia_vpn_import_key']
		: ['protocol_version', 'capabilities', 'interface', 'peer', 'obfuscation',
			'route_allowed_ips', 'install_routes'];
	if (!hasExactKeys(profile, expected)) return false;
	if (type(profile.interface) != 'object' || type(profile.peer) != 'object' || type(profile.obfuscation) != 'object') return false;
	if (profile.protocol_version != 1 || profile.install_routes !== false || !key(profile.interface.private_key) ||
		!ipv4Cidr(profile.interface.address) || !key(profile.peer.public_key) || !key(profile.peer.preshared_key)) return false;
	if (!hasExactKeys(profile.peer, ['public_key', 'preshared_key', 'endpoint', 'persistent_keepalive']) || type(profile.peer.endpoint) != 'object' ||
		!hasExactKeys(profile.peer.endpoint, ['host', 'port']) ||
		type(profile.peer.endpoint.host) != 'string' || !ipv4Cidr(profile.peer.endpoint.host + '/32') ||
		type(profile.peer.endpoint.port) != 'int' || profile.peer.endpoint.port < 1 || profile.peer.endpoint.port > 65535 ||
		type(profile.peer.persistent_keepalive) != 'int' || profile.peer.persistent_keepalive < 0 || profile.peer.persistent_keepalive > 65535) return false;
	if (!hasExactKeys(profile.obfuscation, ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4'])) return false;
	for (let name in profile.obfuscation)
		if (type(profile.obfuscation[name]) != 'int' || profile.obfuscation[name] < 0 || profile.obfuscation[name] > 4294967295) return false;
	let small = ['Jc', 'Jmin', 'Jmax', 'S1', 'S2'];
	for (let i = 0; i < length(small); i++)
		if (profile.obfuscation[small[i]] > 65535) return false;
	if (profile.obfuscation.Jmin > profile.obfuscation.Jmax) return false;
	return true;
}
function ipNumber(address) {
	let parts = split(address, '.');
	let value = 0;
	for (let i = 0; i < 4; i++) value = value * 256 + int(parts[i]);
	return value;
}
function addressFree(address) {
	let raw = output([IP, '-j', '-4', 'route', 'show', 'table', 'main'], 65536);
	let local = output([IP, '-j', '-4', 'address', 'show'], 65536);
	if (raw == null || local == null) return false;
	try {
		let routes = json(raw);
		let devices = json(local);
		if (type(routes) != 'array' || type(devices) != 'array') return false;
		let ip = split(address, '/')[0];
		let number = ipNumber(ip);
		if (number < 16777216 || number >= 3758096384 || substr(ip, 0, 4) == '127.') return false;
		for (let i = 0; i < length(devices); i++) {
			if (devices[i].ifname == DEVICE) continue;
			if (type(devices[i].addr_info) != 'array') return false;
			for (let a = 0; a < length(devices[i].addr_info); a++)
				if (devices[i].addr_info[a].local == ip) return false;
		}
		for (let i = 0; i < length(routes); i++) {
			let route = routes[i];
			if (route.dev == DEVICE || route.dst == 'default' || route.dst == '0.0.0.0/0') continue;
			if (type(route.dst) != 'string') return false;
			let parts = split(route.dst, '/');
			if (!ipv4Cidr(parts[0] + '/32') || length(parts) > 2) return false;
			let bits = length(parts) == 1 ? 32 : int(parts[1]);
			if (bits < 0 || bits > 32) return false;
			let size = 1;
			for (let b = bits; b < 32; b++) size *= 2;
			let start = ipNumber(parts[0]);
			start -= start % size;
			if (number >= start && number < start + size) return false;
		}
		return true;
	} catch (e) { return false; }
}
function privateWrite(path, raw) {
	return writefile(path + '.new', raw) == length(raw) && chmod(path + '.new', 0o600) != null && rename(path + '.new', path) != null;
}
function marker() {
	let raw = readfile(OWNED, 64);
	return raw == MARKER;
}
function profile(path) {
	let raw = readfile(path, 32769);
	if (raw == null || length(raw) > 32768) return null;
	try { return json(raw); } catch (e) { return null; }
}
function link() {
	let raw = output([IP, '-d', '-j', 'link', 'show', 'dev', DEVICE], 8192);
	if (raw == null) return null;
	try {
		let links = json(raw);
		if (type(links) != 'array' || length(links) != 1) return false;
		let item = links[0];
		return type(item) == 'object' && item.ifalias == ALIAS && type(item.linkinfo) == 'object' &&
			item.linkinfo.info_kind == 'amneziawg';
	} catch (e) { return false; }
}
function moduleReady() {
	if (access('/sys/module/amneziawg') === true) return true;
	return command([MODPROBE, 'amneziawg']) && access('/sys/module/amneziawg') === true;
}
function available() {
	return awgBin() != null && moduleReady();
}
function down() {
	/* Never delete an interface that was not created by this controller. */
	if (access(OWNED) !== true) return true;
	if (!marker()) return false;
	let owned = link();
	if (owned === null) {
		/* A power loss leaves a stale marker but no kernel link after boot. */
		if (access('/sys/class/net/' + DEVICE) !== true) return unlink(OWNED) === true;
		return false;
	}
	if (owned !== true || !command([IP, 'link', 'del', 'dev', DEVICE])) return false;
	/* Preserve the marker if deletion failed, so a later retry cannot touch a foreign link. */
	return unlink(OWNED) === true;
}
function up(path) {
	let value = profile(path);
	if (!valid(value) || !addressFree(value.interface.address)) return false;
	if (!available() || !down()) return false;
	/* Refuse a foreign/orphan link before recording a new creation intent. */
	if (access('/sys/class/net/' + DEVICE) === true || !privateWrite(OWNED, MARKER)) return false;
	if (!command([IP, 'link', 'add', 'dev', DEVICE, 'alias', ALIAS, 'type', 'amneziawg']))
		return false;
	let peer = value.peer;
	let o = value.obfuscation;
	let config = '[Interface]\nPrivateKey = ' + value.interface.private_key + '\n' +
		'Jc = ' + o.Jc + '\nJmin = ' + o.Jmin + '\nJmax = ' + o.Jmax + '\n' +
		'S1 = ' + o.S1 + '\nS2 = ' + o.S2 + '\nH1 = ' + o.H1 + '\nH2 = ' + o.H2 + '\nH3 = ' + o.H3 + '\nH4 = ' + o.H4 + '\n\n' +
		'[Peer]\nPublicKey = ' + peer.public_key + '\nPresharedKey = ' + peer.preshared_key + '\n' +
		'AllowedIPs = 0.0.0.0/0\nEndpoint = ' + peer.endpoint.host + ':' + peer.endpoint.port + '\n' +
		'PersistentKeepalive = ' + peer.persistent_keepalive + '\n';
	let awg = awgBin();
	let configPath = ROOT + '/awg.conf';
	let outerMark = LANE.id == 'vpn_zapret' ? '20214' : '20194';
	if (awg == null || !privateWrite(configPath, config) ||
		!command([awg, 'setconf', DEVICE, configPath]) ||
		!command([awg, 'set', DEVICE, 'fwmark', outerMark]) ||
		!command([IP, '-4', 'address', 'replace', value.interface.address, 'dev', DEVICE]) ||
		!command([IP, 'link', 'set', 'dev', DEVICE, 'mtu', '1380']) ||
		!command([IP, 'link', 'set', 'dev', DEVICE, 'up'])) {
		down();
		return false;
	}
	return true;
}
function check(path) {
	let value = profile(path);
	let awg = awgBin();
	return awg != null && valid(value) && marker() && link() === true && command([awg, 'show', DEVICE]);
}

let action = ARGV[0];
let result = false;
if (LANE != null && action == 'down') result = down();
else if (LANE != null && action == 'up' && type(ARGV[1]) == 'string' && ARGV[1] == ROOT + '/awg.json') result = up(ARGV[1]);
else if (LANE != null && action == 'check' && type(ARGV[1]) == 'string' && ARGV[1] == ROOT + '/awg.json') result = check(ARGV[1]);
else if (LANE != null && action == 'available') result = available();
printf('{"ok":%s}\n', result ? 'true' : 'false');
exit(result ? 0 : 1);
