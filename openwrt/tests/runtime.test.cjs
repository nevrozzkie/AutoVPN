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
const machine = loadUcodeModule(path.join(moduleRoot, 'state.uc'));
const lanes = loadUcodeModule(path.join(moduleRoot, 'lanes.uc'));
const zapret = loadUcodeModule(path.join(moduleRoot, 'zapret.uc'));
function loadRuntime() {
	const source = fs.readFileSync(path.join(moduleRoot, 'runtime.uc'), 'utf8');
	const factory = new Function('type', 'length', 'keys', 'sort', 'match', 'push', 'substr', 'int',
		'index', 'replace', 'split', 'lc', 'sprintf', 'json', 'join', 'require', source);
	return factory(
		value => value == null ? null : Array.isArray(value) ? 'array' : typeof value === 'boolean' ? 'bool'
			: Number.isInteger(value) ? 'int' : typeof value,
		value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
		Object.keys, value => value.sort(), (value, expression) => value.match(expression),
		(array, value) => array.push(value), (value, start, count) => count == null ? value.substring(start) : value.substring(start, start + count),
		value => Number.parseInt(value, 10), (value, needle) => value.indexOf(needle),
		(value, expression, replacement) => value.replace(expression, replacement),
		(value, separator) => value.split(separator), value => value.toLowerCase(),
		(format, value) => format === '%J' ? JSON.stringify(value) : null, JSON.parse,
		(separator, value) => value.join(separator),
		name => ({ 'autovpn.zapret': zapret, 'autovpn.lanes': lanes })[name],
	);
}
const runtime = loadUcodeModule(path.join(moduleRoot, 'runtime.uc'));
const laneRuntime = loadRuntime();
const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json')));
const clone = value => JSON.parse(JSON.stringify(value));
const policy = () => ({ selection: 'auto', wan_device: 'eth1', dns_server: '1.1.1.1', direct_domains: ['ru', 'xn--p1ai'], direct_cidrs: [], hysteria_tls_mode: 'subscription', awg_available: false });
const entry = (attempt = 1) => ({ etag: '"' + 'a'.repeat(64) + '"', snapshot: clone(snapshot), attempt });

function legacyAutoBundle(original) {
	const value = clone(runtime.bundle(original, policy(), machine).value);
	value.version = 1;
	delete value.candidates;
	value.profile = 'auto';
	value.config.dns.servers = [{ ...value.config.dns.servers[0], detour: 'auto' }];
	value.config.inbounds = value.config.inbounds.filter(item => !item.tag.startsWith('probe-'));
	const directAt = value.config.outbounds.findIndex(item => item.tag === 'direct');
	value.config.outbounds.splice(directAt, 0, {
		type: 'urltest', tag: 'auto', outbounds: ['vless-reality'],
		url: 'https://www.gstatic.com/generate_204', interval: '1m',
		tolerance: 50, interrupt_exist_connections: false,
	});
	value.config.route.rules = value.config.route.rules.filter(rule => !rule.inbound?.[0]?.startsWith('probe-'));
	value.config.route.rules[0].outbound = 'auto';
	value.config.route.final = 'auto';
	return value;
}

function withHysteria(insecure) {
	const result = clone(snapshot);
	result.protocols.hysteria2 = { enabled: true, outbound: {
		type: 'hysteria2', tag: 'hysteria2', server: snapshot.server.endpoint, server_port: 8443,
		password: 'test-secret', tls: { enabled: true, server_name: 'example.com', insecure }
	} };
	return result;
}

