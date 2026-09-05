'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { spawnSync } = require('node:child_process');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.resolve(__dirname, '..');
const moduleRoot = path.join(root, 'files/usr/share/ucode/autovpn');
const runtime = loadUcodeModule(path.join(moduleRoot, 'runtime.uc'));
const machine = loadUcodeModule(path.join(moduleRoot, 'state.uc'));
const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json')));
const clone = value => JSON.parse(JSON.stringify(value));
const policy = () => ({ selection: 'auto', wan_device: 'eth1', dns_server: '1.1.1.1', direct_domains: ['ru', 'xn--p1ai'], direct_cidrs: [], hysteria_tls_mode: 'subscription', awg_available: false });
const entry = (attempt = 1) => ({ etag: '"' + 'a'.repeat(64) + '"', snapshot: clone(snapshot), attempt });

function withHysteria(insecure) {
	const result = clone(snapshot);
	result.protocols.hysteria2 = { enabled: true, outbound: {
		type: 'hysteria2', tag: 'hysteria2', server: snapshot.server.endpoint, server_port: 8443,
		password: 'test-secret', tls: { enabled: true, server_name: 'example.com', insecure }
	} };
	return result;
}

test('runtime routes only explicit direct exceptions and health always takes VPN', () => {
	const result = runtime.render(snapshot, { ...policy(), direct_cidrs: ['0.0.0.0/0'] }, machine);
	assert.equal(result.ok, true);
	assert.equal(result.config.route.final, 'auto');
	assert.deepEqual(result.config.route.rules[0], { inbound: ['health'], action: 'route', outbound: 'auto' });
	assert.equal(result.config.outbounds.find(outbound => outbound.type === 'urltest').outbounds.includes('direct'), false);
	assert.equal(result.config.dns.servers[0].detour, 'auto');
	assert.equal(result.config.inbounds[0].auto_route, false);
	assert.equal(result.config.inbounds[0].auto_redirect, false);
	assert.equal(result.config.inbounds[1].listen, '127.0.0.1');
	assert.equal(result.config.outbounds.find(outbound => outbound.type === 'direct').bind_interface, 'eth1');
	assert.ok(result.config.route.rules.findIndex(rule => rule.ip_is_private) < result.config.route.rules.findIndex(rule => rule.ip_cidr));
});

test('Hysteria subscription mode preserves insecure verification setting; strict mode rejects it', () => {
	assert.equal(runtime.render(withHysteria(true), policy(), machine).capabilities.hysteria2, true);
	assert.equal(runtime.render(withHysteria(true), { ...policy(), selection: 'hysteria2' }, machine).ok, true);
	assert.equal(runtime.render(withHysteria(true), { ...policy(), hysteria_tls_mode: 'strict', selection: 'hysteria2' }, machine).code, 'hysteria_tls_unverified');
	const secure = runtime.render(withHysteria(false), { ...policy(), selection: 'hysteria2' }, machine);
	assert.equal(secure.ok, true);
	assert.equal(secure.config.route.final, 'hysteria2');
	assert.equal(runtime.render(snapshot, { ...policy(), selection: 'hysteria2' }, machine).code, 'selected_vpn_unavailable');
	assert.equal(runtime.render(snapshot, { ...policy(), selection: 'amneziawg' }, machine).code, 'selected_vpn_unavailable');
});

