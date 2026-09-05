'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const os = require('node:os');
const { spawnSync } = require('node:child_process');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.join(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const planner = loadUcodeModule(path.join(modules, 'networks.uc'));
const transaction = loadUcodeModule(path.join(modules, 'network-transaction.uc'));
const journalPath = '/etc/autovpn/networks/journal.json';
function state(phase = 'confirmed') {
	const before = {}, after = {};
	for (const name of planner.configs) { before[name] = name + '-before'; after[name] = name + '-after'; }
	return { version: 1, sequence: 1, id: '100-1', deadline: 300, expires_uptime: 300,
		phase, ready: true, before, after, ssids: [], radios: [] };
}
function fixture(options = {}) {
	const files = new Map(Object.entries(options.files || {}));
	const values = { 'main.enabled': '0', ...options.values };
	const sets = [], commands = [];
	let error = 'No such file or directory';
	const ctx = { load: () => true, changes: () => options.dirty ? { test: ['pending'] } : {},
		get: (_cfg, section, key) => values[section + '.' + key],
		set: (cfg, section, key, value) => { sets.push([cfg, section, key, value]); values[section + '.' + key] = value; return true; },
		commit: () => !options.commitFailed };
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/wifi-bootstrap.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import .*;\s*$/gm, '').replace(/let result;\ntry \{ result = run\(\);[\s\S]*$/, 'return run();');
	const invoke = new Function('readfile', 'lstat', 'fsError', 'cursor', 'require', 'type', 'length', 'keys', 'json', 'split', 'ARGV', source);
	return { files, values, sets, commands, run(action = 'configure', id) {
		return invoke(name => files.get(name) ?? null, name => {
			if (options.unreadable === name) { error = 'Permission denied'; return null; }
			if (files.has(name)) return { type: 'file' };
			error = 'No such file or directory'; return null;
		}, () => error, () => ctx, name => ({ 'autovpn.networks': planner, 'autovpn.network-transaction': transaction,
			'autovpn.process': { popen: argv => { commands.push(argv); return { read: () => JSON.stringify({ ok: true, phase: 'pending', transaction_id: '100-1', ready: true }), close: () => 0 }; } } })[name],
		value => value == null ? null : Array.isArray(value) ? 'array' : typeof value === 'number' ? 'int' : typeof value === 'boolean' ? 'bool' : typeof value,
		value => typeof value === 'string' ? Buffer.byteLength(value) : value.length, Object.keys, JSON.parse,
		(value, separator) => value.split(separator), [action, id]);
	} };
}

test('installer bootstrap reads two private lines and writes only AutoVPN settings', () => {
	const password = "wifi-'$\\;private";
	const env = fixture({ files: { '/dev/stdin': 'Общага\n' + password + '\n' } });
	const result = env.run();
	assert.equal(result.ok, true);
	assert.equal(result.phase, 'pending');
	assert.equal(env.values['wifi.password'], password);
	assert.equal(env.values['wifi.primary_lan'], '1');
	assert.equal(env.values['wifi.bootstrap_completed'], '0');
	assert.ok(env.sets.every(([cfg]) => cfg === 'autovpn'));
	assert.deepEqual(env.commands, [['/usr/bin/ucode', '/usr/libexec/autovpn/network-helper.uc', 'network-bootstrap']]);
	assert.equal(JSON.stringify(result).includes(password), false);
});

test('invalid input and active/pending installations fail before persisting Wi-Fi', () => {
	for (const raw of ['X\nshort\n', 'X\ngood-password\nextra\n', 'X\ngood-password', 'a'.repeat(28) + '\ngood-password\n', 'X\n' + 'p'.repeat(260) + '\n']) {
		const env = fixture({ files: { '/dev/stdin': raw } });
		assert.equal(env.run().ok, false); assert.equal(env.sets.length, 0);
	}
	for (const options of [{ values: { 'main.enabled': '1' } }, { values: { 'main.setup_prepared': '1' } },
		{ values: { 'main.base_url': 'https://existing.example' } }, { dirty: true },
		{ files: { '/etc/autovpn/credentials': 'existing' } }, { unreadable: '/etc/autovpn/state/journal.json' },
		{ files: { [journalPath]: JSON.stringify(state('pending')) } }]) {
		const env = fixture({ ...options, files: { '/dev/stdin': 'X\ngood-password\n', ...options.files } });
		assert.equal(env.run().ok, false); assert.equal(env.sets.length, 0);
	}
});

test('completion marker requires exact confirmed transaction and unchanged network files', () => {
	const saved = state();
	const files = { [journalPath]: JSON.stringify(saved) };
	for (const name of planner.configs) files['/etc/config/' + name] = saved.after[name];
	const env = fixture({ files, values: { 'wifi.primary_lan': '1' } });
	assert.equal(env.run('confirm', 'wrong').ok, false);
	assert.equal(env.sets.length, 0);
	assert.equal(env.run('confirm', saved.id).ok, true);
	assert.equal(env.values['wifi.bootstrap_completed'], '1');
	const changed = fixture({ files: { ...files, '/etc/config/network': 'user-edited-pppoe' }, values: { 'wifi.primary_lan': '1' } });
	assert.equal(changed.run('confirm', saved.id).ok, false);
	assert.equal(changed.sets.length, 0);
});

test('installer retry recovers only a durable confirmed bootstrap without reading a new key', () => {
	const saved = state();
	const files = { [journalPath]: JSON.stringify(saved) };
	for (const name of planner.configs) files['/etc/config/' + name] = saved.after[name];
	const env = fixture({ files, values: { 'wifi.primary_lan': '1', 'wifi.password': 'keep-old-key' } });
	assert.deepEqual(env.run('resume'), { ok: true, phase: 'confirmed', transaction_id: saved.id });
	assert.deepEqual(env.sets, [['autovpn', 'wifi', 'bootstrap_completed', '1']]);
	assert.equal(env.values['wifi.password'], 'keep-old-key');
	assert.equal(env.commands.length, 0);
	for (const options of [{ files: { ...files, '/etc/config/network': 'later-user-edit' } },
		{ files: { [journalPath]: JSON.stringify(state('pending')) } }, { files, values: { 'wifi.primary_lan': '0' } }]) {
		const blocked = fixture({ ...options, values: { 'wifi.primary_lan': '1', ...options.values } });
		assert.equal(blocked.run('resume').ok, false);
		assert.equal(blocked.sets.length, 0);
	}
	const fresh = fixture();
	assert.deepEqual(fresh.run('resume'), { ok: true, phase: 'not_configured' });
	assert.equal(fresh.sets.length, 0);
});

test('an exactly rolled-back bootstrap is retryable without overriding later router edits', () => {
	const saved = state('rolled_back');
	const files = { [journalPath]: JSON.stringify(saved), '/dev/stdin': 'Retry\ngood-password\n' };
	for (const name of planner.configs) files['/etc/config/' + name] = saved.before[name];
	assert.equal(fixture({ files }).run().ok, true);
	const changed = fixture({ files: { ...files, '/etc/config/network': 'new-pppoe-settings' } });
	assert.equal(changed.run().ok, false);
	assert.equal(changed.sets.length, 0);
});

test('real CLI finalizes bootstrap after network confirmation and preserves legacy results', t => {
	const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-bootstrap-cli-'));
	t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
	const log = path.join(tmp, 'calls');
	const mock = `#!${process.execPath}
const fs = require('node:fs'), path = require('node:path');
const name = path.basename(process.argv[1]), args = process.argv.slice(2);
fs.appendFileSync(process.env.MOCK_LOG, JSON.stringify([name, ...args]) + '\\n');
if (name === 'uci') {
  const key = args.at(-1);
  if (key === 'autovpn.wifi.primary_lan') process.stdout.write(process.env.PRIMARY_LAN || '1');
  if (key === 'autovpn.wifi.bootstrap_completed') process.stdout.write(process.env.COMPLETED || '0');
}
if (name === 'ucode') {
  const finalizing = args[0].endsWith('/wifi-bootstrap.uc');
  const failed = finalizing ? process.env.FINALIZE_FAIL : process.env.NETWORK_FAIL;
  process.stdout.write(JSON.stringify(failed ? {ok:false,code:'injected_failure'} : {ok:true,phase:'confirmed'})+'\\n');
  process.exit(failed ? 1 : 0);
}
`;
	for (const name of ['lock', 'uci', 'ucode']) fs.writeFileSync(path.join(tmp, name), mock, { mode: 0o755 });
	const source = fs.readFileSync(path.join(root, 'files/usr/sbin/autovpnctl'), 'utf8')
		.replace('/usr/libexec/autovpn/credential.sh', path.join(root, 'files/usr/libexec/autovpn/credential.sh'))
		.replaceAll('/usr/bin/ucode', path.join(tmp, 'ucode'));
	fs.writeFileSync(path.join(tmp, 'ctl'), source);
	function run(extra = {}) {
		fs.writeFileSync(log, '');
		const result = spawnSync('/bin/sh', [path.join(tmp, 'ctl'), 'network-confirm', '100-1'], {
			encoding: 'utf8', timeout: 10000,
			env: { ...process.env, PATH: tmp + ':' + process.env.PATH, MOCK_LOG: log, ...extra }
		});
		assert.equal(result.error, undefined);
		return { ...result, calls: fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse) };
	}
	const success = run();
	assert.equal(success.status, 0, success.stderr);
	assert.deepEqual(success.calls.filter(call => call[0] === 'ucode'), [
		['ucode', '/usr/libexec/autovpn/network-helper.uc', 'network-confirm', '100-1'],
		['ucode', '/usr/libexec/autovpn/wifi-bootstrap.uc', 'confirm', '100-1']
	]);
	assert.equal(run({ FINALIZE_FAIL: '1' }).status, 1);
	/* The already-confirmed journal can finish a previously failed marker commit. */
	assert.equal(run({ NETWORK_FAIL: '1' }).status, 0);
	for (const extra of [{ PRIMARY_LAN: '0' }, { COMPLETED: '1' }]) {
		const legacy = run({ ...extra, NETWORK_FAIL: '1' });
		assert.equal(legacy.status, 1);
		assert.equal(legacy.calls.filter(call => call[0] === 'ucode').length, 1);
	}
});