test('runtime fixes auto to one profile and forces health and candidate probes before direct exceptions', () => {
	const result = runtime.render(snapshot, { ...policy(), direct_cidrs: ['0.0.0.0/0'] }, machine);
	assert.equal(result.ok, true);
	assert.equal(result.profile, 'vless-reality');
	assert.deepEqual(result.candidates, ['vless-reality']);
	assert.equal(result.config.route.final, 'vless-reality');
	assert.deepEqual(result.config.route.rules.slice(0, 2), [
		{ inbound: ['health'], action: 'route', outbound: 'vless-reality' },
		{ inbound: ['probe-vless-reality'], action: 'route', outbound: 'vless-reality' },
	]);
	assert.equal(result.config.outbounds.some(outbound => outbound.type === 'urltest'), false);
	assert.equal(result.config.dns.servers[0].detour, 'vless-reality');
	assert.equal(result.config.inbounds[0].auto_route, false);
	assert.equal(result.config.inbounds[0].auto_redirect, false);
	assert.equal(result.config.inbounds[1].listen, '127.0.0.1');
	assert.deepEqual(result.config.inbounds[2], { type: 'socks', tag: 'probe-vless-reality', listen: '127.0.0.1', listen_port: 1089 });
	assert.equal(result.config.outbounds.find(outbound => outbound.type === 'direct').bind_interface, 'eth1');
	assert.ok(result.config.route.rules.findIndex(rule => rule.port === 53) > result.config.route.rules.findIndex(rule => rule.inbound?.[0] === 'probe-vless-reality'));
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
		{ type: 'direct', tag: 'amneziawg', bind_interface: 'avpnwg0', routing_mark: 20193, domain_resolver: 'awg-dns' });
	assert.deepEqual(rendered.config.dns.servers.find(item => item.tag === 'awg-dns'),
		{ type: 'udp', tag: 'awg-dns', server: '1.1.1.1', server_port: 53, detour: 'amneziawg' });
	assert.equal(runtime.render(material, { ...policy(), awg_available: false, selection: 'amneziawg' }, machine).code, 'selected_vpn_unavailable');
	const manualVless = runtime.render(material, { ...policy(), awg_available: true, selection: 'vless-reality' }, machine);
	assert.equal(manualVless.ok, true);
	assert.deepEqual(manualVless.awg, material.protocols.amneziawg.profile);
	assert.ok(manualVless.candidates.includes('amneziawg'));
	assert.deepEqual(manualVless.config.inbounds.find(item => item.tag === 'probe-amneziawg'),
		{ type: 'socks', tag: 'probe-amneziawg', listen: '127.0.0.1', listen_port: 1091 });
	material.protocols.amneziawg.profile.obfuscation.Jc = 65536;
	assert.equal(runtime.render(material, { ...policy(), awg_available: true, selection: 'amneziawg' }, machine).code, 'selected_vpn_unavailable');
});

test('runtime helper uses an absolute modprobe path for stable process dispatch', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-helper.uc'), 'utf8');
	assert.match(source, /command\(\['\/sbin\/modprobe', 'amneziawg'\]\)/);
});

