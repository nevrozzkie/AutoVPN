'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const moduleRoot = path.join(root, 'files/usr/share/ucode/autovpn');
const lanes = loadUcodeModule(path.join(moduleRoot, 'lanes.uc'));
const zapret = loadUcodeModule(path.join(moduleRoot, 'zapret.uc'));
const machine = loadUcodeModule(path.join(moduleRoot, 'state.uc'));
const clone = value => JSON.parse(JSON.stringify(value));
const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json')));

const type = value => value == null ? null : Array.isArray(value) ? 'array'
	: typeof value === 'boolean' ? 'bool' : Number.isInteger(value) ? 'int' : typeof value;

function loadRuntime() {
	const source = fs.readFileSync(path.join(moduleRoot, 'runtime.uc'), 'utf8');
	const factory = new Function('type', 'length', 'keys', 'sort', 'match', 'push', 'substr', 'int',
		'index', 'replace', 'split', 'lc', 'sprintf', 'json', 'join', 'require', source);
	return factory(type, value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
		Object.keys, value => value.sort(), (value, expression) => value.match(expression),
		(array, value) => array.push(value), (value, start, count) => count == null ? value.substring(start) : value.substring(start, start + count),
		value => Number.parseInt(value, 10), (value, needle) => value.indexOf(needle),
		(value, expression, replacement) => value.replace(expression, replacement),
		(value, separator) => value.split(separator), value => value.toLowerCase(),
		(format, value) => format === '%J' ? JSON.stringify(value) : null, JSON.parse,
		(separator, value) => value.join(separator),
		name => ({ 'autovpn.zapret': zapret, 'autovpn.lanes': lanes })[name]);
}

const runtime = loadRuntime();
const policy = (selection = 'auto', withZapret = true) => ({
	selection, wan_device: 'pppoe-wan', dns_server: '1.1.1.1', direct_domains: ['ru'],
	direct_cidrs: [], hysteria_tls_mode: 'subscription', awg_available: true,
	...(withZapret ? { zapret: { vless: 'split', hysteria2: 'fake', amneziawg: 'off', repeats: 2 } } : {}),
});

