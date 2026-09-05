'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');

function request(action, extra = {}) {
	return { action, ...extra };
}

function helperFixture(overrides = {}) {
	const files = new Map(Object.entries({
		'/etc/autovpn': '', '/etc/autovpn/state': '', '/etc/autovpn/runtime': '', '/etc/autovpn/runtime-zapret': '',
		'/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'confirmed' }),
		...(overrides.files || {})
	}));
	const values = {
		'main.credential_file': '/etc/autovpn/credentials', 'main.state_dir': '/etc/autovpn/state',
		'main.runtime_adapter': '/usr/libexec/autovpn/runtime-adapter', 'main.base_url': 'https://old.example',
		'main.router_id': 'old_router', 'main.enabled': '1', 'main.setup_prepared': '0',
		'runtime.wan_device': 'eth0', 'wifi.base_ssid': 'Dorm', 'wifi.password': 'wifi-password',
		...(overrides.values || {})
	};
	let lastError = 'No such file or directory';
	let commits = 0;
	const events = [];
	const ctx = {
		load: () => true,
		get: (_c, section, option) => values[section + '.' + option],
		set: (_c, section, option, value) => { events.push(`set:${section}.${option}`); values[section + '.' + option] = value; return true; },
		delete: (_c, section, option) => { events.push(`delete:${section}.${option}`); delete values[section + '.' + option]; return true; },
		commit: () => { commits++; return overrides.commit !== false && overrides.failCommitAt !== commits; },
		changes: () => ({})
	};
	const source = read('files/usr/libexec/autovpn/maintenance-helper.uc')
		.replace(/^#![^\n]*\n/, '').replace(/^import .*?;\s*$/gm, '')
		.replace("const processRunner = require('autovpn.process');", 'const processRunner = injectedProcess;')
		.replace(/let nonce = ARGV\[0\];[\s\S]*$/, 'return { perform, saveResult, validNonce };');
	const api = new Function('access', 'chmod', 'fsError', 'lstat', 'mkdir', 'readfile', 'rename', 'stat', 'unlink', 'writefile',
		'cursor', 'injectedProcess', 'type', 'length', 'keys', 'match', 'json', 'sprintf', source)(
		(name, mode) => { assert.equal(mode, 'f'); if (files.has(name)) return true; lastError = 'No such file or directory'; return null; },
		() => true, () => lastError,
		name => { if (overrides.unreadable === name) { lastError = 'Permission denied'; return null; } if (files.has(name)) return { type: 'file' }; lastError = 'No such file or directory'; return null; },
		name => { files.set(name, ''); return true; },
		name => files.get(name) ?? null, (from, to) => { if (overrides.renameFails) return null; files.set(to, files.get(from)); files.delete(from); return true; },
		name => { if (overrides.unreadable === name) { lastError = 'Permission denied'; return null; } if (files.has(name)) return {}; lastError = 'No such file or directory'; return null; },
		name => files.delete(name), (name, value) => { files.set(name, value); return value.length; }, () => ctx,
		{ popen: argv => {
			events.push(['popen', ...argv]);
			return { read: () => JSON.stringify({ ok: overrides.runtimeOk !== false }), close: () => overrides.runtimeOk === false ? 1 : 0 };
		} },
		value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value === 'number' ? 'int' : typeof value,
		value => typeof value === 'string' ? Buffer.byteLength(value) : value.length,
		Object.keys,
		(value, expression) => value.match(expression), JSON.parse,
		(format, value) => format === '%J\n' ? JSON.stringify(value) + '\n' : JSON.stringify(value)
	);
	return { api, files, values, events };
}

test('rotate swaps only the private credential without pausing the active runtime or emitting the token', () => {
	const token = 'avrt_newrouter.' + 'n'.repeat(43);
	const env = helperFixture({ files: { '/etc/autovpn/credentials': 'avrt_oldrouter.' + 'o'.repeat(43) + '\n' } });
	const result = env.api.perform(request('rotate', { credential: token }));
	assert.deepEqual(result, { ok: true, rotated: true, requires_activation: false });
	assert.equal(env.files.get('/etc/autovpn/credentials'), token + '\n');
	assert.equal(env.files.has('/etc/autovpn/state/maintenance.lock'), false);
	assert.deepEqual(env.events.filter(event => Array.isArray(event) && event[0] === 'popen'), [['popen', '/bin/sync']]);
	assert.equal(env.values['main.enabled'], '1');
	assert.equal(JSON.stringify(result).includes(token), false);
});

test('rebind fail-closes then destroys only owned old state, preserves Wi-Fi/WAN, and becomes ready to activate', () => {
	const token = 'avrt_newrouter.' + 'n'.repeat(43);
	const env = helperFixture({ files: {
		'/etc/autovpn/credentials': 'avrt_oldrouter.' + 'o'.repeat(43) + '\n',
		'/etc/autovpn/state/journal.json': '{"old":true}', '/etc/autovpn/runtime/current.json': '{"old":true}',
		'/etc/autovpn/runtime/current.json.new': '{"old_secret":true}',
		'/etc/autovpn/runtime-zapret/current.json': '{"old":true}',
		'/etc/autovpn/runtime-zapret/current.json.new': '{"old_secret":true}',
		'/etc/autovpn/runtime-zapret/awg.conf': 'old private key',
		'/etc/autovpn/runtime-zapret/foreign.txt': 'must stay',
		'/etc/autovpn/runtime/foreign.txt': 'must stay'
	} });
	const result = env.api.perform(request('rebind', {
		confirmation: 'REBIND', base_url: 'https://new.example', router_id: 'new_router', credential: token
	}));
	assert.deepEqual(result, { ok: true, rebound: true, requires_activation: true });
	assert.equal(env.values['main.base_url'], 'https://new.example');
	assert.equal(env.values['main.router_id'], 'new_router');
	assert.equal(env.values['main.enabled'], '0');
	assert.equal(env.values['runtime.wan_device'], 'eth0');
	assert.equal(env.values['wifi.base_ssid'], 'Dorm');
	assert.equal(env.files.has('/etc/autovpn/state/journal.json'), false);
	assert.equal(env.files.has('/etc/autovpn/runtime/current.json'), false);
	assert.equal(env.files.has('/etc/autovpn/runtime-zapret/current.json'), false);
	assert.equal(env.files.has('/etc/autovpn/runtime-zapret/current.json.new'), false);
	assert.equal(env.files.has('/etc/autovpn/runtime-zapret/awg.conf'), false);
	assert.equal(env.files.get('/etc/autovpn/runtime/foreign.txt'), 'must stay');
	assert.equal(env.files.get('/etc/autovpn/runtime-zapret/foreign.txt'), 'must stay');
	assert.deepEqual(JSON.parse(env.files.get('/etc/autovpn/state/maintenance.lock')), { schema_version: 1, action: 'rebind', phase: 'ready' });
	const sync = env.events.findIndex(event => Array.isArray(event) && event[1] === '/bin/sync');
	const close = env.events.findIndex(event => Array.isArray(event) && event[1] === '/usr/libexec/autovpn/runtime-adapter');
	assert.ok(sync >= 0 && close > sync, 'durable maintenance gate must precede runtime/state mutation');
	assert.equal(JSON.stringify(result).includes(token), false);
	assert.equal(env.files.has('/etc/autovpn/runtime/current.json.new'), false);
});

test('reset requires explicit confirmation, clears both lanes, and leaves Wi-Fi/WAN and network transaction data untouched', () => {
	const env = helperFixture({
		files: {
			'/etc/autovpn/credentials': 'avrt_oldrouter.' + 'o'.repeat(43) + '\n',
			'/etc/autovpn/runtime-zapret/run.json': '{"old":true}',
			'/etc/autovpn/runtime-zapret/zapret.json': '{"old":true}',
			'/etc/autovpn/runtime-zapret/foreign.txt': 'must stay'
		},
		values: { 'runtime.selection': 'hysteria2', 'runtime_zapret.selection': 'amneziawg' }
	});
	assert.equal(env.api.perform(request('reset')).code, 'reset_confirmation_required');
	const result = env.api.perform(request('reset', { confirmation: 'RESET' }));
	assert.deepEqual(result, { ok: true, reset: true, requires_activation: false });
	assert.equal(env.files.has('/etc/autovpn/credentials'), false);
	assert.equal(env.files.has('/etc/autovpn/networks/journal.json'), true);
	assert.equal(env.values['main.base_url'], '');
	assert.equal(env.values['main.router_id'], '');
	assert.equal(env.values['main.enabled'], '0');
	assert.equal(env.values['runtime.selection'], 'auto');
	assert.equal(env.values['runtime_zapret.selection'], 'auto');
	assert.equal(env.values['runtime.wan_device'], 'eth0');
	assert.equal(env.values['wifi.password'], 'wifi-password');
	assert.equal(env.files.has('/etc/autovpn/runtime-zapret/run.json'), false);
	assert.equal(env.files.has('/etc/autovpn/runtime-zapret/zapret.json'), false);
	assert.equal(env.files.get('/etc/autovpn/runtime-zapret/foreign.txt'), 'must stay');
	assert.deepEqual(JSON.parse(env.files.get('/etc/autovpn/state/maintenance.lock')), { schema_version: 1, action: 'reset', phase: 'ready' });
});

test('maintenance refuses unsafe paths, incomplete network changes, and opaque filesystem errors before mutation', () => {
	const token = 'avrt_newrouter.' + 'n'.repeat(43);
	assert.equal(helperFixture({ values: { 'main.credential_file': '/etc/autovpn/not-ours' } }).api.perform(request('rotate', { credential: token })).code, 'maintenance_path_invalid');
	assert.equal(helperFixture({ files: { '/etc/autovpn/state/update.lock': JSON.stringify({ schema_version: 1, phase: 'ready' }) } }).api.perform(request('rotate', { credential: token })).code, 'update_in_progress');
	assert.equal(helperFixture({ files: { '/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'pending' }) } }).api.perform(request('reset', { confirmation: 'RESET' })).code, 'networks_not_confirmed');
	assert.equal(helperFixture({ unreadable: '/etc/autovpn/networks/journal.json' }).api.perform(request('rebind', {
		confirmation: 'REBIND', base_url: 'https://new.example', router_id: 'new_router', credential: token
	})).code, 'networks_not_confirmed');
});

test('existing maintenance lock blocks rotate but a confirmed rebind can repair a failed rebind', () => {
	const token = 'avrt_newrouter.' + 'n'.repeat(43);
	const locked = helperFixture({ files: { '/etc/autovpn/state/maintenance.lock': JSON.stringify({ schema_version: 1, action: 'reset', phase: 'ready' }) } });
	assert.equal(locked.api.perform(request('rotate', { credential: token })).code, 'maintenance_pending');
	const retry = helperFixture({ files: { '/etc/autovpn/state/maintenance.lock': JSON.stringify({ schema_version: 1, action: 'rebind', phase: 'failed' }) } });
	assert.equal(retry.api.perform(request('rebind', { confirmation: 'REBIND', base_url: 'https://new.example', router_id: 'new_router', credential: token })).ok, true);
});

test('an explicit reset retries a failed reset after secrets were cleared but before UCI commit', () => {
	const failed = helperFixture({ failCommitAt: 2, files: { '/etc/autovpn/credentials': 'avrt_oldrouter.' + 'o'.repeat(43) + '\n' } });
	assert.equal(failed.api.perform(request('reset', { confirmation: 'RESET' })).code, 'config_write_failed');
	assert.deepEqual(JSON.parse(failed.files.get('/etc/autovpn/state/maintenance.lock')), { schema_version: 1, action: 'reset', phase: 'failed' });
	const retry = helperFixture({ files: Object.fromEntries(failed.files), values: failed.values });
	assert.equal(retry.api.perform(request('reset', { confirmation: 'RESET' })).ok, true);
});

test('CLI streams maintenance JSON only over stdin and never places credentials in argv', t => {
	const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-maintenance-cli-'));
	t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
	const log = path.join(tmp, 'args');
	const program = `#!${process.execPath}\nconst fs=require('node:fs'); fs.appendFileSync(process.env.MOCK_LOG, JSON.stringify(process.argv.slice(2))+'\\n');`;
	for (const name of ['lock', 'uci', 'ucode']) fs.writeFileSync(path.join(tmp, name), program, { mode: 0o755 });
	const ctl = read('files/usr/sbin/autovpnctl')
		.replace('/usr/libexec/autovpn/credential.sh', path.join(root, 'files/usr/libexec/autovpn/credential.sh'))
		.replaceAll('/usr/bin/ucode', path.join(tmp, 'ucode'));
	fs.writeFileSync(path.join(tmp, 'ctl'), ctl, { mode: 0o755 });
	const token = 'avrt_newrouter.' + 'n'.repeat(43);
	const run = spawnSync('/bin/sh', [path.join(tmp, 'ctl'), 'maintenance', 'nonce-0123456789'], {
		input: JSON.stringify(request('rotate', { credential: token })), encoding: 'utf8',
		env: { ...process.env, PATH: tmp + ':' + process.env.PATH, MOCK_LOG: log }
	});
	assert.equal(run.status, 0, run.stderr);
	const argv = fs.readFileSync(log, 'utf8');
	assert.match(argv, /maintenance-helper\.uc/);
	assert.doesNotMatch(argv, new RegExp(token));
});
