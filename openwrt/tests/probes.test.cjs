'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.resolve(__dirname, '..');
const probes = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/probes.uc'));

test('probe transport pins each candidate to its own SOCKS listener and fixed HTTPS targets', () => {
	for (const [index, profile] of probes.profiles.entries()) {
		const args = probes.argumentsFor(profile, 'youtube');
		assert.ok(args.includes('socks5h://127.0.0.1:' + (1089 + index)));
		assert.equal(args.at(-1), 'https://www.youtube.com/generate_204');
		assert.ok(args.includes('--disable'));
		assert.ok(args.includes('--head'));
		assert.equal(args[args.indexOf('--max-time') + 1], '5');
		assert.equal(args[args.indexOf('--noproxy') + 1], '');
		assert.equal(args.includes('--insecure'), false);
		assert.equal(args.includes('--location'), false);
	}
	assert.equal(probes.argumentsFor('direct', 'youtube'), null);
	assert.equal(probes.argumentsFor('vless-reality', 'https://private/'), null);
});

test('HTTPS timing distinguishes response failure from tunnel failure and rejects raw output', () => {
	const parse = (raw, status = 0, target = 'youtube') => probes.parseResult('hysteria2', target, raw, status);
	assert.deepEqual(parse('204 0.123456'), {
		profile: 'hysteria2', status: 'ok', latency_ms: 123, http_status: 204, code: null
	});
	assert.equal(parse('302 0.100000', 0, 'instagram').status, 'ok');
	assert.equal(parse('403 0.100000', 0, 'instagram').status, 'http_error');
	assert.equal(parse('200 0.100000').status, 'http_error');
	assert.equal(parse('000 5.000000', 28).code, 'timeout');
	for (const raw of ['secret', '200 60.000000', '204 0.01\nsecret', '000 0.000000'])
		assert.equal(parse(raw).code, 'invalid_probe_response');
});

function harness({ active = 'hysteria2', selection = 'auto', targets = {}, previous = null, action = 'health',
	candidates = ['vless-reality', 'hysteria2'], target = 'youtube' } = {}) {
	const info = { ok: true, active_profile: active, selection,
		candidates, identity: 'identity-' + active };
	const events = [];
	const files = new Map(previous ? [['/var/run/autovpn-health/state.json', JSON.stringify(previous)]] : []);
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/probe-helper.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '')
		.replace(/for \(let (\w+) in ([^)]+)\)/g, 'for (let $1 of $2)');
	const runner = { popen(argv) {
		events.push(argv);
		const profile = argv.includes('socks5h://127.0.0.1:1089') ? 'vless-reality' : 'hysteria2';
		const raw = argv[0] === '/usr/bin/ucode' ? JSON.stringify(info) :
			targets[profile + ':' + argv.at(-1)] ?? targets[profile] ?? '204 0.123000';
		return { read() { return raw; }, close() { return 0; } };
	} };
	let result;
	new Function('readfile', 'writefile', 'rename', 'chmod', 'mkdir', 'lstat', 'require',
		'type', 'length', 'index', 'json', 'sprintf', 'push', 'time', 'ARGV', 'printf', 'exit', source)(
		p => files.get(p) ?? null,
		(p, raw) => { events.push(['write', p]); files.set(p, raw); return raw.length; },
		(a, b) => { files.set(b, files.get(a)); files.delete(a); return true; },
		() => true, () => true, () => ({ type: 'directory', uid: 0 }),
		name => name === 'autovpn.process' ? runner : probes,
		v => v == null ? null : Array.isArray(v) ? 'array' : Number.isInteger(v) ? 'int' : typeof v,
		v => v.length, (a, b) => a.indexOf(b), JSON.parse, (_fmt, v) => JSON.stringify(v),
		(a, b) => a.push(b), () => 12345, [action, '/etc/autovpn/state/journal.json', target],
		(_fmt, v) => { result = v; }, () => {}
	);
	return { result, events, state: JSON.parse(files.get('/var/run/autovpn-health/state.json') || 'null') };
}

test('healthy current VPN remains selected even when another is faster or earlier in the list', () => {
	const env = harness({ targets: { hysteria2: '204 4.900000', 'vless-reality': '204 0.010000' } });
	assert.equal(env.result.healthy, true);
	assert.equal(env.result.next_profile, null);
	assert.equal(env.events.filter(e => e[0] === '/usr/bin/curl').length, 1);
	assert.equal(env.events.some(e => e.includes('socks5h://127.0.0.1:1089')), false);
});

test('three failed rounds trigger fallback only to a candidate passing two checks', () => {
	let previous = null;
	for (let round = 1; round <= 3; round++) {
		const env = harness({ previous, targets: { hysteria2: '000 0.000000' } });
		previous = env.state;
		assert.equal(env.result.next_profile, round === 3 ? 'vless-reality' : null);
		assert.equal(env.result.failures, round);
	}
	const changed = harness({ previous, active: 'vless-reality' });
	assert.equal(changed.result.next_profile, null);
	assert.equal(changed.state.failures, 0);
});

test('one failed target does not fail the tunnel; manual selection never fails over', () => {
	const google = 'https://www.gstatic.com/generate_204';
	assert.equal(harness({ targets: { ['hysteria2:' + google]: '000 0.000000' } }).result.healthy, true);
	const env = harness({ selection: 'hysteria2', action: 'recover', targets: { hysteria2: '000 0.000000' } });
	assert.equal(env.result.next_profile, null);
	assert.equal(env.result.failed, true);
	assert.equal(env.events.some(e => e.includes('socks5h://127.0.0.1:1089')), false);
});

test('Ping all probes each available candidate without touching health or selection', () => {
	const previous = { identity: 'identity-hysteria2', failures: 2 };
	const env = harness({ action: 'ping-all', previous });
	assert.equal(env.result.active_profile, 'hysteria2');
	assert.equal(env.result.checked_at, 12345);
	assert.deepEqual(env.result.results.map(r => r.status), ['ok', 'ok', 'unavailable']);
	assert.deepEqual(env.state, previous);
	assert.equal(env.events.some(e => e[0] === 'write'), false);
	assert.equal(env.events.filter(e => e[0] === '/usr/bin/ucode').length, 1);
	assert.equal(env.events[0][2], 'probe-info-live');
});

test('hard AWG failure bootstraps the working non-AWG candidate, confirmation never probes old selection', () => {
	const options = { active: 'amneziawg', candidates: ['vless-reality', 'hysteria2', 'amneziawg'],
		targets: { 'vless-reality': '000 0.000000' } };
	const env = harness({ ...options, action: 'bootstrap' });
	assert.equal(env.result.next_profile, 'hysteria2');
	assert.equal(env.events.some(e => e.includes('socks5h://127.0.0.1:1091')), false);
	assert.equal(env.events.some(e => e[0] === 'write'), false);
	const checked = harness({ ...options, action: 'confirm', target: 'hysteria2' });
	assert.equal(checked.result.healthy, true);
	assert.equal(checked.events.filter(e => e[0] === '/usr/bin/curl').length, 2);
	assert.equal(checked.events.some(e => e.includes('socks5h://127.0.0.1:1091')), false);
	assert.equal(harness({ ...options, action: 'bootstrap', selection: 'manual' }).result.code, 'selection_not_auto');
});