function awgProfile(key, address, legacy) {
	const value = {
		protocol_version: 1,
		capabilities: { awg_obfuscation_v1: true, awg2_i_fields: false,
			obfuscation_fields: ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4'] },
		interface: { private_key: key.repeat(43) + '=', address, dns_servers: ['1.1.1.1'] },
		peer: { public_key: 'P'.repeat(43) + '=', preshared_key: 'S'.repeat(43) + '=',
			endpoint: { host: base.server.endpoint, port: 51820 }, persistent_keepalive: 25 },
		obfuscation: { Jc: 3, Jmin: 20, Jmax: 700, S1: 30, S2: 30, H1: 1, H2: 2, H3: 3, H4: 4 },
		route_allowed_ips: ['0.0.0.0/0', '::/0'], install_routes: false,
	};
	if (legacy) value.legacy_amnezia_vpn_import_key = 'vpn://opaque';
	return value;
}

function snapshot4() {
	const snapshot = clone(base);
	snapshot.schema_version = 4;
	snapshot.protocols.hysteria2 = { enabled: true, outbound: {
		type: 'hysteria2', tag: 'hysteria2', server: base.server.endpoint, server_port: 8443,
		password: 'bounded-secret', tls: { enabled: true, server_name: 'example.com', insecure: false },
	} };
	snapshot.protocols.amneziawg = { enabled: true, profile: awgProfile('A', '10.66.66.8/32', true) };
	snapshot.protocols.amneziawg_aux = { enabled: true, profile: awgProfile('B', '10.66.66.9/32', false) };
	return snapshot;
}

const entry = snapshot => ({ etag: '"' + 'a'.repeat(64) + '"', snapshot, attempt: 7 });

test('schema 4 renders disjoint lane resources and never reuses the primary AWG identity', () => {
	const snapshot = snapshot4();
	const primary = runtime.render(snapshot, policy('amneziawg'), machine, null, 'vpn');
	const secondary = runtime.render(snapshot, policy('amneziawg'), machine, null, 'vpn_zapret');
	assert.equal(primary.ok, true);
	assert.equal(secondary.ok, true);
	assert.deepEqual(primary.awg, snapshot.protocols.amneziawg.profile);
	assert.deepEqual(secondary.awg, snapshot.protocols.amneziawg_aux.profile);
	assert.notEqual(primary.awg.interface.private_key, secondary.awg.interface.private_key);
	assert.deepEqual(primary.config.inbounds.slice(0, 2), [
		{ type: 'tun', tag: 'vpn-net', interface_name: 'avpn0', address: ['172.30.255.1/30'], mtu: 1400,
			auto_route: false, auto_redirect: false, stack: 'system' },
		{ type: 'socks', tag: 'health', listen: '127.0.0.1', listen_port: 1088 },
	]);
	assert.deepEqual(secondary.config.inbounds.slice(0, 2), [
		{ type: 'tun', tag: 'vpn-net', interface_name: 'avpn1', address: ['172.30.255.5/30'], mtu: 1400,
			auto_route: false, auto_redirect: false, stack: 'system' },
		{ type: 'socks', tag: 'health', listen: '127.0.0.1', listen_port: 1108 },
	]);
	assert.deepEqual(primary.config.outbounds.find(item => item.tag === 'amneziawg'),
		{ type: 'direct', tag: 'amneziawg', bind_interface: 'avpnwg0', routing_mark: 20193, domain_resolver: 'awg-dns' });
	assert.deepEqual(secondary.config.outbounds.find(item => item.tag === 'amneziawg'),
		{ type: 'direct', tag: 'amneziawg', bind_interface: 'avpnwg1', routing_mark: 20213, domain_resolver: 'awg-dns' });
	assert.equal(primary.zapret, null);
	assert.equal(secondary.zapret.flows.some(flow => flow.profile === 'vless-reality'), true);
	assert.deepEqual(secondary.config.inbounds.filter(item => item.tag.startsWith('probe-')).map(item => item.listen_port),
		[1109, 1110, 1111]);
});

test('explicit schema 4 bundles bind a lane while legacy and schema 3 contracts remain exact', () => {
	const current = snapshot4();
	const primary = runtime.bundle(entry(current), policy(), machine, null, 'vpn').value;
	const secondary = runtime.bundle(entry(current), policy(), machine, null, 'vpn_zapret').value;
	assert.equal(primary.version, 3);
	assert.equal(primary.lane, 'vpn');
	assert.equal('zapret' in primary, false);
	assert.equal(secondary.version, 3);
	assert.equal(secondary.lane, 'vpn_zapret');
	assert.equal(runtime.matchesBundle(primary, entry(current), machine, 'vpn'), true);
	assert.equal(runtime.matchesBundle(primary, entry(current), machine, 'vpn_zapret'), false);
	assert.equal(runtime.matchesBundle({ ...secondary, lane: 'vpn' }, entry(current), machine), false);

	const oldDefault = runtime.bundle(entry(clone(base)), policy(), machine).value;
	const oldExplicit = runtime.bundle(entry(clone(base)), policy(), machine, null, 'vpn').value;
	assert.deepEqual(oldExplicit, oldDefault);
	assert.equal(oldDefault.version, 2);
	assert.equal('lane' in oldDefault, false);
	assert.deepEqual(runtime.bundle(entry(clone(base)), policy(), machine, null, 'vpn_zapret'),
		{ ok: true, empty: true, lane: 'vpn_zapret' });
});

test('whole snapshot validation precedes lane selection', () => {
	const rejecting = { ...machine, validateSnapshot: () => ({ ok: false }) };
	assert.deepEqual(runtime.render(snapshot4(), policy(), rejecting, null, '../vpn'),
		{ ok: false, code: 'snapshot_validation_failed' });
	assert.deepEqual(runtime.render(snapshot4(), policy(), machine, null, '../vpn'),
		{ ok: false, code: 'invalid_lane' });
});

function helperHarness(snapshot, uciValues = {}) {
	const storage = new Map([['/etc/autovpn/state/journal.json', JSON.stringify({
		phase: 'READY', desired: entry(snapshot), applied: null, last_good: null,
	})]]);
	let output;
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-helper.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'access', 'cursor', 'require',
		'length', 'sprintf', 'type', 'match', 'index', 'json', 'ARGV', 'printf', 'exit', 'int', source);
	return {
		storage,
		run(lane, action = 'prepare') {
			invoke(
				(file, limit) => storage.has(file) ? storage.get(file).slice(0, limit) : null,
				(file, value) => { storage.set(file, value); return value.length; }, () => true,
				(from, to) => { storage.set(to, storage.get(from)); storage.delete(from); return true; },
				() => false,
				() => ({ load() {}, get(_config, section, key) {
					if (section === 'main') return base.router_id;
					if (section === 'runtime_zapret' && key === 'selection') return 'hysteria2';
					return uciValues[key] ?? ({ selection: 'vless-reality', wan_device: 'pppoe-wan',
						dns_server: '1.1.1.1', direct_domains: [], direct_cidrs: [],
						hysteria_tls_mode: 'subscription', zapret_enabled: '0' })[key];
				} }),
				name => ({ 'autovpn.state': machine, 'autovpn.journal': { validState: () => true },
					'autovpn.runtime': runtime, 'autovpn.process': {}, 'autovpn.lanes': lanes })[name],
				value => value.length, (format, value) => JSON.stringify(value ?? null) + (format.endsWith('\n') ? '\n' : ''),
				type, (value, expression) => value.match(expression), (values, value) => values.indexOf(value),
				JSON.parse, [action, '/etc/autovpn/state/journal.json', null, lane],
				(_format, value) => { output = value; }, () => {}, Number,
			);
			return output;
		},
	};
}

