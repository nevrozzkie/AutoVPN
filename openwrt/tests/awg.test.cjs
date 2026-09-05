'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const lanes = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/lanes.uc'));
const helper = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/awg-helper.uc'), 'utf8');
const adapter = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-lane'), 'utf8');

function runAwg(action, accessResult, popenResult, readResult = () => null) {
	let code;
	const source = helper.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'fsError', 'lstat', 'popen', 'require',
		'type', 'match', 'sort', 'keys', 'join', 'length', 'split', 'int', 'substr', 'json', 'ARGV', 'printf', 'exit', source);
	invoke(
		path => readResult(path), () => null, () => true, () => true, () => true, path => accessResult(path),
		() => 'No such file or directory', () => null,
		path => popenResult(path), name => name === 'autovpn.process' ? { popen: popenResult }
			: name === 'autovpn.lanes' ? lanes : null,
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

function awgHarness({ commandFailure, routes = [], marker, link, lane = 'vpn', profileValue,
	engineReceipt, engineReceiptInfo, engineReceiptError, engineReceiptReadFailure,
	action = 'up' } = {}) {
	const descriptor = lanes.get(lane);
	const runtimeRoot = descriptor.root;
	const deviceName = descriptor.awg.device;
	const alias = lane === 'vpn_zapret' ? 'autovpn-awg-zapret-v1' : 'autovpn-awg-v1';
	const value = profileValue || profile();
	const files = new Map([[runtimeRoot + '/awg.json', JSON.stringify(value)]]);
	if (engineReceipt !== undefined)
		files.set('/usr/share/autovpn/awg-engine.json', engineReceipt);
	if (marker != null) files.set(runtimeRoot + '/awg-owned', marker);
	let device = link || null;
	const calls = [];
	let code;
	const source = helper.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'fsError', 'lstat', 'popen', 'require',
		'type', 'match', 'sort', 'keys', 'join', 'length', 'split', 'int', 'substr', 'json', 'ARGV', 'printf', 'exit', source);
	function access(path) {
		if (path === '/usr/bin/awg' || path === '/sys/module/amneziawg') return true;
		if (path === '/sys/class/net/' + deviceName) return device != null;
		return files.has(path);
	}
	function popen(argv) {
		calls.push(argv);
	if (argv.join(' ') === '/sbin/ip -j -4 route show table main')
			return { read: () => JSON.stringify(routes), close: () => 0 };
	if (argv.join(' ') === '/sbin/ip -j -4 address show')
			return { read: () => '[]', close: () => 0 };
	if (argv.join(' ') === '/sbin/ip -d -j link show dev ' + deviceName)
			return { read: () => device == null ? '' : JSON.stringify([{ ifalias: device.alias, linkinfo: { info_kind: device.kind } }]), close: () => device == null ? 1 : 0 };
		const failing = commandFailure && commandFailure(argv);
		return {
			read: () => '',
			close: () => {
				if (failing) return 1;
			if (argv.join(' ') === '/sbin/ip link add dev ' + deviceName + ' alias ' + alias + ' type amneziawg') device = { alias, kind: 'amneziawg' };
			if (argv.join(' ') === '/sbin/ip link del dev ' + deviceName) device = null;
				return 0;
			}
		};
	}
	invoke(
		(path, limit) => engineReceiptReadFailure && path === '/usr/share/autovpn/awg-engine.json'
			? null : files.has(path) ? files.get(path).slice(0, limit) : null,
		(path, value) => { files.set(path, value); return value.length; }, () => true,
		(from, to) => { files.set(to, files.get(from)); files.delete(from); return true; }, path => files.delete(path), access,
		() => engineReceiptError || 'No such file or directory',
		path => {
			if (path !== '/usr/share/autovpn/awg-engine.json') return null;
			if (engineReceiptInfo !== undefined) return engineReceiptInfo;
			return engineReceipt === undefined ? null : { type: 'file', uid: 0 };
		},
		popen,
		name => name === 'autovpn.process' ? { popen } : name === 'autovpn.lanes' ? lanes : null,
		value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : Number.isInteger(value) ? 'int' : typeof value,
		(value, expression) => value.match(expression), value => Array.isArray(value) ? value.slice().sort() : Object.keys(value).sort(), Object.keys,
		(separator, values) => values.join(separator), value => value.length, (value, separator) => value.split(separator),
		value => Number.parseInt(value, 10), (value, start, length) => value.substr(start, length), JSON.parse,
		[action, runtimeRoot + '/awg.json', lane], () => {}, value => { code = value; }
	);
	return { code, calls, files, device, descriptor, alias, profile: value };
}

