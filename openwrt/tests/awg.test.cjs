'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const helper = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/awg-helper.uc'), 'utf8');
const adapter = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-adapter'), 'utf8');

function runAwg(action, accessResult, popenResult, readResult = () => null) {
	let code;
	const source = helper.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'popen', 'require',
		'type', 'match', 'sort', 'keys', 'join', 'length', 'split', 'int', 'substr', 'json', 'ARGV', 'printf', 'exit', source);
	invoke(
		path => readResult(path), () => null, () => true, () => true, () => true, path => accessResult(path),
		path => popenResult(path), name => name === 'autovpn.process' ? { popen: popenResult } : null,
		value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value,
		(value, expression) => value.match(expression), value => Array.isArray(value) ? value.slice().sort() : Object.keys(value).sort(), Object.keys,
		(separator, values) => values.join(separator), value => value.length, (value, separator) => value.split(separator),
		value => Number.parseInt(value, 10), (value, start, length) => value.substr(start, length), JSON.parse,
		[action], () => {}, value => { code = value; }
	);
	return code;
}

const profile = () => ({
	protocol_version: 1,
	capabilities: { awg_obfuscation_v1: true, awg2_i_fields: false, obfuscation_fields: ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4'] },
	interface: { private_key: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', address: '10.66.66.8/32', dns_servers: ['1.1.1.1'] },
	peer: { public_key: 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=', preshared_key: 'CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=', endpoint: { host: '203.0.113.9', port: 51820 }, persistent_keepalive: 25 },
	obfuscation: { Jc: 3, Jmin: 20, Jmax: 700, S1: 30, S2: 30, H1: 1, H2: 2, H3: 3, H4: 4 },
	route_allowed_ips: ['0.0.0.0/0', '::/0'], install_routes: false, legacy_amnezia_vpn_import_key: 'vpn://opaque'
});

function awgHarness({ commandFailure, routes = [], marker, link } = {}) {
	const files = new Map([['/etc/autovpn/runtime/awg.json', JSON.stringify(profile())]]);
	if (marker != null) files.set('/etc/autovpn/runtime/awg-owned', marker);
	let device = link || null;
	const calls = [];
	let code;
	const source = helper.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'popen', 'require',
		'type', 'match', 'sort', 'keys', 'join', 'length', 'split', 'int', 'substr', 'json', 'ARGV', 'printf', 'exit', source);
	function access(path) {
		if (path === '/usr/bin/awg' || path === '/sys/module/amneziawg') return true;
		if (path === '/sys/class/net/avpnwg0') return device != null;
		return files.has(path);
	}
	function popen(argv) {
		calls.push(argv);
	if (argv.join(' ') === '/sbin/ip -j -4 route show table main')
			return { read: () => JSON.stringify(routes), close: () => 0 };
	if (argv.join(' ') === '/sbin/ip -j -4 address show')
			return { read: () => '[]', close: () => 0 };
	if (argv.join(' ') === '/sbin/ip -d -j link show dev avpnwg0')
			return { read: () => device == null ? '' : JSON.stringify([{ ifalias: device.alias, linkinfo: { info_kind: device.kind } }]), close: () => device == null ? 1 : 0 };
		const failing = commandFailure && commandFailure(argv);
		return {
			read: () => '',
			close: () => {
				if (failing) return 1;
			if (argv.join(' ') === '/sbin/ip link add dev avpnwg0 alias autovpn-awg-v1 type amneziawg') device = { alias: 'autovpn-awg-v1', kind: 'amneziawg' };
			if (argv.join(' ') === '/sbin/ip link del dev avpnwg0') device = null;
				return 0;
			}
		};
	}
	invoke(
		(path, limit) => files.has(path) ? files.get(path).slice(0, limit) : null,
		(path, value) => { files.set(path, value); return value.length; }, () => true,
		(from, to) => { files.set(to, files.get(from)); files.delete(from); return true; }, path => files.delete(path), access, popen,
		name => name === 'autovpn.process' ? { popen } : null,
		value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : Number.isInteger(value) ? 'int' : typeof value,
		(value, expression) => value.match(expression), value => Array.isArray(value) ? value.slice().sort() : Object.keys(value).sort(), Object.keys,
		(separator, values) => values.join(separator), value => value.length, (value, separator) => value.split(separator),
		value => Number.parseInt(value, 10), (value, start, length) => value.substr(start, length), JSON.parse,
		['up', '/etc/autovpn/runtime/awg.json'], () => {}, value => { code = value; }
	);
	return { code, calls, files, device };
}

test('AWG helper requires an AmneziaWG kernel link and never invokes ordinary wg', () => {
	assert.match(helper, /IP, 'link', 'add', 'dev', DEVICE, 'alias', ALIAS, 'type', 'amneziawg/);
	assert.match(helper, /awg, 'setconf', DEVICE, '\/etc\/autovpn\/runtime\/awg\.conf'/);
	assert.doesNotMatch(helper, /['"]wg['"]/);
	assert.match(helper, /chmod\(path \+ '\.new', 0o600\)/);
	assert.match(helper, /Never delete an interface that was not created by this controller/);
	assert.match(helper, /access\(OWNED\) !== true/);
	assert.match(helper, /const MARKER = 'autovpn-awg-v1\\n'/);
	assert.match(helper, /item\.ifalias == ALIAS/);
	assert.match(helper, /item\.linkinfo\.info_kind == 'amneziawg'/);
	assert.match(helper, /access\('\/sys\/module\/amneziawg'\) === true/);
	assert.match(helper, /'mtu', '1380'/);
	assert.doesNotMatch(helper, /(?:command|output)\(\['(?!\/)/, 'every process argv starts with an absolute executable');
	assert.match(helper, /const IP = '\/sbin\/ip'/);
	assert.match(helper, /const MODPROBE = '\/sbin\/modprobe'/);
});

test('AWG up makes a durable intent before atomically aliased link creation and keeps secrets out of argv', () => {
	const env = awgHarness();
	assert.equal(env.code, 0);
	assert.equal(env.files.get('/etc/autovpn/runtime/awg-owned'), 'autovpn-awg-v1\n');
	assert.deepEqual(env.device, { alias: 'autovpn-awg-v1', kind: 'amneziawg' });
	const linkAdd = env.calls.findIndex(argv => argv.join(' ') === '/sbin/ip link add dev avpnwg0 alias autovpn-awg-v1 type amneziawg');
	const setConf = env.calls.findIndex(argv => argv.join(' ') === '/usr/bin/awg setconf avpnwg0 /etc/autovpn/runtime/awg.conf');
	assert.ok(linkAdd >= 0 && linkAdd < setConf);
	const argv = env.calls.flat().join(' ');
	assert.equal(argv.includes(profile().interface.private_key), false);
	assert.equal(argv.includes(profile().peer.preshared_key), false);
	assert.equal(env.files.get('/etc/autovpn/runtime/awg.conf').includes(profile().interface.private_key), true);
});

test('AWG retains its ownership intent when configuration cleanup fails', () => {
	const env = awgHarness({ commandFailure: argv =>
		argv.join(' ') === '/usr/bin/awg setconf avpnwg0 /etc/autovpn/runtime/awg.conf' ||
		argv.join(' ') === '/sbin/ip link del dev avpnwg0' });
	assert.equal(env.code, 1);
	assert.equal(env.files.get('/etc/autovpn/runtime/awg-owned'), 'autovpn-awg-v1\n');
	assert.deepEqual(env.device, { alias: 'autovpn-awg-v1', kind: 'amneziawg' });
});

test('AWG resumes a crash orphan only with its marker plus exact alias and AmneziaWG type', () => {
	const owned = awgHarness({ marker: 'autovpn-awg-v1\n', link: { alias: 'autovpn-awg-v1', kind: 'amneziawg' } });
	// `up()` first calls down(), then creates a fresh fully-owned device.
	assert.equal(owned.code, 0);
	assert.ok(owned.calls.some(argv => argv.join(' ') === '/sbin/ip link del dev avpnwg0'));
	const foreign = awgHarness({ marker: 'autovpn-awg-v1\n', link: { alias: '', kind: 'wireguard' } });
	assert.equal(foreign.code, 1);
	assert.equal(foreign.calls.some(argv => argv.join(' ') === '/sbin/ip link del dev avpnwg0'), false);
});

test('AWG refuses an interface address overlapping a main-table route before writing an intent marker', () => {
	const env = awgHarness({ routes: [{ dst: '10.66.66.0/24', dev: 'br-lan' }] });
	assert.equal(env.code, 1);
	assert.equal(env.files.has('/etc/autovpn/runtime/awg-owned'), false);
	assert.equal(env.calls.some(argv => argv.join(' ') === '/sbin/ip link add dev avpnwg0 alias autovpn-awg-v1 type amneziawg'), false);
});

test('AWG transport has separate inner and outer marks with an unreachable fallback', () => {
	assert.match(helper, /awg, 'set', DEVICE, 'fwmark', '20194'/);
	assert.match(adapter, /priority 20193 fwmark 20193 lookup 20193/);
	assert.match(adapter, /priority 20194 fwmark 20193 unreachable/);
	assert.match(adapter, /if awg_active && ! awg_up; then return 1; fi/);
});

test('AWG down treats false/null access as not-owned and never probes or deletes', () => {
	for (const value of [false, null]) {
		let probes = 0;
		assert.equal(runAwg('down', () => value, () => { probes++; return null; }), 0);
		assert.equal(probes, 0);
	}
});

test('AWG down refuses a stale marker paired with a foreign link and preserves it on delete failure', () => {
	const stale = runAwg('down', path => path === '/etc/autovpn/runtime/awg-owned' ? true : false, () => null,
		path => path === '/etc/autovpn/runtime/awg-owned' ? 'autovpn-awg-v1\n' : null);
	assert.equal(stale, 0);
	const foreign = runAwg('down', path => path === '/etc/autovpn/runtime/awg-owned' ? true : true,
		() => ({ read: () => JSON.stringify([{ ifalias: 'other', linkinfo: { info_kind: 'wireguard' } }]), close: () => 0 }),
		path => path === '/etc/autovpn/runtime/awg-owned' ? 'autovpn-awg-v1\n' : null);
	assert.equal(foreign, 1);
	const deletionFailed = runAwg('down', () => true,
		argv => argv[0] === 'ip' && argv[1] === '-d'
			? { read: () => JSON.stringify([{ ifalias: 'autovpn-awg-v1', linkinfo: { info_kind: 'amneziawg' } }]), close: () => 0 }
			: { read: () => '', close: () => 1 },
		path => path === '/etc/autovpn/runtime/awg-owned' ? 'autovpn-awg-v1\n' : null);
	assert.equal(deletionFailed, 1);
});