function withAwg() {
	const result = clone(snapshot);
	result.protocols.amneziawg = { enabled: true, profile: {
		protocol_version: 1,
		capabilities: { awg_obfuscation_v1: true, awg2_i_fields: false, obfuscation_fields: ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4'] },
		interface: { private_key: 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=', address: '10.66.66.8/32', dns_servers: ['1.1.1.1'] },
		peer: { public_key: 'BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB=', preshared_key: 'CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC=', endpoint: { host: snapshot.server.endpoint, port: 51820 }, persistent_keepalive: 25 },
		obfuscation: { Jc: 3, Jmin: 20, Jmax: 700, S1: 30, S2: 30, H1: 1, H2: 2, H3: 3, H4: 4 },
		route_allowed_ips: ['0.0.0.0/0', '::/0'], install_routes: false, legacy_amnezia_vpn_import_key: 'vpn://opaque'
	} };
	return result;
}

test('AmneziaWG is opt-in by detected kernel tool and gets a marked kernel direct outbound', () => {
	const material = withAwg();
	assert.equal(runtime.render(material, policy(), machine).capabilities.amneziawg, false);
	const rendered = runtime.render(material, { ...policy(), awg_available: true, selection: 'amneziawg' }, machine);
	assert.equal(rendered.ok, true);
	assert.equal(rendered.profile, 'amneziawg');
	assert.deepEqual(rendered.awg, material.protocols.amneziawg.profile);
	assert.deepEqual(rendered.config.outbounds.find(item => item.tag === 'amneziawg'),
		{ type: 'direct', tag: 'amneziawg', bind_interface: 'avpnwg0', routing_mark: 20193 });
	assert.equal(runtime.render(material, { ...policy(), awg_available: false, selection: 'amneziawg' }, machine).code, 'selected_vpn_unavailable');
	const manualVless = runtime.render(material, { ...policy(), awg_available: true, selection: 'vless-reality' }, machine);
	assert.equal(manualVless.ok, true);
	assert.equal(manualVless.awg, null);
	material.protocols.amneziawg.profile.obfuscation.Jc = 65536;
	assert.equal(runtime.render(material, { ...policy(), awg_available: true, selection: 'amneziawg' }, machine).code, 'selected_vpn_unavailable');
});

test('runtime helper uses an absolute modprobe path for stable process dispatch', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-helper.uc'), 'utf8');
	assert.match(source, /command\(\['\/sbin\/modprobe', 'amneziawg'\]\)/);
});

test('runtime rejects policy injection, malformed addresses and server global config', () => {
	for (const override of [
		{ wan_device: 'eth1;touch /tmp/pwned' }, { wan_device: 'avpn0' },
		{ direct_domains: ['*.ru'] }, { direct_domains: ['https://example.com'] },
		{ direct_cidrs: ['999.1.1.1/24'] }, { direct_cidrs: ['1.2.3.4/99'] },
		{ dns_server: '1.1.1.01' }, { injected: true }
	]) assert.equal(runtime.render(snapshot, { ...policy(), ...override }, machine).ok, false, JSON.stringify(override));
	const bad = clone(snapshot);
	bad.protocols.vless.outbound.detour = 'direct';
	assert.equal(runtime.render(bad, policy(), machine).code, 'snapshot_validation_failed');
});

test('persistent bundle binds device, snapshot and local-policy attempt, rejects corruption', () => {
	const original = entry();
	const bundle = runtime.bundle(original, policy(), machine).value;
	assert.equal(runtime.matchesBundle(bundle, original, machine), true);
	for (const mutated of [
		{ ...bundle, router_id: 'foreign_router' }, { ...bundle, attempt: 2 },
		{ ...bundle, config: { ...bundle.config, route: { final: 'direct' } } },
		{ ...bundle, extra: 'unknown' }
	]) assert.equal(runtime.matchesBundle(mutated, original, machine), false);
	assert.equal(runtime.matchesBundle(bundle, entry(2), machine), false);
	const longDomain = [63, 63, 63, 61].map(size => 'a'.repeat(size)).join('.');
	assert.equal(runtime.bundle(original, {
		...policy(), direct_domains: Array(128).fill(longDomain), direct_cidrs: Array(128).fill('203.0.113.0/24')
	}, machine).code, 'runtime_bundle_too_large');
});

test('explicit policy apply reuses server snapshot with a new transaction and report key', () => {
	const state = machine.initialState();
	state.applied = entry();
	state.sequence = 3;
	assert.equal(machine.receiveSnapshot(state, snapshot, entry().etag).noop, true);
	assert.equal(machine.receiveSnapshot(state, snapshot, entry().etag, true).noop, false);
	assert.equal(state.desired.attempt, 4);
	assert.equal(state.phase, 'READY');
});

function helperHarness() {
	const storage = new Map();
	const journal = loadUcodeModule(path.join(moduleRoot, 'journal.uc'));
	const state = machine.initialState();
	let localPolicy = policy();
	let localRouter = snapshot.router_id;
	let failPath = '';
	let output;
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-helper.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'access', 'cursor', 'require',
		'length', 'sprintf', 'type', 'match', 'json', 'ARGV', 'printf', 'exit', source);
	return {
		state, storage,
		setPolicy(value) { localPolicy = value; },
		setRouter(value) { localRouter = value; },
		failWrite(value) { failPath = value; },
		run(action) {
			storage.set('/etc/autovpn/state/journal.json', JSON.stringify(state));
			invoke(
				(file, limit) => storage.has(file) ? storage.get(file).slice(0, limit) : null,
			(file, value) => { if (file === failPath) return null; storage.set(file, value); return value.length; },
				() => true,
				(from, to) => { storage.set(to, storage.get(from)); storage.delete(from); return true; },
				() => null,
				() => ({ load() {}, get(_config, section, key) { return section === 'main' ? localRouter : localPolicy[key]; } }),
				name => ({ 'autovpn.state': machine, 'autovpn.journal': journal, 'autovpn.runtime': runtime })[name],
				value => value.length,
				(format, value) => JSON.stringify(value) + (format.endsWith('\n') ? '\n' : ''),
				value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value,
				(value, expression) => value.match(expression), JSON.parse,
				[action, '/etc/autovpn/state/journal.json'], (_format, value) => { output = value; }, () => {}
			);
			return output;
		},
		begin() {
			machine.receiveSnapshot(state, clone(snapshot), entry().etag, true);
			machine.beginApply(state);
		},
		activate() { machine.prepared(state); machine.activating(state); return this.run('activate'); },
		commit() {
			machine.activated(state);
			const verified = this.run('verify');
			machine.verified(state, verified);
			machine.reportAccepted(state, state.pending_report.idempotency_key);
		}
	};
}

