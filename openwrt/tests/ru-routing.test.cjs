'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.resolve(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const runtime = loadUcodeModule(path.join(modules, 'runtime.uc'));
const machine = loadUcodeModule(path.join(modules, 'state.uc'));
const source = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json')));
function policy() {
	return { selection: 'auto', wan_device: 'eth1', dns_server: '1.1.1.1',
		direct_domains: ['example.org'], direct_cidrs: ['203.0.113.0/24'],
		hysteria_tls_mode: 'subscription', awg_available: false, ru_bypass: true };
}
function entry(dual = false) {
	const snapshot = structuredClone(source);
	snapshot.protocols.vless.outbound.tls.reality.public_key = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
	if (dual) {
		snapshot.schema_version = 4;
		snapshot.protocols.amneziawg_aux = { enabled: false, profile: null };
	}
	return { snapshot, etag: '"' + 'a'.repeat(64) + '"', attempt: 1 };
}

test('RU database is shared by both VPN lanes after DNS, private/IPv6 guards and forced probes', () => {
	for (const lane of ['vpn', 'vpn_zapret']) {
		const result = runtime.render(entry(true).snapshot, policy(), machine, null, lane);
		assert.equal(result.ok, true, result.code);
		assert.deepEqual(result.config.route.rule_set, [{ type: 'local', tag: 'autovpn-ru',
			format: 'source', path: '/etc/autovpn/routing/ru.json' }]);
		const rules = result.config.route.rules;
		const at = rules.findIndex(rule => rule.rule_set);
		assert.deepEqual(rules[at], { rule_set: ['autovpn-ru'], action: 'route', outbound: 'direct' });
		for (const predicate of [rule => rule.inbound?.[0] === 'health',
			rule => rule.inbound?.[0]?.startsWith('probe-'), rule => rule.port === 53,
			rule => rule.ip_version === 6, rule => rule.ip_is_private]) {
			assert.ok(rules.findIndex(predicate) >= 0 && rules.findIndex(predicate) < at);
		}
		assert.deepEqual(rules.find(rule => rule.domain_suffix).domain_suffix, ['example.org']);
		assert.deepEqual(rules.find(rule => rule.ip_cidr).ip_cidr, ['203.0.113.0/24']);
		assert.equal(result.config.route.final, 'vless-reality');
		assert.equal(result.config.dns.servers[0].detour, 'vless-reality');
		assert.equal(result.config.experimental, undefined);
	}
});

test('database toggle preserves old bundles and rejects non-boolean input', () => {
	const old = policy();
	delete old.ru_bypass;
	const legacy = runtime.bundle(entry(), old, machine);
	assert.equal(legacy.ok, true);
	assert.equal(legacy.value.config.route.rule_set, undefined);
	assert.equal(runtime.matchesBundle(legacy.value, entry(), machine), true);
	const disabled = runtime.render(entry().snapshot, { ...policy(), ru_bypass: false }, machine);
	assert.deepEqual(disabled.config, legacy.value.config);
	for (const value of [null, 1, 0, '1', [], {}]) {
		assert.equal(runtime.validatePolicy({ ...policy(), ru_bypass: value }).code, 'invalid_ru_bypass');
	}
	const enabled = runtime.bundle(entry(true), policy(), machine, null, 'vpn_zapret');
	assert.equal(enabled.ok, true);
	assert.equal(runtime.matchesBundle(enabled.value, entry(true), machine, 'vpn_zapret'), true);
	enabled.value.config.route.rule_set[0].path = '/tmp/foreign-rules.json';
	assert.equal(runtime.matchesBundle(enabled.value, entry(true), machine, 'vpn_zapret'), false);
});

test('packaged offline seed and lifecycle never depend on remote startup', () => {
	const seed = JSON.parse(fs.readFileSync(path.join(root, 'files/etc/autovpn/routing/ru.json')));
	assert.deepEqual(seed, { version: 1, rules: [{ domain_suffix: ['ru', 'xn--p1ai', 'su'] }] });
	const init = fs.readFileSync(path.join(root, 'files/etc/init.d/autovpn'), 'utf8');
	assert.match(init, /procd_open_instance routing[\s\S]*?command \/usr\/libexec\/autovpn\/ru-db-loop/);
	assert.doesNotMatch(fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/loop'), 'utf8'), /ru-db-update/);
	const makefile = fs.readFileSync(path.join(root, 'Makefile'), 'utf8');
	assert.match(makefile, /DEPENDS:=.*\+flock(?:\s|$)/);
	assert.match(makefile, /DEPENDS:=.*\+coreutils-stat(?:\s|$)/);
	assert.match(makefile, /DEPENDS:=.*\+coreutils-timeout(?:\s|$)/);
	assert.match(makefile, /conffiles[\s\S]*?\/etc\/autovpn\/routing\/ru.json[\s\S]*?endef/);
	const maintenance = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/maintenance-helper.uc'), 'utf8');
	assert.match(maintenance, /ctx.set\('autovpn', 'runtime', 'ru_bypass', '1'\)/);
	assert.doesNotMatch(maintenance, /removeOwned\([^\n]*routing/);
});

test('actual sing-box validates local RU database routing without downloads', { skip: !process.env.AUTOVPN_SING_BOX }, () => {
	const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-ru-config-'));
	try {
		for (const lane of ['vpn', 'vpn_zapret']) {
			const result = runtime.render(entry(true).snapshot, policy(), machine, null, lane);
			assert.equal(result.ok, true);
			assert.equal(result.config.route.rule_set[0].path, '/etc/autovpn/routing/ru.json');
			// Only translate the target filesystem path for this host-only syntax check.
			result.config.route.rule_set[0].path = path.join(root, 'files/etc/autovpn/routing/ru.json');
			const config = path.join(temp, lane + '.json');
			fs.writeFileSync(config, JSON.stringify(result.config));
			const checked = spawnSync(process.env.AUTOVPN_SING_BOX, ['check', '-c', config], { encoding: 'utf8', timeout: 15000 });
			assert.equal(checked.status, 0, checked.stderr || String(checked.error));
		}
	} finally { fs.rmSync(temp, { recursive: true, force: true }); }
});
