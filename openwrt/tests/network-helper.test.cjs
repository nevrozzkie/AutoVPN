'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const planner = loadUcodeModule(path.join(modules, 'networks.uc'));
const transaction = loadUcodeModule(path.join(modules, 'network_transaction.uc'));
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
function freshConfigs() {
	return {
		network: [{ '.name': 'lan', '.type': 'interface', device: 'br-lan', proto: 'static',
			ipaddr: '192.168.1.1', netmask: '255.255.255.0' }],
		wireless: [
			{ '.name': 'radio0', '.type': 'wifi-device', type: 'mac80211', band: '2g', channel: '1', htmode: 'HE20', disabled: '1' },
			{ '.name': 'default_radio0', '.type': 'wifi-iface', device: 'radio0', mode: 'ap', network: 'lan', ssid: 'OpenWrt', encryption: 'none' },
			{ '.name': 'radio1', '.type': 'wifi-device', type: 'mac80211', band: '5g', channel: '36', htmode: 'HE80', disabled: '1' },
			{ '.name': 'default_radio1', '.type': 'wifi-iface', device: 'radio1', mode: 'ap', network: 'lan', ssid: 'OpenWrt', encryption: 'none' },
		],
		dhcp: [{ '.name': 'lan', '.type': 'dhcp', interface: 'lan' }],
		firewall: [{ '.name': 'defaults', '.type': 'defaults' },
			{ '.name': 'lan', '.type': 'zone', name: 'lan', network: ['lan'], input: 'ACCEPT', output: 'ACCEPT', forward: 'ACCEPT' },
			{ '.name': 'wan', '.type': 'zone', name: 'wan', masq: '1', network: ['wan', 'wan6'] },
			{ '.name': 'lan_wan', '.type': 'forwarding', src: 'lan', dest: 'wan' }],
	};
}
function fixture(options = {}) {
	const configs = options.fresh ? freshConfigs() : baseConfigs();
	const files = new Map();
	files.set('/proc/uptime', '100.00 20.00\n');
	for (const [name, value] of Object.entries(configs)) files.set('/etc/config/' + name, JSON.stringify(value));
	files.set('/etc/config/autovpn', JSON.stringify([{ '.name': 'wifi', '.type': 'wifi', base_ssid: options.baseSsid || 'Dorm',
		password: 'test-passphrase', primary_lan: options.primaryLan ? '1' : '0' }]));
	const env = { files, calls: [], modes: [], changes: {}, now: 100, exit: null, output: null, ready: true, failCommit: false, unreadable: '' };
	env.sets = [];
	env.missingBridges = new Set();
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
				env.sets.push([config, section, key, clone(value)]);
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
		if (argv[0] === '/bin/busybox' && argv[1] === 'timeout')
			return { read: () => '', close: () => 127 };
		const output = argv.includes('/sbin/ip') && argv.includes('-j') ? '[]' :
			argv.includes('/bin/ubus') ? JSON.stringify(Object.fromEntries(['radio0', 'radio1'].map(name => [name, {
				up: true, interfaces: env.ready ? ['direct', 'vpn', 'zapret', 'vpn_zapret'].map(mode => ({ ifname: 'test', section: 'avpn_' + mode + '_' + name })) : []
			}]))) : '';
		let status = 0;
		if (argv.includes('/sbin/ip') && argv.includes('link') && argv.includes('dev')) {
			let device = argv[argv.indexOf('dev') + 1];
			if (env.missingBridges.has(device)) status = 1;
		}
		return { read: () => output, close: () => status };
	}
	function run(action, args = [], catchErrors = false) {
		env.exit = null; env.output = null;
		const source = catchErrors ? helperSource : helperSource.replace(
			"catch (e) { result = { ok: false, code: 'network_helper_failed' }; }",
			'catch (e) { throw e; }'
		);
		const invoke = new Function('readfile', 'writefile', 'chmod', 'rename', 'mockPopen', 'error', 'cursor', 'require', 'die',
			'length', 'type', 'keys', 'push', 'json', 'sprintf', 'time', 'match', 'int', 'split', 'index', 'ARGV', 'printf', 'exit', source);
		invoke(read,
			(file, raw) => { files.set(file, raw); return Buffer.byteLength(raw); }, (file, mode) => { env.modes.push([file, mode]); return true; },
			(from, to) => { files.set(to, files.get(from)); files.delete(from); return true; }, popen,
			() => { const value = lastError; lastError = null; return value; }, cursor,
			name => ({ 'autovpn.networks': planner, 'autovpn.network_transaction': transaction, 'autovpn.process': { popen } })[name],
			value => { throw value; },
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

test('network bootstrap and rollback use the packaged timeout without a BusyBox timeout applet', () => {
	const { env, run } = fixture({ fresh: true, primaryLan: true });
	assert.equal(run('network-bootstrap').phase, 'pending');
	env.files.set('/proc/uptime', '400.00 20.00\n');
	assert.equal(run('network-tick').phase, 'rolled_back');
	assert.ok(env.calls.length > 0);
	for (const argv of env.calls) assert.deepEqual(argv.slice(0, 2), ['/usr/bin/timeout', '20']);
	assert.match(fs.readFileSync(path.join(root, 'Makefile'), 'utf8'), /\+coreutils-timeout(?:\s|$)/);
});

test('network-bootstrap patches the actual stock radios and keeps the primary SSID on management LAN', () => {
	const { env, run } = fixture({ fresh: true, primaryLan: true, baseSsid: 'x' });
	const result = run('network-bootstrap');
	assert.equal(env.exit, 0, JSON.stringify(result));
	assert.equal(result.phase, 'pending');
	assert.deepEqual(result.ssids.map(item => item.ssid), ['x', 'x-в']);
	assert.deepEqual(result.ssids.map(item => item.enabled), [true, true]);
	assert.equal(result.ssids[0].primary_lan, true);
	const wireless = JSON.parse(env.files.get('/etc/config/wireless'));
	assert.deepEqual(wireless.find(item => item['.name'] === 'radio0'),
		{ ...freshConfigs().wireless[0], disabled: '0' });
	assert.deepEqual(wireless.find(item => item['.name'] === 'radio1'),
		{ ...freshConfigs().wireless[2], disabled: '0' });
	assert.equal(wireless.find(item => item['.name'] === 'default_radio0').disabled, '1');
	assert.equal(wireless.find(item => item['.name'] === 'default_radio1').disabled, '1');
	assert.deepEqual(wireless.find(item => item['.name'] === 'avpn_direct_radio0').network, ['lan']);
	const network = JSON.parse(env.files.get('/etc/config/network'));
	assert.equal(network.some(item => item['.name'] === 'avpn_direct' || item['.name'] === 'avpn_direct_bridge'), false);
	assert.ok(network.some(item => item['.name'] === 'avpn_vpn'));
	assert.deepEqual(env.sets.filter(call => call[1] === 'radio0' || call[1] === 'radio1' || call[1].startsWith('default_radio')), [
		['wireless', 'default_radio0', 'disabled', '1'],
		['wireless', 'radio0', 'disabled', '0'],
		['wireless', 'default_radio1', 'disabled', '1'],
		['wireless', 'radio1', 'disabled', '0'],
	]);
	// Primary-LAN readiness needs the VPN bridge, but never the removed direct bridge.
	env.calls.length = 0;
	env.missingBridges.add('br-avpnd');
	assert.equal(run('network-confirm', [result.transaction_id]).phase, 'confirmed');
	assert.equal(env.calls.some(argv => argv.includes('br-avpnd')), false);
});

test('network-bootstrap is one-shot, but a clean complete rollback can be retried', () => {
	{
		const { env, run } = fixture({ fresh: true, primaryLan: true });
		let pending = run('network-bootstrap');
		assert.equal(run('network-bootstrap').code, 'network_transaction_pending');
		const journal = JSON.parse(env.files.get('/etc/autovpn/networks/journal.json'));
		journal.phase = 'rollback_conflict';
		env.files.set('/etc/autovpn/networks/journal.json', JSON.stringify(journal));
		assert.equal(run('network-bootstrap').code, 'network_transaction_pending');
		journal.phase = 'pending';
		env.files.set('/etc/autovpn/networks/journal.json', JSON.stringify(journal));
		assert.equal(run('network-confirm', [pending.transaction_id]).phase, 'confirmed');
		assert.equal(run('network-bootstrap').code, 'network_already_configured');
	}
	{
		const { env, run } = fixture({ fresh: true, primaryLan: true });
		assert.equal(run('network-bootstrap').phase, 'pending');
		env.files.set('/proc/uptime', '281.00 30.00\n');
		assert.equal(run('network-tick').phase, 'rolled_back');
		assert.equal(run('network-bootstrap').phase, 'pending');
	}
	{
		const { env, run } = fixture({ fresh: true, primaryLan: true });
		assert.equal(run('network-bootstrap').phase, 'pending');
		env.files.set('/proc/uptime', '281.00 30.00\n');
		assert.equal(run('network-tick').phase, 'rolled_back');
		env.files.set('/etc/config/wireless', env.files.get('/etc/config/wireless') + '\n');
		assert.equal(run('network-bootstrap').code, 'network_config_changed');
	}
});

test('ordinary network-setup preserves primary-LAN topology but never enables a disabled radio', () => {
	const disabled = fixture({ fresh: true, primaryLan: true });
	assert.equal(disabled.run('network-setup').code, 'enabled_wifi_radio_required');
	assert.equal(disabled.env.sets.length, 0);
	const enabled = fixture({ fresh: true, primaryLan: true });
	let wireless = JSON.parse(enabled.env.files.get('/etc/config/wireless'));
	for (const name of ['radio0', 'radio1']) wireless.find(item => item['.name'] === name).disabled = '0';
	enabled.env.files.set('/etc/config/wireless', JSON.stringify(wireless));
	assert.equal(enabled.run('network-setup').phase, 'pending');
	const network = JSON.parse(enabled.env.files.get('/etc/config/network'));
	assert.equal(network.some(item => item['.name'] === 'avpn_direct'), false);
});

test('real helper confirms only after both generated APs are present on each radio', () => {
	const { env, run } = fixture();
	const result = run('network-setup');
	env.ready = false;
	assert.equal(run('network-confirm', [result.transaction_id]).code, 'wifi_not_ready');
	env.ready = true;
	const legacy = JSON.parse(env.files.get('/etc/autovpn/networks/journal.json'));
	for (const ssid of legacy.ssids) delete ssid.primary_lan;
	env.files.set('/etc/autovpn/networks/journal.json', JSON.stringify(legacy));
	env.missingBridges.add('br-avpnd');
	assert.equal(run('network-confirm', [result.transaction_id]).code, 'wifi_not_ready');
	env.missingBridges.delete('br-avpnd');
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

test('invalid uptime remains a catchable helper failure', () => {
	const { env, run } = fixture();
	env.files.set('/proc/uptime', 'unavailable\n');
	assert.equal(run('network-setup', [], true).code, 'network_helper_failed');
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