test('helper rollback after activation restores the old policy, including same-ETag local edits', () => {
	const env = helperHarness();
	env.begin();
	assert.equal(env.run('prepare').ok, true);
	assert.equal(env.activate().ok, true);
	env.commit();
	const previous = env.storage.get('/etc/autovpn/runtime/current.json');
	env.setPolicy({ ...policy(), selection: 'vless-reality', direct_cidrs: ['0.0.0.0/0'] });
	env.begin();
	assert.equal(env.run('prepare').ok, true);
	assert.equal(env.activate().ok, true);
	assert.notEqual(env.storage.get('/etc/autovpn/runtime/current.json'), previous);
	// Power loss before verify/commit: journal still identifies the old applied entry.
	assert.equal(env.run('rollback').ok, true);
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), previous);
});

test('helper refuses foreign router and failed backup without replacing the current bundle', () => {
	const env = helperHarness();
	env.begin(); env.run('prepare'); env.activate(); env.commit();
	const current = env.storage.get('/etc/autovpn/runtime/current.json');
	env.setRouter('different_router');
	assert.equal(env.run('restore').code, 'router_identity_mismatch');
	env.setRouter(snapshot.router_id);
	env.begin(); env.run('prepare');
	env.failWrite('/etc/autovpn/runtime/previous.json.new');
	assert.equal(env.activate().code, 'runtime_write_failed');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
});