test('runtime helper reads one typed RU bypass setting for both lanes', () => {
	for (const value of [undefined, '0', '1', 'invalid']) {
		for (const lane of ['vpn', 'vpn_zapret']) {
			const env = helperHarness(snapshot4(), { ru_bypass: value });
			const result = env.run(lane);
			assert.equal(result.ok, value !== 'invalid');
			if (!result.ok) continue;
			const directory = lane === 'vpn' ? 'runtime' : 'runtime-zapret';
			const bundle = JSON.parse(env.storage.get('/etc/autovpn/' + directory + '/prepared.json'));
			assert.equal(bundle.policy.ru_bypass, value !== '0');
			assert.equal(Boolean(bundle.config.route.rule_set), value !== '0');
		}
	}
});

test('runtime helper stages independent roots and secondary remains VPN-capable with zapret disabled', () => {
	const env = helperHarness(snapshot4());
	const primary = env.run('vpn');
	const secondary = env.run('vpn_zapret');
	assert.deepEqual({ lane: primary.lane, legacy: primary.legacy_transport, profile: primary.active_profile },
		{ lane: 'vpn', legacy: false, profile: 'vless-reality' });
	assert.deepEqual({ lane: secondary.lane, legacy: secondary.legacy_transport, profile: secondary.active_profile },
		{ lane: 'vpn_zapret', legacy: false, profile: 'hysteria2' });
	const primaryBundle = JSON.parse(env.storage.get('/etc/autovpn/runtime/prepared.json'));
	const secondaryBundle = JSON.parse(env.storage.get('/etc/autovpn/runtime-zapret/prepared.json'));
	assert.equal(primaryBundle.version, 3);
	assert.equal(secondaryBundle.version, 3);
	assert.equal('zapret' in primaryBundle, false);
	assert.equal('zapret' in secondaryBundle, false);
	assert.equal(primaryBundle.config.inbounds[0].interface_name, 'avpn0');
	assert.equal(secondaryBundle.config.inbounds[0].interface_name, 'avpn1');
});

test('secondary helper is explicitly empty for schema 3 and rejects path-like lane ids', () => {
	const env = helperHarness(clone(base));
	assert.deepEqual(env.run('vpn_zapret'),
		{ ok: true, empty: true, lane: 'vpn_zapret', legacy_transport: false });
	assert.equal([...env.storage.keys()].some(file => file.startsWith('/etc/autovpn/runtime-zapret/')), false);
	assert.deepEqual(env.run('../vpn'), { ok: false, code: 'invalid_lane' });
});

test('first dual activation needs no nonexistent legacy secondary bundle, rollback remains empty', () => {
	const desired = entry(snapshot4());
	const old = entry(clone(base));
	const env = helperHarness(desired.snapshot);
	const setState = phase => env.storage.set('/etc/autovpn/state/journal.json',
		JSON.stringify({ phase, desired, applied: old, last_good: old }));
	setState('PREPARING');
	assert.equal(env.run('vpn_zapret').ok, true);
	setState('ACTIVATING');
	assert.equal(env.run('vpn_zapret', 'activate').ok, true);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime-zapret/current.json')).lane, 'vpn_zapret');
	assert.equal(env.storage.has('/etc/autovpn/runtime-zapret/previous.json'), false);
	setState('ROLLING_BACK');
	assert.deepEqual(env.run('vpn_zapret', 'rollback'),
		{ ok: true, empty: true, lane: 'vpn_zapret', legacy_transport: false });
});

test('an established dual lane still requires its previous applied bundle before replacement', () => {
	const env = helperHarness(snapshot4());
	assert.equal(env.run('vpn_zapret').ok, true);
	const old = entry(snapshot4());
	old.attempt = 6;
	env.storage.set('/etc/autovpn/state/journal.json', JSON.stringify({
		phase: 'ACTIVATING', desired: entry(snapshot4()), applied: old, last_good: old,
	}));
	assert.deepEqual(env.run('vpn_zapret', 'activate'), { ok: false, code: 'rollback_bundle_missing' });
	assert.equal(env.storage.has('/etc/autovpn/runtime-zapret/current.json'), false);
});