test('AWG addresses cannot overlap any managed /24 or either lane TUN subnet', () => {
	for (const address of ['192.168.29.200/32', '192.168.30.250/32', '192.168.31.254/32',
		'192.168.32.100/32', '172.30.255.5/32', '172.30.255.7/32']) {
		const material = withAwg();
		material.protocols.amneziawg.profile.interface.address = address;
		assert.equal(runtime.render(material, { ...policy(), awg_available: true, selection: 'amneziawg' }, machine).code,
			'selected_vpn_unavailable', address);
	}
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
	assert.equal(bundle.version, 2);
	assert.deepEqual(bundle.candidates, ['vless-reality']);
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

test('auto bundle keeps an available preferred profile and falls back deterministically without urltest', () => {
	const material = withHysteria(false);
	const original = entry();
	original.snapshot = material;
	const sticky = runtime.bundle(original, policy(), machine, 'hysteria2').value;
	assert.equal(sticky.profile, 'hysteria2');
	assert.deepEqual(sticky.candidates, ['vless-reality', 'hysteria2']);
	assert.equal(sticky.config.outbounds.some(item => item.type === 'urltest'), false);
	assert.deepEqual(sticky.config.inbounds.filter(item => item.tag.startsWith('probe-')).map(item => [item.tag, item.listen_port]), [
		['probe-vless-reality', 1089], ['probe-hysteria2', 1090],
	]);
	assert.ok(sticky.config.route.rules.findIndex(rule => rule.inbound?.[0] === 'probe-hysteria2') <
		sticky.config.route.rules.findIndex(rule => rule.port === 53));
	assert.equal(runtime.bundle(original, policy(), machine, 'amneziawg').value.profile, 'vless-reality');
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
	let localAwgAvailable = false;
	let failPath = '';
	let output;
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-helper.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'access', 'cursor', 'require',
		'length', 'sprintf', 'type', 'match', 'index', 'json', 'ARGV', 'printf', 'exit', 'int', source);
	return {
		state, storage,
		setPolicy(value) { localPolicy = value; },
		setRouter(value) { localRouter = value; },
		setAwgAvailable(value) { localAwgAvailable = value; },
		failWrite(value) { failPath = value; },
		run(action, selectedProfile) {
			storage.set('/etc/autovpn/state/journal.json', JSON.stringify(state));
			invoke(
				(file, limit) => storage.has(file) ? storage.get(file).slice(0, limit) : null,
			(file, value) => { if (file === failPath) return null; storage.set(file, value); return value.length; },
				() => true,
				(from, to) => { storage.set(to, storage.get(from)); storage.delete(from); return true; },
				file => localAwgAvailable && (file === '/usr/bin/awg' || file === '/sys/module/amneziawg'),
				() => ({ load() {}, get(_config, section, key) { return section === 'main' ? localRouter : localPolicy[key]; } }),
				name => ({ 'autovpn.state': machine, 'autovpn.journal': journal,
					'autovpn.runtime': laneRuntime, 'autovpn.lanes': lanes })[name],
				value => value.length,
				(format, value) => JSON.stringify(value) + (format.endsWith('\n') ? '\n' : ''),
				value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value,
				(value, expression) => value.match(expression), (values, value) => values.indexOf(value), JSON.parse,
				[action, '/etc/autovpn/state/journal.json', selectedProfile], (_format, value) => { output = value; }, () => {}, Number
			);
			return output;
		},
		begin(material = clone(snapshot)) {
			machine.receiveSnapshot(state, material, entry().etag, true);
			machine.beginApply(state);
		},
		activate() { machine.prepared(state); machine.activating(state); return this.run('activate'); },
		commit() {
			if (state.phase === 'ACTIVATING') machine.activated(state);
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

test('zapret settings persist with the VPN bundle, survive auto selection and roll back together', () => {
	const env = helperHarness();
	const local = { ...policy(), zapret_enabled: '1', zapret_repeats: '3' };
	env.setPolicy(local);
	env.begin(withHysteria(false));
	assert.equal(env.run('prepare').ok, true);
	assert.equal(env.activate().ok, true);
	env.commit();
	const previous = env.storage.get('/etc/autovpn/runtime/current.json');
	const plan = JSON.parse(env.storage.get('/etc/autovpn/runtime/zapret.json'));
	assert.equal(plan.repeats, 3);
	assert.deepEqual(plan.flows.map(f => f.profile), ['vless-reality', 'hysteria2']);
	assert.equal(env.run('select-profile', 'hysteria2').ok, true);
	assert.equal(env.run('commit-profile', 'hysteria2').ok, true);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json')).policy.zapret.repeats, 3);
	env.run('select-profile', 'vless-reality'); env.run('commit-profile', 'vless-reality');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), previous);
	env.setPolicy({ ...local, zapret_repeats: '1' });
	env.begin(withHysteria(false)); env.run('prepare'); env.activate();
	assert.equal(env.run('rollback').ok, true);
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), previous);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/zapret.json')).repeats, 3);
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

test('helper probes all candidates without selection side effects and keeps auto preference across prepare', () => {
	const env = helperHarness();
	const material = withHysteria(false);
	env.begin(material);
	assert.equal(env.run('probe-info').code, 'invalid_phase');
	assert.equal(env.run('prepare').ok, true);
	env.activate();
	machine.activated(env.state);
	const runtimeFiles = () => [...env.storage.entries()].filter(([name]) => name.startsWith('/etc/autovpn/runtime/'));
	const beforeInfo = runtimeFiles();
	assert.deepEqual(env.run('probe-info'), {
		ok: true,
		active_profile: 'vless-reality',
		selection: 'auto',
		candidates: ['vless-reality', 'hysteria2'],
		identity: entry().etag + ':' + env.state.desired.attempt + ':vless-reality',
		lane: 'vpn',
		legacy_transport: true,
	});
	assert.deepEqual(runtimeFiles(), beforeInfo);
	const previous = env.storage.get('/etc/autovpn/runtime/previous.json');
	const current = env.storage.get('/etc/autovpn/runtime/current.json');
	assert.equal(env.run('select-profile', 'hysteria2').active_profile, 'hysteria2');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/failover.json')).profile, 'hysteria2');
	assert.equal(env.storage.get('/etc/autovpn/runtime/previous.json'), previous);
	assert.equal(env.run('commit-profile', 'vless-reality').code, 'failover_bundle_mismatch');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(env.run('commit-profile', 'hysteria2').active_profile, 'hysteria2');
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json')).profile, 'hysteria2');
	assert.equal(env.storage.get('/etc/autovpn/runtime/previous.json'), previous);
	env.commit();
	env.begin(material);
	assert.equal(env.run('prepare').ok, true);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/prepared.json')).profile, 'hysteria2');
});

test('helper never changes a manual selection while exposing read-only probe candidates', () => {
	const env = helperHarness();
	env.setPolicy({ ...policy(), selection: 'vless-reality' });
	env.begin(withHysteria(false));
	env.run('prepare'); env.activate(); env.commit();
	assert.equal(env.run('probe-info').selection, 'manual');
	const before = [...env.storage.entries()];
	assert.equal(env.run('select-profile', 'hysteria2').code, 'selection_not_auto');
	assert.equal(env.run('commit-profile', 'hysteria2').code, 'selection_not_auto');
	assert.deepEqual([...env.storage.entries()], before);
});

test('live diagnostics refuse a runtime that differs from the committed bundle without repairing it', () => {
	const env = helperHarness();
	env.begin(withHysteria(false)); env.run('prepare'); env.activate(); env.commit();
	const config = JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json')).config;
	env.storage.set('/etc/autovpn/runtime/run.json', JSON.stringify(config));
	assert.equal(env.run('probe-info-live').ok, true);
	env.run('select-profile', 'hysteria2');
	env.storage.set('/etc/autovpn/runtime/run.json', env.storage.get('/etc/autovpn/runtime/candidate.json'));
	const before = [...env.storage.entries()];
	assert.equal(env.run('probe-info-live').code, 'runtime_bundle_mismatch');
	assert.deepEqual([...env.storage.entries()], before);
	// Confirmation can inspect the allowlisted staged candidate before commit.
	assert.equal(env.run('probe-info').ok, true);
});

test('restore ignores an uncommitted staged failover and regenerates the committed candidate', () => {
	const env = helperHarness();
	env.begin(withHysteria(false));
	env.run('prepare'); env.activate(); env.commit();
	const current = env.storage.get('/etc/autovpn/runtime/current.json');
	const previous = env.storage.get('/etc/autovpn/runtime/previous.json');
	assert.equal(env.run('select-profile', 'hysteria2').ok, true);
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(env.run('restore').active_profile, 'vless-reality');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/candidate.json')).route.final, 'vless-reality');
	assert.equal(env.storage.get('/etc/autovpn/runtime/previous.json'), previous);
	assert.equal(env.run('commit-profile', 'hysteria2').code, 'failover_bundle_mismatch');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
});

test('helper drops optional AWG from the exact staged bundle and commits only the verified fallback', () => {
	const env = helperHarness();
	const material = withAwg();
	material.protocols.hysteria2 = withHysteria(false).protocols.hysteria2;
	env.setAwgAvailable(true);
	env.begin(material);
	env.run('prepare'); env.activate(); env.commit();
	const current = env.storage.get('/etc/autovpn/runtime/current.json');
	const previous = env.storage.get('/etc/autovpn/runtime/previous.json');
	assert.equal(env.run('select-profile', 'hysteria2').ok, true);
	assert.equal(env.run('disable-awg').active_profile, 'hysteria2');
	const staged = JSON.parse(env.storage.get('/etc/autovpn/runtime/failover.json'));
	assert.equal(staged.policy.awg_available, false);
	assert.deepEqual(staged.candidates, ['vless-reality', 'hysteria2']);
	assert.equal(env.storage.get('/etc/autovpn/runtime/awg.json'), 'null\n');
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(env.run('commit-profile', 'hysteria2').active_profile, 'hysteria2');
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json')).policy.awg_available, false);
	assert.equal(env.storage.get('/etc/autovpn/runtime/previous.json'), previous);
});

test('helper disables AWG in the active candidate only when AWG is not the selected profile', () => {
	const optional = helperHarness();
	optional.setAwgAvailable(true);
	optional.begin(withAwg());
	optional.run('prepare'); optional.activate();
	assert.equal(optional.run('disable-awg').active_profile, 'vless-reality');
	const current = JSON.parse(optional.storage.get('/etc/autovpn/runtime/current.json'));
	assert.equal(current.policy.awg_available, false);
	assert.deepEqual(current.candidates, ['vless-reality']);

	const required = helperHarness();
	required.setAwgAvailable(true);
	required.setPolicy({ ...policy(), selection: 'amneziawg' });
	required.begin(withAwg());
	required.run('prepare'); required.activate();
	const before = [...required.storage.entries()];
	assert.equal(required.run('disable-awg').code, 'active_awg_required');
	assert.deepEqual([...required.storage.entries()], before);
});

test('dead auto-AWG fallback can stage and commit a checked non-AWG profile during rollback', () => {
	const env = helperHarness();
	env.setAwgAvailable(true);
	const material = withAwg();
	material.protocols.hysteria2 = withHysteria(false).protocols.hysteria2;
	env.begin(material); env.run('prepare'); env.activate(); env.commit();
	env.run('select-profile', 'amneziawg'); env.run('commit-profile', 'amneziawg');
	env.begin(material); env.run('prepare'); env.activate();
	machine.applyFailed(env.state, 'test_failed_apply');
	assert.equal(env.run('rollback').ok, true);
	const current = env.storage.get('/etc/autovpn/runtime/current.json');
	assert.equal(env.run('fallback-awg').ok, true);
	assert.equal(env.storage.get('/etc/autovpn/runtime/current.json'), current);
	assert.equal(env.run('fallback-awg', 'hysteria2').active_profile, 'hysteria2');
	assert.equal(env.run('probe-info').selection, 'auto');
	assert.equal(env.run('commit-profile', 'hysteria2').active_profile, 'hysteria2');
	const committed = JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json'));
	assert.equal(committed.policy.awg_available, false);
	assert.equal(runtime.matchesBundle(committed, env.state.applied, machine), true);
});

test('version 1 bundles validate with the legacy renderer and restore upgrades them in place', () => {
	const original = entry();
	const legacy = legacyAutoBundle(original);
	assert.equal(runtime.matchesBundle(legacy, original, machine), true);
	const env = helperHarness();
	env.begin(); env.run('prepare'); env.activate(); env.commit();
	assert.deepEqual(env.state.applied, original);
	env.storage.set('/etc/autovpn/runtime/current.json', JSON.stringify(legacy));
	assert.equal(env.run('probe-info').code, 'runtime_upgrade_required');
	assert.equal(env.run('restore').ok, true);
	const restored = JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json'));
	assert.equal(restored.version, 2);
	assert.equal(restored.profile, 'vless-reality');
	assert.equal(runtime.matchesBundle(restored, original, machine), true);
	assert.equal(env.run('probe-info').active_profile, 'vless-reality');

	env.begin();
	env.run('prepare');
	env.storage.set('/etc/autovpn/runtime/prepared.json', JSON.stringify(legacyAutoBundle(env.state.desired)));
	assert.equal(env.activate().ok, true);
	assert.equal(JSON.parse(env.storage.get('/etc/autovpn/runtime/prepared.json')).version, 2);
	env.storage.set('/etc/autovpn/runtime/previous.json', JSON.stringify(legacyAutoBundle(env.state.applied)));
	assert.equal(env.run('rollback').ok, true);
	const rolledBack = JSON.parse(env.storage.get('/etc/autovpn/runtime/current.json'));
	assert.equal(rolledBack.version, 2);
	assert.equal(runtime.matchesBundle(rolledBack, env.state.applied, machine), true);
});

test('controller restores VPN before fetching and never hides restore failure behind HTTP 304', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/controller.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '').split('\nlet config = configuration();')[0];
	const factory = new Function('require', 'popen', 'access', 'chmod', 'type', 'length', 'match', 'json', 'push', source + '\nreturn refresh;');
	for (const restored of [{ ok: true }, { ok: false, code: 'restore_test' }, { ok: true, restored: false }]) {
		const restoreOk = restored.ok && restored.restored !== false;
		const calls = [];
		const refresh = factory(
			name => name === 'autovpn.process' ? { popen: argv => {
				calls.push(argv[1]);
				const response = argv[1] === 'restore' ? restored :
					argv[1] === 'fetch' ? { ok: true, status: 304 } : { ok: true };
				return { read() { return JSON.stringify(response); }, close() { return 0; } };
			} } : loadUcodeModule(path.join(moduleRoot, name.split('.').pop() + '.uc')),
			argv => {
				calls.push(argv[1]);
				const response = argv[1] === 'restore' ? restored :
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
		assert.equal(calls.includes('fail-closed'), !restored.ok);
		if (!restoreOk) assert.equal(result.code, restored.restored === false ? 'runtime_partially_restored' : 'restore_test');
	}
});

function fixture(t, scenario = '') {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-runtime-test-'));
	t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
	const work = path.join(directory, 'runtime');
	const bin = path.join(directory, 'bin');
	fs.mkdirSync(work);
	fs.mkdirSync(bin);
	for (const name of ['ip', 'nft', 'uci', 'ucode', 'curl', 'sing-box', 'service', 'jsonfilter'])
		fs.symlinkSync(path.join(__dirname, 'fake-runtime.cjs'), path.join(bin, name));
	let source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-lane'), 'utf8');
	source = source.replace('\t\tROOT=/etc/autovpn/runtime\n', '\t\tROOT=' + work + '\n')
		.replace('GATE_ROOT=/etc/autovpn/state', 'GATE_ROOT=' + work)
		.replace('\t\tSERVICE=/etc/init.d/autovpn-tunnel\n', '\t\tSERVICE=' + path.join(bin, 'service') + '\n')
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

test('health keeps a healthy or transiently failing current VPN without selection/restart', t => {
	for (const scenario of ['health-sticky', 'health-transient']) {
		const env = fixture(t, scenario);
		fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
		fs.writeFileSync(path.join(env.work, 'run.json'), '{"test":"generated"}');
		const result = env.run('health-tick');
		assert.equal(result.status, 0, result.stdout + result.stderr);
		const events = env.events().map(e => e.join(' '));
		assert.equal(events.some(e => /service (start|stop)/.test(e)), false);
		assert.equal(events.some(e => e.includes('select-profile')), false);
		assert.ok(events.some(e => e.includes('route replace default dev avpn0 table 20191')));
		if (scenario === 'health-sticky')
			assert.ok(events.findIndex(e => e.includes('route replace default dev avpn0')) < events.findIndex(e => e.includes('guard-vpn-open.nft')));
	}
});

test('failover stages and verifies a candidate before durable commit, failures keep old selection', t => {
	for (const scenario of ['health-switch', 'health-switch-failed', 'health-dead']) {
		const env = fixture(t, scenario);
		fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
		fs.writeFileSync(path.join(env.work, 'run.json'), '{"test":"generated"}');
		const result = env.run('health-tick');
		const events = env.events().map(e => e.join(' '));
		if (scenario === 'health-switch') {
			assert.equal(result.status, 0, result.stdout + result.stderr);
			const at = text => events.findIndex(e => e.includes(text));
			assert.ok(at('select-profile') < at('service start'));
			assert.ok(at('service start') < at('probe-helper.uc confirm'));
			assert.ok(at('probe-helper.uc confirm') < at('commit-profile'));
			assert.ok(at('commit-profile') < at('guard-vpn-open.nft'));
		} else {
			assert.equal(fs.existsSync(path.join(env.directory, 'committed-profile')), false);
			assert.equal(events.some(e => e.includes('guard-vpn-open.nft')), false);
		}
	}
});

test('failed route repair stays closed even with healthy local probes', t => {
	const env = fixture(t, 'route-failed');
	fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
	fs.writeFileSync(path.join(env.work, 'run.json'), '{"test":"generated"}');
	assert.equal(env.run('health-tick').status, 1);
	assert.equal(env.events().some(e => e.join(' ').includes('guard-vpn-open.nft')), false);
});

test('health preflight failure closes an already open lane or reports unconfirmed closure', t => {
	for (const scenario of ['offload', 'offload-foreign-table']) {
		const env = fixture(t, scenario);
		fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
		const result = env.run('health-tick');
		assert.equal(result.status, 1);
		assert.equal(JSON.parse(result.stdout).code,
			scenario === 'offload' ? 'runtime_preflight_failed' : 'fail_closed_unconfirmed');
		const events = env.events().map(event => event.join(' '));
		assert.equal(events.some(event => event === 'service stop'), true);
		assert.equal(events.some(event => event.includes('guard-vpn-open.nft')), false);
		assert.equal(fs.existsSync(path.join(env.directory, 'service-running')), false);
	}
});

test('Ping all never invokes restore, start, profile changes or guard opening', t => {
	const env = fixture(t);
	fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
	assert.equal(env.run('ping-all').status, 0);
	const events = env.events().map(e => e.join(' '));
	assert.equal(events.some(e => /service (start|stop)|guard-open|select-profile|commit-profile/.test(e)), false);
	assert.equal(events.filter(e => e.includes('runtime-helper.uc')).every(e => e.includes(' status ')), true);
	assert.ok(events.some(e => e.includes('probe-helper.uc ping-all')));
});

test('Ping all remains read-only when maintenance blocks diagnostics', t => {
	const env = fixture(t);
	fs.writeFileSync(path.join(env.directory, 'events'), '');
	fs.symlinkSync('missing-target', path.join(env.work, 'maintenance.lock'));
	const result = env.run('ping-all');
	assert.equal(result.status, 1);
	assert.equal(JSON.parse(result.stdout).code, 'maintenance_locked');
	assert.equal(fs.readFileSync(path.join(env.directory, 'events'), 'utf8'), '');
});

test('shell runtime checks candidate, closes guard before restart, opens only after two VPN probes', t => {
	const env = fixture(t);
	for (const action of ['prepare', 'activate', 'verify']) {
		const result = env.run(action);
		assert.equal(result.status, 0, result.stdout + result.stderr);
	}
	const events = env.events();
	const at = text => events.findIndex(event => event.join(' ').includes(text));
	assert.ok(at('sing-box check') < at('guard-vpn-closed.nft'));
	assert.ok(at('guard-vpn-closed.nft') < at('service stop'));
	assert.ok(at('zapret-helper.uc up') < at('service start'));
	assert.ok(at('service start') < at('curl --disable'));
	assert.ok(at('curl --disable') < at('guard-vpn-open.nft'));
	assert.equal(events.filter(event => event[0] === 'curl').length, 2);
	assert.ok(events.some(event => event.join(' ').includes('priority 20192 iif br-avpn unreachable')));
});

test('zapret failure prevents activation and cannot open or select a different VPN', t => {
	const env = fixture(t, 'zapret-failed');
	assert.equal(env.run('activate').status, 1);
	const events = env.events().map(e => e.join(' '));
	assert.equal(events.some(e => /service start|guard-open|select-profile/.test(e)), false);
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
		assert.equal(env.events().some(event => event.join(' ').includes('guard-vpn-open.nft')), false);
		assert.equal(env.events().some(event => event.join(' ').includes('service start')), false);
	}
});

test('runtime refuses to activate while SSID transaction needs confirmation', t => {
	const env = fixture(t, 'networks-pending');
	assert.equal(env.run('activate').status, 1);
	assert.equal(env.events().some(event => event.join(' ').includes('service start')), false);
	assert.equal(env.events().some(event => event.join(' ').includes('guard-vpn-open.nft')), false);
});

test('failed HTTPS probe stays closed; fail-closed validates ownership before stopping legacy zapret', t => {
	const env = fixture(t, 'probe-failed');
	assert.equal(env.run('activate').status, 0);
	assert.equal(env.run('verify').status, 1);
	assert.equal(env.events().some(event => event.join(' ').includes('guard-vpn-open.nft')), false);
	const before = env.events().length;
	assert.equal(env.run('fail-closed').status, 0);
	assert.equal(env.events().slice(before).filter(event => event[1]?.endsWith('/runtime-helper.uc'))
		.every(event => event[2] === 'status'), true);
	assert.ok(env.events().slice(before).some(event => event[1]?.endsWith('/zapret-helper.uc') && event[2] === 'down'));
});

test('foreign nft ownership and route priority collisions are refused without flushing', t => {
	for (const scenario of ['foreign-table', 'foreign-route']) {
		const env = fixture(t, scenario);
		assert.equal(env.run('activate').status, 1);
		const events = env.events();
		assert.equal(events.some(event => event.join(' ').includes('guard-vpn-open.nft')), false);
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

test('restore does not restart a working non-AWG profile for unused AWG health', t => {
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
	assert.equal(events.some(event => event.includes('awg-helper.uc check')), false);
});

test('optional AWG startup failure removes that candidate without blocking VLESS', t => {
	const env = fixture(t, 'optional-awg-failed');
	fs.writeFileSync(path.join(env.work, 'awg.json'), '{"interface":{"private_key":"test"}}');
	assert.equal(env.run('activate').status, 0);
	assert.equal(fs.readFileSync(path.join(env.work, 'awg.json'), 'utf8'), 'null');
	assert.ok(env.events().some(e => e.join(' ').includes('disable-awg')));
	assert.ok(env.events().some(e => e.join(' ') === 'service start'));
});

test('an unstartable selected AWG bootstraps and verifies another VPN instead of retrying AWG forever', t => {
	const env = fixture(t, 'hard-awg-failed');
	fs.writeFileSync(path.join(env.work, 'awg.json'), '{"interface":{"private_key":"test"}}');
	fs.writeFileSync(path.join(env.work, 'run.json'), '{"test":"generated"}');
	fs.writeFileSync(path.join(env.directory, 'service-running'), '1');
	const result = env.run('health-tick');
	assert.equal(result.status, 0, result.stdout + result.stderr);
	assert.equal(fs.readFileSync(path.join(env.directory, 'committed-profile'), 'utf8'), 'hysteria2');
	const events = env.events().map(e => e.join(' '));
	assert.ok(events.findIndex(e => e.includes('bootstrap')) < events.findIndex(e => e.includes('commit-profile')));
	assert.ok(events.findIndex(e => e.includes('confirm')) < events.findIndex(e => e.includes('commit-profile')));
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
		for (const lane of [null, 'vpn', 'vpn_zapret']) {
			const snapshot = clone(material);
			if (lane != null) {
				snapshot.schema_version = 4;
				snapshot.protocols.amneziawg_aux = { enabled: true, profile: clone(snapshot.protocols.amneziawg.profile) };
				snapshot.protocols.amneziawg_aux.profile.interface.private_key = Buffer.alloc(32, 5).toString('base64');
				snapshot.protocols.amneziawg_aux.profile.interface.address = '10.66.66.9/32';
				delete snapshot.protocols.amneziawg_aux.profile.legacy_amnezia_vpn_import_key;
			}
			const rendered = runtime.render(snapshot, { ...policy(), awg_available: process.platform === 'linux', selection }, machine, null, lane);
			assert.equal(rendered.ok, true, rendered.code);
			const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-singbox-check-'));
			try {
				const file = path.join(directory, 'config.json');
				fs.writeFileSync(file, JSON.stringify(rendered.config), { mode: 0o600 });
				const checked = spawnSync(process.env.AUTOVPN_SING_BOX, ['check', '-c', file], { encoding: 'utf8', timeout: 15000 });
				assert.equal(checked.status, 0, checked.stderr || String(checked.error));
			} finally { fs.rmSync(directory, { recursive: true, force: true }); }
		}
	}
});