test('controller restores VPN before fetching and never hides restore failure behind HTTP 304', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/controller.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '').split('\nlet config = configuration();')[0];
	const factory = new Function('require', 'popen', 'access', 'chmod', 'type', 'length', 'match', 'json', 'push', source + '\nreturn refresh;');
	for (const restoreOk of [true, false]) {
		const calls = [];
		const refresh = factory(
			name => name === 'autovpn.process' ? { popen: argv => {
				calls.push(argv[1]);
				const response = argv[1] === 'restore' ? { ok: restoreOk, code: 'restore_test' } :
					argv[1] === 'fetch' ? { ok: true, status: 304 } : { ok: true };
				return { read() { return JSON.stringify(response); }, close() { return 0; } };
			} } : loadUcodeModule(path.join(moduleRoot, name.split('.').pop() + '.uc')),
			argv => {
				calls.push(argv[1]);
				const response = argv[1] === 'restore' ? { ok: restoreOk, code: 'restore_test' } :
					argv[1] === 'fetch' ? { ok: true, status: 304 } : { ok: true };
				return { read() { return JSON.stringify(response); }, close() { return 0; } };
			},
			() => true, () => true,
			value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value,
			value => value.length, (value, expression) => value.match(expression), JSON.parse,
			(array, value) => array.push(value)
		);
		const state = machine.initialState();
		state.applied = entry();
		const result = refresh({ base_url: 'https://example.com', router_id: snapshot.router_id,
			state_dir: '/etc/autovpn/state', credential_file: '/etc/autovpn/credentials',
			runtime_adapter: '/runtime', http_adapter: '/http' }, state);
		assert.equal(result.ok, restoreOk);
		assert.ok(calls.indexOf('restore') < calls.indexOf('fetch'));
		assert.equal(calls.includes('fail-closed'), !restoreOk);
		if (!restoreOk) assert.equal(result.code, 'restore_test');
	}
});

function fixture(t, scenario = '') {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-runtime-test-'));
	t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
	const work = path.join(directory, 'runtime');
	const bin = path.join(directory, 'bin');
	fs.mkdirSync(work);
	fs.mkdirSync(bin);
	for (const name of ['ip', 'nft', 'uci', 'ucode', 'curl', 'sing-box', 'service'])
		fs.symlinkSync(path.join(__dirname, 'fake-runtime.cjs'), path.join(bin, name));
	let source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-adapter'), 'utf8');
	source = source.replace('ROOT=/etc/autovpn/runtime', 'ROOT=' + work)
		.replace('GATE_ROOT=/etc/autovpn/state', 'GATE_ROOT=' + work)
		.replace('SERVICE=/etc/init.d/autovpn-tunnel', 'SERVICE=' + path.join(bin, 'service'))
		.replace('chmod 0700 /etc/autovpn "$ROOT"', 'chmod 0700 "$ROOT"');
	const adapter = path.join(directory, 'adapter');
	fs.writeFileSync(adapter, source);
	return { directory, work, run(action) {
		return spawnSync('/bin/sh', [adapter, action, '/etc/autovpn/state/journal.json'], {
			encoding: 'utf8', timeout: 15000,
			env: { ...process.env, PATH: bin + ':' + process.env.PATH, AUTOVPN_TEST_DIR: directory, AUTOVPN_TEST_SCENARIO: scenario }
		});
	}, events() { return fs.readFileSync(path.join(directory, 'events'), 'utf8').trim().split('\n').map(JSON.parse); } };
}

test('shell runtime checks candidate, closes guard before restart, opens only after two VPN probes', t => {
	const env = fixture(t);
	for (const action of ['prepare', 'activate', 'verify']) {
		const result = env.run(action);
		assert.equal(result.status, 0, result.stdout + result.stderr);
	}
	const events = env.events();
	const at = text => events.findIndex(event => event.join(' ').includes(text));
	assert.ok(at('sing-box check') < at('guard-closed.nft'));
	assert.ok(at('guard-closed.nft') < at('service stop'));
	assert.ok(at('service start') < at('curl --disable'));
	assert.ok(at('curl --disable') < at('guard-open.nft'));
	assert.equal(events.filter(event => event[0] === 'curl').length, 2);
	assert.ok(events.some(event => event.join(' ').includes('priority 20192 iif br-avpn unreachable')));
});
test('maintenance gates block every runtime opening path including dangling symlinks', t => {
	for (const gate of ['maintenance.lock', 'update.lock']) {
		const env = fixture(t);
		fs.symlinkSync('missing-target', path.join(env.work, gate));
		for (const action of ['prepare', 'activate', 'verify', 'rollback', 'restore']) {
			const result = env.run(action);
			assert.equal(result.status, 1);
			assert.equal(JSON.parse(result.stdout).code, 'maintenance_locked');
		}
		assert.equal(env.events().some(event => event.join(' ').includes('guard-open.nft')), false);
		assert.equal(env.events().some(event => event.join(' ').includes('service start')), false);
	}
});

