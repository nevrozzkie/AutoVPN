'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const planner = loadUcodeModule(path.join(modules, 'networks.uc'));
const transaction = loadUcodeModule(path.join(modules, 'network-transaction.uc'));
const helperSource = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/network-helper.uc'), 'utf8')
	.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');

const type = value => value == null ? null : Array.isArray(value) ? 'array' : Number.isInteger(value) ? 'int' : typeof value;
const clone = value => structuredClone(value);
function baseConfigs() {
	return {
		network: [{ '.name': 'lan', '.type': 'interface', device: 'br-lan', ipaddr: '192.168.1.1', netmask: '255.255.255.0' }],
		wireless: [{ '.name': 'radio0', '.type': 'wifi-device', band: '2g' }, { '.name': 'radio1', '.type': 'wifi-device', band: '5g' }],
		dhcp: [{ '.name': 'lan', '.type': 'dhcp', interface: 'lan' }],
		firewall: [{ '.name': 'defaults', '.type': 'defaults' }, { '.name': 'wan', '.type': 'zone', name: 'wan', masq: '1', network: ['wan'] }],
	};
}
function fixture() {
	const configs = baseConfigs();
	const files = new Map();
	files.set('/proc/uptime', '100.00 20.00\n');
	for (const [name, value] of Object.entries(configs)) files.set('/etc/config/' + name, JSON.stringify(value));
	files.set('/etc/config/autovpn', JSON.stringify([{ '.name': 'wifi', '.type': 'wifi', base_ssid: 'Dorm', password: 'test-passphrase' }]));
	const env = { files, calls: [], modes: [], changes: {}, now: 100, exit: null, output: null, ready: true, failCommit: false, unreadable: '' };
	let lastError = null;
	function read(path, limit) {
		const value = files.get(path);
		if (path === env.unreadable) { lastError = 'Permission denied'; return null; }
		if (value == null) { lastError = 'No such file or directory'; return null; }
		return value.slice(0, limit);
	}
	function cursor(configDir = '/etc/config') {
		const data = {};
		return {
			load(name) {
				const raw = read(configDir + '/' + name, 1 << 20);
				if (raw == null) return false;
				data[name] = JSON.parse(raw); return true;
			},
			revert() { return true; },
			changes(name) { return Object.hasOwn(env.changes, name) ? env.changes[name] : {}; },
			get(config, section, option) {
				const item = (data[config] || []).find(value => value['.name'] === section);
				return item && item[option];
			},
			foreach(config, _kind, callback) { for (const item of data[config] || []) callback(clone(item)); },
			delete(config, section) { data[config] = (data[config] || []).filter(item => item['.name'] !== section); return true; },
			set(config, section, key, value) {
				let item = (data[config] || []).find(entry => entry['.name'] === section);
				if (!item) { item = { '.name': section }; (data[config] ||= []).push(item); }
				if (value === undefined) item['.type'] = key; else item[key] = clone(value);
				return true;
			},
			commit(config) { if (env.failCommit) return null; files.set(configDir + '/' + config, JSON.stringify(data[config])); return true; },
		};
	}
	function popen(argv) {
		env.calls.push(argv);
		const output = argv.includes('/sbin/ip') && argv.includes('-j') ? '[]' :
			argv.includes('/bin/ubus') ? JSON.stringify(Object.fromEntries(['radio0', 'radio1'].map(name => [name, {
				up: true, interfaces: env.ready ? ['direct', 'vpn'].map(mode => ({ ifname: 'test', section: 'avpn_' + mode + '_' + name })) : []
			}]))) : '';
		return { read: () => output, close: () => 0 };
	}
	function run(action, args = []) {
		env.exit = null; env.output = null;
		const uncaught = helperSource.replace("catch (e) { result = { ok: false, code: 'network_helper_failed' }; }", 'catch (e) { throw e; }');
		const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'mockPopen', 'error', 'cursor', 'require',
			'length', 'type', 'keys', 'push', 'json', 'sprintf', 'time', 'match', 'int', 'split', 'index', 'ARGV', 'printf', 'exit', uncaught);
		invoke(read,
			(file, raw) => { files.set(file, raw); return Buffer.byteLength(raw); }, (file, mode) => { env.modes.push([file, mode]); return true; },
			(from, to) => { files.set(to, files.get(from)); files.delete(from); return true; }, popen,
			() => { const value = lastError; lastError = null; return value; }, cursor,
			name => ({ 'autovpn.networks': planner, 'autovpn.network-transaction': transaction, 'autovpn.process': { popen } })[name],
			value => typeof value === 'string' ? Buffer.byteLength(value) : value.length, type, Object.keys,
			(array, value) => array.push(value), JSON.parse, (format, value) => JSON.stringify(value) + (format.endsWith('\n') ? '\n' : ''), () => env.now,
			(value, expression) => value.match(expression), value => Number.parseInt(value, 10), (value, separator) => value.split(separator),
			(value, needle) => value.indexOf(needle),
			[action, ...args], (_format, value) => { env.output = value; }, value => { env.exit = value; });
		return env.output;
	}
	return { env, run };
}

