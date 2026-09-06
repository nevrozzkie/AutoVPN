'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const os = require('node:os');
const { spawnSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const policy = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/setup_policy.uc'));

function helperFixture(overrides = {}) {
	const files = new Map(Object.entries(overrides.files || {}));
	const values = {
		'main.credential_file': '/etc/autovpn/credentials', 'main.enabled': '0', 'main.setup_prepared': '0',
		'main.base_url': '', 'main.router_id': '', 'runtime.wan_device': '', 'wifi.base_ssid': '', 'wifi.password': '',
		...(overrides.values || {})
	};
	const events = [];
	let lastError = 'No such file or directory';
	const ctx = {
		load: () => true,
		get: (_config, section, option) => values[section + '.' + option],
		set: (_config, section, option, value) => { events.push('set:' + section + '.' + option); values[section + '.' + option] = value; return true; },
		commit: () => overrides.commit === false ? false : true,
		changes: () => overrides.dirty ? { main: ['enabled'] } : {}
	};
	const access = (name, mode) => {
		assert.equal(mode, 'f', `unknown fs.access mode for ${name}`);
		if (name === '/sys/class/net/eth0' || name === '/etc/autovpn' || name === '/etc/autovpn/state' || files.has(name)) return true;
		lastError = 'No such file or directory';
		return null;
	};
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/setup-helper.uc'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import .*?;\s*$/gm, '')
		.replace("const setupPolicy = require('autovpn.setup_policy');", 'const setupPolicy = injectedPolicy;')
		.replace("const processRunner = require('autovpn.process');", 'const processRunner = injectedProcess;')
		.replace(/let action = ARGV\[0\];[\s\S]*$/, 'return { configure, activate, resume, saveResult };');
	const api = new Function('access', 'chmod', 'fsError', 'lstat', 'mkdir', 'readfile', 'rename', 'stat', 'unlink', 'writefile', 'cursor',
		'injectedPolicy', 'injectedProcess', 'type', 'length', 'keys', 'match', 'index', 'substr', 'rindex', 'json', 'sprintf', 'trim', source)(
		access, () => true, () => lastError, name => {
			if (files.has(name)) return { type: 'file' };
			lastError = 'No such file or directory'; return null;
		}, () => true,
		(name) => files.get(name) ?? null,
		(from, to) => { if (overrides.renameFails && to === '/etc/autovpn/credentials') return null; files.set(to, files.get(from)); files.delete(from); return true; },
		name => {
			if (overrides.unreadable === name) { lastError = 'Permission denied'; return null; }
		if (files.has(name)) return { type: 'file' };
			lastError = 'No such file or directory'; return null;
		}, name => files.delete(name),
		(name, value) => { files.set(name, value); return value.length; }, () => ctx, policy,
		{ popen: () => ({ read: () => JSON.stringify({ ok: !overrides.invalidNetwork }), close: () => overrides.invalidNetwork ? 1 : 0 }) },
		value => value === null || value === undefined ? null : Array.isArray(value) ? 'array' : typeof value === 'number' ? 'int' : typeof value,
		value => typeof value === 'string' ? Buffer.byteLength(value) : value.length, Object.keys,
		(value, expression) => value.match(expression), (value, needle) => value.indexOf(needle),
		(value, start, count) => count === undefined ? value.substring(start) : value.substring(start, start + count),
		value => value.lastIndexOf('/'), JSON.parse, (format, value) => format === '%J\n' ? JSON.stringify(value) + '\n' : JSON.stringify(value), value => value.trim()
	);
	return { api, files, values, events };
}

function validRequest() {
	return JSON.stringify({ base_url: 'https://vpn.example', router_id: 'router_123',
		credential: 'avrt_abcdefgh.' + 's'.repeat(43), base_ssid: 'Dorm', password: 'good-pass', wan_device: 'eth0' });
}

test('injected helper configures a clean first run, and rejects invalid nonce before touching files', () => {
	const env = helperFixture({ files: { '/dev/stdin': validRequest() } });
	assert.deepEqual(env.api.configure('nonce-0123456789'), { ok: true, configured: true, wan_device: 'eth0' });
	assert.equal(env.files.get('/etc/autovpn/credentials'), 'avrt_abcdefgh.' + 's'.repeat(43) + '\n');
	assert.equal(env.values['main.enabled'], '0');
	assert.equal(env.values['main.setup_prepared'], '1');
	const invalid = helperFixture({ files: { '/dev/stdin': validRequest() } });
	assert.equal(invalid.api.configure('bad').code, 'invalid_setup_nonce');
	assert.equal(invalid.events.length, 0);
});

test('SSID validation rejects NUL, controls and DEL without rejecting ordinary UTF-8 or WPA2 input', () => {
	for (const base_ssid of ['bad\0ssid', 'bad\x01ssid', 'bad\x1fssid', 'bad\x7fssid']) {
		const request = { ...JSON.parse(validRequest()), base_ssid };
		const env = helperFixture({ files: { '/dev/stdin': JSON.stringify(request) } });
		assert.equal(env.api.configure('nonce-0123456789').code, 'setup_request_invalid');
		assert.equal(env.events.length, 0);
	}
	const request = { ...JSON.parse(validRequest()), base_ssid: 'вифи' };
	const accepted = helperFixture({ files: { '/dev/stdin': JSON.stringify(request) } });
	assert.deepEqual(accepted.api.configure('nonce-0123456789'), { ok: true, configured: true, wan_device: 'eth0' });
	assert.equal(accepted.values['wifi.password'], 'good-pass');
});

test('injected helper preserves active setup and makes credential-write failure safely retryable', () => {
	const active = helperFixture({ values: { 'main.enabled': '1' }, files: { '/dev/stdin': validRequest(), '/etc/autovpn/credentials': 'old\n' } });
	assert.equal(active.api.configure('nonce-0123456789').code, 'existing_installation');
	assert.equal(active.events.length, 0);
	const retry = helperFixture({ files: { '/dev/stdin': validRequest() }, renameFails: true });
	assert.equal(retry.api.configure('nonce-0123456789').code, 'credential_write_failed');
	assert.equal(retry.values['main.enabled'], '0');
	retry.files.set('/dev/stdin', validRequest());
	/* The preactivation commit is durable; a later credential write may retry with the same identity. */
	retry.api = helperFixture({ files: Object.fromEntries(retry.files), values: retry.values }).api;
	assert.equal(retry.api.configure('nonce-0123456789').ok, true);
});

test('injected helper treats null stat with an error other than ENOENT as an existing boundary', () => {
	const env = helperFixture({ files: { '/dev/stdin': validRequest() }, unreadable: '/etc/autovpn/state/journal.json' });
	assert.equal(env.api.configure('nonce-0123456789').code, 'existing_installation');
	assert.equal(env.events.length, 0);
});

test('injected helper gates activation on confirmed networks and rejects dirty UCI', () => {
	const files = { '/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'confirmed' }),
		'/etc/autovpn/credentials': 'avrt_abcdefgh.' + 's'.repeat(43) + '\n' };
	const values = { 'main.setup_prepared': '1', 'main.base_url': 'https://vpn.example', 'main.router_id': 'router_123' };
	const env = helperFixture({ files, values });
	assert.deepEqual(env.api.activate(), { ok: true, enabled: true });
	assert.equal(env.values['main.enabled'], '1');
	const dirty = helperFixture({ files, values, dirty: true });
	assert.equal(dirty.api.activate().code, 'existing_installation');
});

test('pairing reuses confirmed installer Wi-Fi without disclosing or silently changing its key', () => {
	const request = { ...JSON.parse(validRequest()), password: '' };
	const values = { 'wifi.primary_lan': '1', 'wifi.bootstrap_completed': '1',
		'wifi.base_ssid': 'Dorm', 'wifi.password': 'installer-private-key' };
	const files = { '/dev/stdin': JSON.stringify(request),
		'/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'confirmed' }) };
	const env = helperFixture({ files, values });
	const result = env.api.configure('nonce-0123456789');
	assert.equal(result.ok, true);
	assert.equal(env.values['wifi.password'], values['wifi.password']);
	assert.equal(JSON.stringify(result).includes(values['wifi.password']), false);
	for (const change of [{ base_ssid: 'Other' }, { password: 'changed-key' }]) {
		const other = helperFixture({ values, files: { ...files, '/dev/stdin': JSON.stringify({ ...request, ...change }) } });
		assert.equal(other.api.configure('nonce-0123456789').code, 'wifi_change_use_networks');
		assert.equal(other.events.length, 0);
	}
	const unconfirmed = helperFixture({ values, files: { ...files,
		'/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'pending' }) } });
	assert.equal(unconfirmed.api.configure('nonce-0123456789').code, 'existing_installation');
});

test('bootstrap flag permits only confirmed no-credential first pairing', () => {
	const boot = { uci_clean: true, enabled: false, controller_journal: false, network_phase: 'confirmed',
		network_invalid: false, prepared: false, credential_present: false, bootstrap_confirmed: true };
	assert.deepEqual(policy.firstRunAllowed(boot), { ok: true, retry: false });
	for (const change of [{ network_phase: 'pending' }, { enabled: true }, { credential_present: true },
		{ controller_journal: true }, { bootstrap_confirmed: false }])
		assert.equal(policy.firstRunAllowed({ ...boot, ...change }).ok, false);
});

test('first-run policy preserves an active or otherwise existing installation', () => {
	const clean = { uci_clean: true, enabled: false, controller_journal: false, network_phase: null,
		network_invalid: false, prepared: false, identity_matches: false, credential_present: false };
	assert.deepEqual(policy.firstRunAllowed(clean), { ok: true, retry: false });
	for (const changed of [
		{ enabled: true }, { controller_journal: true }, { network_phase: 'pending' },
		{ network_phase: 'confirmed' }, { network_invalid: true }, { credential_present: true }, { uci_clean: false }
	]) assert.equal(policy.firstRunAllowed({ ...clean, ...changed }).ok, false);
});

test('only a matching, inactive prepared setup can retry', () => {
	const retry = { uci_clean: true, enabled: false, controller_journal: false, network_phase: null,
		network_invalid: false, prepared: true, identity_matches: true, credential_present: true };
	assert.deepEqual(policy.firstRunAllowed(retry), { ok: true, retry: true });
	assert.deepEqual(policy.firstRunAllowed({ ...retry, identity_matches: false }), { ok: false, code: 'setup_identity_locked' });
});

test('first-run pairing keeps the credential out of command arguments, UCI and results', () => {
	const rpc = read('files/usr/share/rpcd/ucode/luci.autovpn');
	const helper = read('files/usr/libexec/autovpn/setup-helper.uc');
	const cli = read('files/usr/sbin/autovpnctl');

	assert.match(rpc, /popen\(\['\/usr\/sbin\/autovpnctl', 'setup', nonce\], 'w'\)/);
	assert.match(rpc, /credential: request\.args\.credential/);
	assert.doesNotMatch(rpc, /autovpnctl', 'setup', nonce, .*credential/);
	assert.match(helper, /readfile\('\/dev\/stdin', REQUEST_LIMIT \+ 1\)/);
	assert.match(helper, /atomic\(path, request\.credential \+ '\\n'\)/);
	assert.doesNotMatch(helper, /ctx\.set\('autovpn', [^\n]*credential/);
	assert.match(helper, /return \{ ok: true, configured: true, wan_device: wan \}/);
	assert.match(cli, /setup_action configure "\$\{2:-\}"/);
	assert.doesNotMatch(cli, /setup_action configure .*credential/);
});

test('first-run activation is gated by a confirmed network transaction', () => {
	const helper = read('files/usr/libexec/autovpn/setup-helper.uc');
	const cli = read('files/usr/sbin/autovpnctl');
	assert.match(helper, /state\.phase != 'confirmed'/);
	assert.match(helper, /ctx\.set\('autovpn', 'main', 'enabled', '1'\)/);
	assert.match(cli, /\/etc\/init\.d\/autovpn enable/);
	assert.match(cli, /\/etc\/init\.d\/autovpn start/);
	assert.doesNotMatch(cli, /\/etc\/init\.d\/autovpn restart/);
	assert.match(cli, /exec 9>&-[\s\S]*\/etc\/init\.d\/autovpn enable/);
	assert.match(cli, /uci set autovpn\.main\.enabled=0/);
});

test('configure and ordinary activation refuse every maintenance/update gate', () => {
	const request = validRequest();
	for (const [path, gate] of [
		['/etc/autovpn/state/maintenance.lock', { schema_version: 1, action: 'rebind', phase: 'ready' }],
		['/etc/autovpn/state/update.lock', { schema_version: 1, phase: 'ready' }],
		['/etc/autovpn/state/maintenance.lock', { schema_version: 1, action: 'rebind', phase: 'running' }]
	]) {
		const configured = helperFixture({ files: { '/dev/stdin': request, [path]: JSON.stringify(gate) } });
		assert.equal(configured.api.configure('nonce-0123456789').code, gate.phase == 'ready' ? 'maintenance_pending' : 'maintenance_gate_invalid');
		const active = helperFixture({ files: {
			[path]: JSON.stringify(gate), '/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'confirmed' }),
			'/etc/autovpn/credentials': 'avrt_abcdefgh.' + 's'.repeat(43) + '\n'
		}, values: { 'main.setup_prepared': '1', 'main.base_url': 'https://vpn.example', 'main.router_id': 'router_123' } });
		assert.equal(active.api.activate().code, gate.phase == 'ready' ? 'maintenance_pending' : 'maintenance_gate_invalid');
	}
});

test('resume requires a strict ready gate, clean valid binding and confirmed networks, then clears only ready locks', () => {
	const files = {
		'/etc/autovpn/state/maintenance.lock': JSON.stringify({ schema_version: 1, action: 'rebind', phase: 'ready' }),
		'/etc/autovpn/state/update.lock': JSON.stringify({ schema_version: 1, phase: 'ready' }),
		'/etc/autovpn/networks/journal.json': JSON.stringify({ phase: 'confirmed' }),
		'/etc/autovpn/credentials': 'avrt_abcdefgh.' + 's'.repeat(43) + '\n'
	};
	const env = helperFixture({ files, values: { 'main.base_url': 'https://vpn.example', 'main.router_id': 'router_123' } });
	assert.deepEqual(env.api.resume(), { ok: true, enabled: true, resumed: true });
	assert.equal(env.values['main.enabled'], '1');
	assert.equal(env.files.has('/etc/autovpn/state/maintenance.lock'), false);
	assert.equal(env.files.has('/etc/autovpn/state/update.lock'), false);
	assert.equal(helperFixture({ files: { '/etc/autovpn/state/maintenance.lock': JSON.stringify({ schema_version: 1, action: 'reset', phase: 'failed' }) } }).api.resume().code, 'maintenance_gate_invalid');
	const invalid = helperFixture({ files, values: { 'main.base_url': 'https://vpn.example', 'main.router_id': 'router_123' }, invalidNetwork: true });
	assert.equal(invalid.api.resume().code, 'networks_not_confirmed');
	assert.equal(invalid.files.has('/etc/autovpn/state/maintenance.lock'), true);
	assert.equal(invalid.values['main.enabled'], '0');
});

test('wizard asks rpcd to detect WAN and leaves radio/offload policy to LuCI', () => {
	const rpc = read('files/usr/share/rpcd/ucode/luci.autovpn');
	const view = read('files/www/luci-static/resources/view/autovpn/setup.js');
	assert.match(rpc, /network\.interface\.wan', 'status'/);
	assert.match(rpc, /response\.l3_device/);
	assert.match(view, /WAN is detected from the active OpenWrt wan interface/);
	assert.match(view, /Radio settings and hardware offloading remain standard LuCI controls/);
});

test('real CLI activation releases its lock before service start and leaves failed start retryable', t => {
	const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-setup-cli-'));
	t.after(() => fs.rmSync(tmp, { recursive: true, force: true }));
	const log = path.join(tmp, 'calls');
	const program = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
fs.appendFileSync(process.env.MOCK_LOG, JSON.stringify([name,...args]) + '\\n');
if (name === 'uci' && args.includes('get')) process.stdout.write('/etc/autovpn/credentials\\n');
if (name === 'ucode') {
  if (args.includes('resume') && process.env.MOCK_STATE) fs.unlinkSync(process.env.MOCK_STATE + '/maintenance.lock');
  process.stdout.write(JSON.stringify(process.env.MOCK_HELPER_FAIL ? {ok:false,code:'networks_not_confirmed'} : {ok:true,enabled:true})+'\\n');
  process.exit(process.env.MOCK_HELPER_FAIL ? 1 : 0);
}
if (name === 'init-autovpn' && args[0] === 'start' && process.env.MOCK_START_FAIL) process.exit(1);
if (name === 'init-autovpn') {
  try {
    const fd = fs.fstatSync(9), lock = fs.statSync(path.join(path.dirname(process.argv[1]), 'controller.lock'));
    if (fd.dev === lock.dev && fd.ino === lock.ino) process.exit(90);
  } catch (_) {}
}
`;
	for (const name of ['flock', 'uci', 'ucode', 'init-autovpn']) fs.writeFileSync(path.join(tmp, name), program, { mode: 0o755 });
	const script = read('files/usr/sbin/autovpnctl')
		.replace('/var/lock/autovpn-controller.lock', path.join(tmp, 'controller.lock'))
		.replaceAll('/etc/autovpn/state', path.join(tmp, 'state'))
		.replace('/usr/libexec/autovpn/credential.sh', path.join(root, 'files/usr/libexec/autovpn/credential.sh'))
		.replaceAll('/usr/bin/ucode', path.join(tmp, 'ucode'))
		.replaceAll('/etc/init.d/autovpn', path.join(tmp, 'init-autovpn'));
	fs.writeFileSync(path.join(tmp, 'ctl'), script);
	const run = (env, command = 'setup-activate') => {
		fs.writeFileSync(log, '');
		const result = spawnSync('/bin/sh', [path.join(tmp, 'ctl'), command], {
			encoding: 'utf8', timeout: 10000,
			env: { ...process.env, PATH: tmp + ':' + process.env.PATH, MOCK_LOG: log, ...env }
		});
		assert.equal(result.error, undefined);
		return { ...result, events: fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse) };
	};
	const success = run({});
	assert.equal(success.status, 0, success.stderr);
	assert.deepEqual(JSON.parse(success.stdout), { ok: true, enabled: true });
	const start = success.events.findIndex(e => e[0] === 'init-autovpn' && e[1] === 'start');
	assert.ok(start >= 0);
	assert.equal(success.events.filter(e => e[0] === 'flock').length, 1);
	assert.equal(success.events.some(e => e.includes('restart') || e.includes('stop')), false);
	const failed = run({ MOCK_START_FAIL: '1' });
	assert.notEqual(failed.status, 0);
	assert.equal(JSON.parse(failed.stdout).code, 'service_start_failed');
	assert.ok(failed.events.some(e => e.join(' ') === 'uci set autovpn.main.enabled=0'));
	assert.ok(failed.events.some(e => e.join(' ') === 'uci set autovpn.main.setup_prepared=1'));
	const denied = run({ MOCK_HELPER_FAIL: '1' });
	assert.notEqual(denied.status, 0);
	assert.equal(denied.events.some(e => e[0] === 'init-autovpn'), false);
	const resumed = run({}, 'setup-resume');
	assert.equal(resumed.status, 0, resumed.stderr);
	const resumeStart = resumed.events.findIndex(e => e[0] === 'init-autovpn' && e[1] === 'start');
	assert.ok(resumeStart >= 0);
	const state = path.join(tmp, 'state');
	fs.mkdirSync(state);
	fs.writeFileSync(path.join(state, 'maintenance.lock'), JSON.stringify({ schema_version: 1, action: 'rebind', phase: 'ready' }));
	const restoreCtl = script.replaceAll('/etc/autovpn/state', state);
	fs.writeFileSync(path.join(tmp, 'restore-ctl'), restoreCtl, { mode: 0o755 });
	fs.writeFileSync(log, '');
	const restored = spawnSync('/bin/sh', [path.join(tmp, 'restore-ctl'), 'maintenance-resume'], {
		encoding: 'utf8', timeout: 10000,
		env: { ...process.env, PATH: tmp + ':' + process.env.PATH, MOCK_LOG: log, MOCK_START_FAIL: '1', MOCK_STATE: state }
	});
	assert.notEqual(restored.status, 0);
	assert.equal(JSON.parse(restored.stdout).code, 'service_start_failed');
	assert.deepEqual(JSON.parse(fs.readFileSync(path.join(state, 'maintenance.lock'), 'utf8')), { schema_version: 1, action: 'rebind', phase: 'ready' });
	assert.equal(fs.existsSync(path.join(state, '.resume-maintenance.lock')), false);
});

test('update commands bypass the controller lock and dispatch only the fixed update helper actions', () => {
	const cli = read('files/usr/sbin/autovpnctl');
	assert.match(cli, /case "\$command" in\s+update-check\)/);
	assert.match(cli, /exec \/usr\/libexec\/autovpn\/update-helper check "\$\{2:-\}"/);
	assert.match(cli, /update-apply\)[\s\S]*update-helper queue/);
	assert.match(cli, /update-status\)[\s\S]*update-helper status/);
	const beforeLock = cli.indexOf('update-check)');
	assert.ok(beforeLock >= 0 && beforeLock < cli.lastIndexOf('acquire_lock'));
});