test('runtime refuses to activate while SSID transaction needs confirmation', t => {
	const env = fixture(t, 'networks-pending');
	assert.equal(env.run('activate').status, 1);
	assert.equal(env.events().some(event => event.join(' ').includes('service start')), false);
	assert.equal(env.events().some(event => event.join(' ').includes('guard-open.nft')), false);
});

test('failed HTTPS probe does not open forwarding; fail-closed does not consult journal/helper', t => {
	const env = fixture(t, 'probe-failed');
	assert.equal(env.run('activate').status, 0);
	assert.equal(env.run('verify').status, 1);
	assert.equal(env.events().some(event => event.join(' ').includes('guard-open.nft')), false);
	const before = env.events().length;
	assert.equal(env.run('fail-closed').status, 0);
	assert.equal(env.events().slice(before).some(event => event[0] === 'ucode'), false);
});

test('foreign nft ownership and route priority collisions are refused without flushing', t => {
	for (const scenario of ['foreign-table', 'foreign-route']) {
		const env = fixture(t, scenario);
		assert.equal(env.run('activate').status, 1);
		const events = env.events();
		assert.equal(events.some(event => event.join(' ').includes('guard-open.nft')), false);
		assert.equal(events.some(event => event.join(' ').includes('route flush')), false);
		if (scenario === 'foreign-table') assert.equal(events.some(event => event[0] === 'nft' && event[1] === '-f'), false);
	}
});

test('offload preflight rejects activation and reboot restore starts the committed config', t => {
	const blocked = fixture(t, 'offload');
	assert.equal(blocked.run('prepare').status, 1);
	const env = fixture(t);
	assert.equal(env.run('restore').status, 0);
	assert.ok(env.events().some(event => event.join(' ') === 'service start'));
});

test('restore reuses a verified AWG device only when the generated profile still matches', t => {
	const env = fixture(t);
	fs.writeFileSync(path.join(env.work, 'awg.json'), '{"interface":{"private_key":"test"}}');
	fs.writeFileSync(path.join(env.work, 'awg-run.json'), '{"interface":{"private_key":"test"}}');
	fs.writeFileSync(path.join(env.work, 'candidate.json'), '{"test":"generated"}');
	fs.writeFileSync(path.join(env.work, 'run.json'), '{"test":"generated"}');
	fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
	const result = env.run('restore');
	assert.equal(result.status, 0, result.stdout + result.stderr);
	const events = env.events().map(event => event.join(' '));
	assert.equal(events.some(event => event === 'service stop'), false);
	assert.equal(events.some(event => event === 'service start'), false);
	assert.ok(events.some(event => event.includes('awg-helper.uc check')));
});

test('generated sing-box configuration passes the real binary when configured', { skip: !process.env.AUTOVPN_SING_BOX }, () => {
	const material = withAwg();
	material.protocols.hysteria2 = withHysteria(false).protocols.hysteria2;
	// Validly encoded dummy public key for offline config validation, never a real credential.
	material.protocols.vless.outbound.tls.reality.public_key = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
	const selections = process.platform === 'linux'
		? ['auto', 'vless-reality', 'hysteria2', 'amneziawg']
		: ['auto', 'vless-reality', 'hysteria2'];
	for (const selection of selections) {
		const rendered = runtime.render(material, { ...policy(), awg_available: process.platform === 'linux', selection }, machine);
		const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-singbox-check-'));
		try {
			const file = path.join(directory, 'config.json');
			fs.writeFileSync(file, JSON.stringify(rendered.config), { mode: 0o600 });
			const checked = spawnSync(process.env.AUTOVPN_SING_BOX, ['check', '-c', file], { encoding: 'utf8', timeout: 15000 });
			assert.equal(checked.status, 0, checked.stderr || String(checked.error));
		} finally { fs.rmSync(directory, { recursive: true, force: true }); }
	}
});