test('real network helper stages private files, commits UCI stage and returns redacted status', () => {
	const { env, run } = fixture();
	const result = run('network-setup');
	assert.equal(env.exit, 0, JSON.stringify(result));
	assert.equal(result.ok, true);
	assert.equal(result.phase, 'pending');
	assert.equal(JSON.stringify(result).includes('test-passphrase'), false);
	assert.equal(env.files.has('/etc/autovpn/networks/stage/network'), true);
	assert.equal(env.files.has('/etc/autovpn/networks/journal.json'), true);
	assert.match(env.files.get('/etc/config/wireless'), /avpn_vpn_radio0/);
	assert.ok(env.calls.some(argv => argv.includes('/usr/libexec/autovpn/runtime-adapter') && argv.includes('fail-closed')));
	assert.ok(env.calls.some(argv => argv.includes('/etc/init.d/autovpn-networks') && argv.includes('start')));
	assert.ok(env.calls.some(argv => argv.includes('/sbin/wifi') && argv.includes('reload')));
	assert.ok(env.modes.some(([name, mode]) => name.endsWith('/journal.json.new') && mode === 0o600));
	assert.equal(JSON.stringify(env.calls).includes('test-passphrase'), false);
});

test('real helper confirms only after both generated APs are present on each radio', () => {
	const { env, run } = fixture();
	const result = run('network-setup');
	env.ready = false;
	assert.equal(run('network-confirm', [result.transaction_id]).code, 'wifi_not_ready');
	env.ready = true;
	assert.equal(run('network-confirm', [result.transaction_id]).phase, 'confirmed');
});
test('real helper reloads wireless on timeout rollback as well as initial setup', () => {
	const { env, run } = fixture();
	const before = env.files.get('/etc/config/wireless');
	assert.equal(run('network-setup').phase, 'pending');
	env.files.set('/proc/uptime', '281.00 30.00\n');
	assert.equal(run('network-tick').phase, 'rolled_back');
	assert.equal(env.files.get('/etc/config/wireless'), before);
	assert.equal(env.calls.filter(argv => argv.includes('/sbin/wifi')).length, 2);
});

test('helper clean check accepts empty change records but rejects actual changes', () => {
	for (const [changes, expected] of [
		[{}, true],
		[{ network: [] }, true],
		[{ network: [{ option: 'ipaddr' }] }, false],
		[null, false],
	]) {
		const { env, run } = fixture();
		for (const name of ['network', 'wireless', 'dhcp', 'firewall']) env.changes[name] = changes;
		const result = run('network-setup');
		assert.equal(result.ok, expected, JSON.stringify(changes) + ' => ' + JSON.stringify(result));
		if (!expected) assert.equal(result.code, 'uncommitted_network_changes');
	}
});

test('staged UCI commit failure leaves live config unchanged; unreadable journal is not missing', () => {
	const { env, run } = fixture();
	const before = env.files.get('/etc/config/wireless');
	env.failCommit = true;
	assert.equal(run('network-setup').code, 'network_stage_write_failed');
	assert.equal(env.files.get('/etc/config/wireless'), before);
	env.unreadable = '/etc/autovpn/networks/journal.json';
	env.files.set(env.unreadable, 'private backup');
	assert.equal(run('network-setup').code, 'network_journal_invalid');
});

test('malformed journal is reported and never overwritten by a read-only status action', () => {
	const { env, run } = fixture();
	env.files.set('/etc/autovpn/networks/journal.json', '{bad-json');
	const result = run('network-status');
	assert.equal(result.code, 'network_journal_invalid');
	assert.equal(env.files.get('/etc/autovpn/networks/journal.json'), '{bad-json');
});

test('RPC confirm takes transaction_id from request.args and validates it before spawning', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/share/rpcd/ucode/luci.autovpn'), 'utf8')
		.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
	const calls = [];
	const rpc = new Function('require', 'push', 'length', 'type', 'match', 'json', source)(
		name => {
			assert.equal(name, 'autovpn.process');
			return { popen: argv => { calls.push(argv); return { read: () => '{"ok":true}', close: () => 0 }; } };
		},
		(array, value) => array.push(value), value => value.length, type,
		(value, expression) => value.match(expression), JSON.parse
	);
	assert.deepEqual(rpc['luci.autovpn'].network_confirm.call({ args: { transaction_id: '123-7' } }), { ok: true });
	assert.deepEqual(calls[0], ['/usr/sbin/autovpnctl', 'network-confirm', '123-7']);
	assert.equal(rpc['luci.autovpn'].network_confirm.call({ args: { transaction_id: 'bad' } }).code, 'invalid_transaction_id');
});