test('AWG helper requires an AmneziaWG kernel link and never invokes ordinary wg', () => {
	assert.match(helper, /IP, 'link', 'add', 'dev', DEVICE, 'alias', ALIAS, 'type', 'amneziawg/);
	assert.match(helper, /awg, 'setconf', DEVICE, configPath/);
	assert.doesNotMatch(helper, /['"]wg['"]/);
	assert.match(helper, /chmod\(path \+ '\.new', 0o600\)/);
	assert.match(helper, /Never delete an interface that was not created by this controller/);
	assert.match(helper, /access\(OWNED\) !== true/);
	assert.match(helper, /const MARKER = ALIAS \+ '\\n'/);
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
	assert.doesNotMatch(env.files.get('/etc/autovpn/runtime/awg.conf'), /(?:S3|S4|AdvancedSecurity)/,
		'no receipt preserves the existing AWG1 config bytes');
});

test('exact UAPI2 receipt renders the documented AWG1-on-UAPI2 fields in both lanes', () => {
	const receipt = JSON.stringify({ schema_version: 1, config_mode: 'awg1-on-uapi2' });
	const primary = awgHarness({ engineReceipt: receipt });
	assert.equal(primary.code, 0);
	const primaryConfig = primary.files.get('/etc/autovpn/runtime/awg.conf');
	assert.match(primaryConfig, /S3 = 0\nS4 = 0\n/);
	assert.match(primaryConfig, /\[Peer\][\s\S]*AdvancedSecurity = on\n/);
	assert.doesNotMatch(primaryConfig, /\nI[1-5] =/);

	const secondaryProfile = profile();
	delete secondaryProfile.legacy_amnezia_vpn_import_key;
	secondaryProfile.interface.private_key = 'D'.repeat(43) + '=';
	secondaryProfile.interface.address = '10.66.66.9/32';
	const secondary = awgHarness({ lane: 'vpn_zapret', profileValue: secondaryProfile, engineReceipt: receipt });
	assert.equal(secondary.code, 0);
	const secondaryConfig = secondary.files.get('/etc/autovpn/runtime-zapret/awg.conf');
	assert.match(secondaryConfig, /S3 = 0\nS4 = 0\n/);
	assert.match(secondaryConfig, /\[Peer\][\s\S]*AdvancedSecurity = on\n/);
	assert.doesNotMatch(secondaryConfig, /\nI[1-5] =/);
});

test('present receipt must be the exact supported UAPI2 contract and otherwise fails closed', () => {
	for (const receipt of [
		'{',
		JSON.stringify({ schema_version: 2, config_mode: 'awg1-on-uapi2' }),
		JSON.stringify({ schema_version: '1', config_mode: 'awg1-on-uapi2' }),
		JSON.stringify({ schema_version: 1, config_mode: 'unknown' }),
		JSON.stringify({ schema_version: 1, config_mode: 'awg1-on-uapi2', extra: true }),
		'x'.repeat(513),
	]) {
		const env = awgHarness({ engineReceipt: receipt });
		assert.equal(env.code, 1);
		assert.equal(env.files.has('/etc/autovpn/runtime/awg-owned'), false);
		assert.equal(env.calls.some(argv => argv.includes('amneziawg')), false);
		assert.equal(awgHarness({ engineReceipt: receipt, action: 'available' }).code, 1);
	}
});

test('receipt read failures, non-files and non-root files never fall back to legacy AWG1', () => {
	for (const options of [
		{ engineReceiptError: 'Permission denied' },
		{ engineReceipt: '{"schema_version":1,"config_mode":"awg1-on-uapi2"}', engineReceiptReadFailure: true },
		{ engineReceipt: '{"schema_version":1,"config_mode":"awg1-on-uapi2"}', engineReceiptInfo: { type: 'directory', uid: 0 } },
		{ engineReceipt: '{"schema_version":1,"config_mode":"awg1-on-uapi2"}', engineReceiptInfo: { type: 'file', uid: 1000 } },
	]) {
		const env = awgHarness(options);
		assert.equal(env.code, 1);
		assert.equal(env.files.has('/etc/autovpn/runtime/awg-owned'), false);
		assert.equal(awgHarness({ ...options, action: 'available' }).code, 1);
	}
});

test('secondary AWG owns a separate root, interface, alias and outer mark without an import key', () => {
	const secondaryProfile = profile();
	delete secondaryProfile.legacy_amnezia_vpn_import_key;
	secondaryProfile.interface.private_key = 'D'.repeat(43) + '=';
	secondaryProfile.interface.address = '10.66.66.9/32';
	const env = awgHarness({ lane: 'vpn_zapret', profileValue: secondaryProfile });
	assert.equal(env.code, 0);
	assert.equal(env.files.get('/etc/autovpn/runtime-zapret/awg-owned'), 'autovpn-awg-zapret-v1\n');
	assert.deepEqual(env.device, { alias: 'autovpn-awg-zapret-v1', kind: 'amneziawg' });
	const calls = env.calls.map(argv => argv.join(' '));
	assert.ok(calls.includes('/sbin/ip link add dev avpnwg1 alias autovpn-awg-zapret-v1 type amneziawg'));
	assert.ok(calls.includes('/usr/bin/awg setconf avpnwg1 /etc/autovpn/runtime-zapret/awg.conf'));
	assert.ok(calls.includes('/usr/bin/awg set avpnwg1 fwmark 20214'));
	assert.equal(calls.join(' ').includes(secondaryProfile.interface.private_key), false);
	assert.equal(env.files.get('/etc/autovpn/runtime-zapret/awg.conf').includes(secondaryProfile.interface.private_key), true);

	const wrongShape = awgHarness({ lane: 'vpn_zapret', profileValue: profile() });
	assert.equal(wrongShape.code, 1);
	assert.equal(wrongShape.files.has('/etc/autovpn/runtime-zapret/awg-owned'), false);
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
	assert.match(helper, /let outerMark = LANE\.id == 'vpn_zapret' \? '20214' : '20194'/);
	assert.match(adapter, /ip -4 rule add priority "\$AWG_PRIORITY" fwmark "\$AWG_TABLE" lookup "\$AWG_TABLE"/);
	assert.match(adapter, /ip -4 rule add priority "\$AWG_UNREACHABLE_PRIORITY" fwmark "\$AWG_TABLE" unreachable/);
	assert.match(adapter, /if awg_active && ! awg_up; then/);
	assert.match(adapter, /helper fallback-awg \|\| return 1/);
	assert.match(adapter, /helper disable-awg \|\| return 1/);
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
