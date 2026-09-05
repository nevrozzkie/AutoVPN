'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.join(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const zapret = loadUcodeModule(path.join(modules, 'zapret.uc'));
const runtime = loadUcodeModule(path.join(modules, 'runtime.uc'));
const machine = loadUcodeModule(path.join(modules, 'state.uc'));
const snapshot = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json')));
const settings = { vless: 'split', hysteria2: 'fake', amneziawg: 'fake', repeats: 2 };
const policy = { selection: 'auto', wan_device: 'eth1', dns_server: '1.1.1.1', direct_domains: ['ru'],
	direct_cidrs: ['0.0.0.0/0'], hysteria_tls_mode: 'subscription', awg_available: false };
function plan() { return zapret.plan(snapshot, ['vless-reality'], 'eth1', settings); }

test('typed zapret plan scopes only marked outer server tuples and never direct traffic', () => {
	const value = plan();
	assert.equal(zapret.validPlan(value), true);
	assert.deepEqual(value.flows, [{ profile: 'vless-reality', ip: snapshot.server.endpoint,
		port: snapshot.protocols.vless.outbound.server_port, transport: 'tcp', mark: 20201, strategy: 'split' }]);
	const rules = zapret.nft(value);
	assert.match(rules, /hook postrouting priority 101/);
	assert.match(rules, /oifname "eth1" meta mark 20201 ip daddr [0-9.]+ tcp dport \d+ ct original packets 1-12 queue num 20195/);
	assert.match(rules, /meta mark 1073762025 ip daddr [0-9.]+ tcp dport \d+ notrack/);
	assert.doesNotMatch(rules, /bypass|br-avpn|prerouting|flush ruleset/);
	const rendered = runtime.render(snapshot, { ...policy, zapret: settings }, machine);
	assert.equal(rendered.ok, true);
	assert.equal(rendered.capabilities.zapret, true);
	assert.equal(rendered.config.outbounds.find(o => o.tag === 'vless-reality').routing_mark, 20201);
	assert.equal(rendered.config.outbounds.find(o => o.tag === 'direct').routing_mark, undefined);
	assert.equal(rendered.config.route.final, 'vless-reality');
	assert.deepEqual(rendered.zapret, value);
});

test('UDP strategies handle obfuscated payloads while preserving AWG inner/outer marks', () => {
	const material = structuredClone(snapshot);
	material.protocols.hysteria2 = { enabled: true, outbound: { server: snapshot.server.endpoint, server_port: 443 } };
	material.protocols.amneziawg = { enabled: true, profile: { peer: { endpoint: { host: snapshot.server.endpoint, port: 51820 } } } };
	const value = zapret.plan(material, ['vless-reality', 'hysteria2', 'amneziawg'], 'eth1', settings);
	assert.equal(zapret.validPlan(value), true);
	assert.deepEqual(value.flows.map(f => f.mark), [20201, 20202, 20194]);
	const config = zapret.config(value);
	assert.match(config, /--bind-fix4/);
	assert.match(config, /--filter-mark=20194\/0xffffffff/);
	assert.equal(config.match(/--lua-desync=fake:payload=all:blob=fake_default_quic:badsum:repeats=2/g).length, 2);
	assert.equal(config.match(/--new/g).length, 2);
	assert.doesNotMatch(config, /--daemon|--writable|--lua-init=[^@]/);
});

test('zapret policy rejects shell/Lua, unsafe WAN/marks, duplicates and excessive work', () => {
	for (const change of [{ vless: 'split;id' }, { repeats: 100 }, { repeats: '2' }, { hysteria2: '--lua-init=evil' }, { injected: true }])
		assert.equal(zapret.validPolicy({ ...settings, ...change }), false);
	for (const change of [{ wan_device: 'br-lan' }, { wan_device: 'eth1";drop' }, { repeats: 7 }, { version: 2 }])
		assert.equal(zapret.validPlan({ ...plan(), ...change }), false);
	for (const change of [{ ip: '1.2.3.999' }, { ip: '1.2.3.4;accept' }, { port: 0 }, { mark: 0 }, { strategy: 'fake' }, { unknown: 'x' }]) {
		const value = plan(); value.flows[0] = { ...value.flows[0], ...change };
		assert.equal(zapret.validPlan(value), false);
		assert.equal(zapret.nft(value), null);
	}
	const duplicate = plan(); duplicate.flows.push(duplicate.flows[0]);
	assert.equal(zapret.validPlan(duplicate), false);
});

test('disabled zapret preserves old bundle validation and local enabled policy is persisted', () => {
	const entry = { snapshot, etag: '"' + 'a'.repeat(64) + '"', attempt: 1 };
	const old = runtime.bundle(entry, policy, machine).value;
	assert.equal(old.zapret, undefined);
	assert.equal(runtime.matchesBundle(old, entry, machine), true);
	const enabled = runtime.bundle(entry, { ...policy, zapret: settings }, machine).value;
	assert.equal(runtime.matchesBundle(enabled, entry, machine), true);
	enabled.zapret.flows[0].mark = 0;
	assert.equal(runtime.matchesBundle(enabled, entry, machine), false);
	assert.equal(zapret.plan(snapshot, ['vless-reality'], 'eth1', { ...settings, vless: 'off' }), null);
});

test('LuCI uses bounded strategy controls and ACL protected installation without arbitrary arguments', () => {
	const ui = fs.readFileSync(path.join(root, 'files/www/luci-static/resources/view/autovpn/settings.js'), 'utf8');
	assert.match(ui, /zapret_enabled/);
	assert.match(ui, /range\(1,6\)/);
	assert.match(ui, /state.phase === 'queued'/);
	const acl = JSON.parse(fs.readFileSync(path.join(root, 'files/usr/share/rpcd/acl.d/luci-app-autovpn.json')));
	assert.ok(acl['luci-app-autovpn'].write.ubus['luci.autovpn'].includes('zapret_install'));
	const rpc = fs.readFileSync(path.join(root, 'files/usr/share/rpcd/ucode/luci.autovpn'), 'utf8');
	assert.match(rpc, /zapret_install: \{ call: function\(\) \{ return callController\('zapret-install'\); \} \}/);
});
