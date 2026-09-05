'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');
const root = path.resolve(__dirname, '..');
const modules = path.join(root, 'files/usr/share/ucode/autovpn');
const planner = loadUcodeModule(path.join(modules, 'networks.uc'));
const tx = loadUcodeModule(path.join(modules, 'network-transaction.uc'));
const clone = value => structuredClone(value);
const settings = { base_ssid: 'Общага', password: "hello-'$;pass" };
function configs() {
	return {
		network: [{ '.name': 'lan', '.type': 'interface', device: 'br-lan', ipaddr: '192.168.1.1', netmask: '255.255.255.0' }],
		wireless: [{ '.name': 'radio0', '.type': 'wifi-device', band: '2g' },
			{ '.name': 'radio1', '.type': 'wifi-device', band: '5g' },
			{ '.name': 'default_radio0', '.type': 'wifi-iface', ssid: 'Management', network: 'lan', key: 'untouched' }],
		dhcp: [{ '.name': 'lan', '.type': 'dhcp', interface: 'lan' }],
		firewall: [{ '.name': 'defaults', '.type': 'defaults' }, { '.name': 'wan', '.type': 'zone', name: 'wan', network: ['wan'], masq: '1' }]
	};
}
function installed(base, plan) {
	const result = clone(base);
	for (const item of plan.remove) result[item.config] = result[item.config].filter(s => s['.name'] !== item.name);
	for (const patch of plan.patches) result[patch.config].find(s => s['.name'] === patch.name)[patch.option] = patch.value;
	for (const item of plan.sections) result[item.config].push({ '.name': item.name, '.type': item.section_type, ...item.values });
	return result;
}
test('creates personal WPA2 SSIDs on both bands and preserves management sections', () => {
	const base = configs();
	const before = clone(base);
	const plan = planner.plan(settings, base, []);
	assert.equal(plan.ok, true);
	assert.deepEqual(base, before);
	assert.deepEqual(plan.ssids.map(s => s.ssid), ['Общага', 'Общага-в', 'Общага-з', 'Общага-вз']);
	const aps = plan.sections.filter(s => s.config === 'wireless');
	assert.equal(aps.length, 8);
	assert.equal(aps.filter(s => s.values.disabled === '0').length, 8);
	for (const ap of aps) {
		assert.equal(ap.values.encryption, 'psk2+ccmp');
		assert.equal(ap.values.key, settings.password);
		assert.equal(ap.values.isolate, '1');
	}
	assert.deepEqual(plan.remove, []);
	assert.equal(plan.sections.some(s => s.name === 'lan' || s.name === 'radio0'), false);
	const direct = plan.sections.find(s => s.name === 'avpn_direct_wan');
	assert.equal(direct.values.dest, 'wan');
	assert.equal(plan.sections.filter(s => s.section_type === 'forwarding').length, 2);
});
test('repeat setup updates only owned sections and does not duplicate APs or bridges', () => {
	const first = planner.plan(settings, configs(), []);
	const base = installed(configs(), first);
	const second = planner.plan({ ...settings, base_ssid: 'Dorm', password: 'new-password' }, base,
		[{ dst: '192.168.30.0/24', dev: 'br-avpn' }, { dst: '172.30.255.0/30', dev: 'avpn0' }]);
	assert.equal(second.ok, true);
	const final = installed(base, second);
	assert.equal(final.wireless.length, base.wireless.length);
	assert.equal(final.network.length, base.network.length);
	assert.equal(final.wireless.find(s => s['.name'] === 'default_radio0').key, 'untouched');
});
test('validates SSID byte length and WPA2 passphrase including metacharacters as data', () => {
	for (const name of ['', 'a'.repeat(28), 'я'.repeat(14), 'bad\nssid'])
		assert.equal(planner.validSettings({ ...settings, base_ssid: name }).ok, false);
	assert.equal(planner.validSettings({ ...settings, base_ssid: 'я'.repeat(13) + 'a' }).ok, true);
	assert.equal(Buffer.byteLength('я'.repeat(13) + 'a-вз'), 32);
	assert.equal(planner.validSettings({ ...settings, initial_setup: '1' }).ok, false);
	assert.equal(planner.validSettings({ ...settings, primary_lan: 1 }).ok, false);
	for (const password of ['', 'short', 'g'.repeat(64), 'bad\npassword', 'пароль123'])
		assert.equal(planner.validSettings({ ...settings, password }).ok, false);
	assert.equal(planner.validSettings({ ...settings, password: 'f'.repeat(64) }).ok, true);
	assert.equal(planner.validSettings(settings).ok, true);
});
test('refuses conflicting resources, prefixes and subnets, including WAN routes', () => {
	for (const [config, section, code] of [
		['network', { '.name': 'avpn_vpn', '.type': 'interface' }, 'network_ownership_conflict'],
		['network', { '.name': 'other', '.type': 'device', name: 'br-avpn' }, 'network_ownership_conflict'],
		['network', { '.name': 'other', '.type': 'interface', ipaddr: '192.168.30.1/24' }, 'network_subnet_conflict'],
		['network', { '.name': 'other', '.type': 'interface', ipaddr: '192.168.2.1', netmask: '255.255.0.0' }, 'network_subnet_conflict'],
		['network', { '.name': 'other', '.type': 'interface', ipaddr: '192.168.2.1', netmask: '255.0.255.0' }, 'network_subnet_conflict'],
		['wireless', { '.name': 'other', '.type': 'wifi-iface', network: 'avpn_vpn' }, 'network_ownership_conflict'],
		['wireless', { '.name': 'other', '.type': 'wifi-iface', ssid: 'Общага-в' }, 'ssid_already_exists'],
		['firewall', { '.name': 'other', '.type': 'defaults', flow_offloading: '1' }, 'disable_flow_offloading']
	]) {
		const base = configs(); base[config].push(section);
		assert.equal(planner.plan(settings, base, []).code, code);
	}
	assert.equal(planner.plan(settings, configs(), [{ dst: '192.168.0.0/16', dev: 'wan' }]).code, 'network_subnet_conflict');
	assert.equal(planner.plan(settings, configs(), [{ dst: '192.168.31.0/24', dev: 'br-avpn' }]).code, 'network_subnet_conflict');
	assert.equal(planner.plan(settings, configs(), [{ dst: 'default', dev: 'wan' }]).ok, true);
});
test('uses enabled radios only, rejects missing radios/WAN instead of changing them', () => {
	const base = configs(); base.wireless[1].disabled = '1';
	assert.deepEqual(planner.plan(settings, base, []).radios, ['radio0']);
	base.wireless[0].disabled = '1';
	assert.equal(planner.plan(settings, base, []).code, 'enabled_wifi_radio_required');
	const noWan = configs(); noWan.firewall.pop();
	assert.equal(planner.plan(settings, noWan, []).code, 'masquerading_wan_zone_required');
});

function freshOpenWrt() {
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
			{ '.name': 'wan', '.type': 'zone', name: 'wan', network: ['wan', 'wan6'], masq: '1' },
			{ '.name': 'lan_wan', '.type': 'forwarding', src: 'lan', dest: 'wan' }],
	};
}

test('initial primary-LAN plan safely enables both stock-disabled radios without replacing their hardware options', () => {
	const base = freshOpenWrt();
	const planned = planner.plan({ ...settings, base_ssid: 'OpenWrt', initial_setup: true, primary_lan: true }, base, []);
	assert.equal(planned.ok, true);
	assert.deepEqual(planned.radios, ['radio0', 'radio1']);
	assert.deepEqual(planned.patches, [
		{ config: 'wireless', name: 'default_radio0', option: 'disabled', value: '1' },
		{ config: 'wireless', name: 'radio0', option: 'disabled', value: '0' },
		{ config: 'wireless', name: 'default_radio1', option: 'disabled', value: '1' },
		{ config: 'wireless', name: 'radio1', option: 'disabled', value: '0' },
	]);
	assert.deepEqual(planned.ssids, [
		{ ssid: 'OpenWrt', enabled: true, mode: 'direct', primary_lan: true },
		{ ssid: 'OpenWrt-в', enabled: true, mode: 'vpn', primary_lan: false },
		{ ssid: 'OpenWrt-з', enabled: true, mode: 'zapret', primary_lan: false },
		{ ssid: 'OpenWrt-вз', enabled: true, mode: 'vpn_zapret', primary_lan: false },
	]);
	const directAps = planned.sections.filter(item => item.config === 'wireless' && item.name.startsWith('avpn_direct_'));
	assert.equal(directAps.length, 2);
	assert.ok(directAps.every(item => JSON.stringify(item.values.network) === '["lan"]'));
	assert.equal(planned.sections.some(item => item.config !== 'wireless' && item.name.startsWith('avpn_direct')), false);
	assert.equal(planned.sections.filter(item => item.config === 'wireless' && item.values.disabled === '1').length, 0);
	const applied = installed(base, planned);
	assert.deepEqual(applied.wireless.find(item => item['.name'] === 'radio0'),
		{ ...base.wireless[0], disabled: '0' });
	assert.deepEqual(applied.wireless.find(item => item['.name'] === 'radio1'),
		{ ...base.wireless[2], disabled: '0' });
	assert.equal(applied.wireless.find(item => item['.name'] === 'default_radio0').disabled, '1');
	assert.equal(applied.wireless.find(item => item['.name'] === 'default_radio1').disabled, '1');
});

test('initial setup preserves disabled custom BSSes but refuses activating an unknown BSS with its radio', () => {
	let base = freshOpenWrt();
	base.wireless.push({ '.name': 'guest', '.type': 'wifi-iface', device: 'radio0', mode: 'ap',
		network: 'lan', ssid: 'Private', encryption: 'psk2', key: 'untouched', disabled: '1' });
	let planned = planner.plan({ ...settings, initial_setup: true, primary_lan: true }, base, []);
	assert.equal(planned.ok, true);
	assert.equal(planned.patches.some(patch => patch.name === 'guest'), false);
	assert.equal(installed(base, planned).wireless.find(item => item['.name'] === 'guest').key, 'untouched');
	base = freshOpenWrt();
	base.wireless.push({ '.name': 'guest', '.type': 'wifi-iface', device: 'radio0', mode: 'ap',
		network: 'lan', ssid: 'Private', encryption: 'none' });
	assert.equal(planner.plan({ ...settings, initial_setup: true, primary_lan: true }, base, []).code,
		'disabled_radio_bss_conflict');
	base = freshOpenWrt();
	base.wireless[0].disabled = '0';
	base.wireless.push({ '.name': 'custom', '.type': 'wifi-iface', device: 'radio0', mode: 'ap',
		network: 'lan', ssid: settings.base_ssid, encryption: 'psk2', key: 'untouched' });
	assert.equal(planner.plan({ ...settings, initial_setup: true, primary_lan: true }, base, []).code, 'ssid_already_exists');
});

test('primary-LAN bootstrap requires dual-band radios and a complete static LAN-to-WAN policy', () => {
	for (const mutate of [
		base => { base.network[0].proto = 'dhcp'; },
		base => { base.firewall = base.firewall.filter(item => item.name !== 'lan'); },
		base => { base.firewall = base.firewall.filter(item => item.src !== 'lan'); },
		base => { base.wireless = base.wireless.filter(item => item.device !== 'radio1' && item['.name'] !== 'radio1'); },
	]) {
		const base = freshOpenWrt(); mutate(base);
		assert.equal(planner.plan({ ...settings, initial_setup: true, primary_lan: true }, base, []).ok, false);
	}
});
function fixture() {
	const files = Object.fromEntries(planner.configs.map(n => [n, 'before-' + n]));
	let state = null;
	const env = { files, events: [], clock: 1000, fail: '', dirty: false, ready: true,
		get state() { return state; }, set state(value) { state = clone(value); } };
	const io = {
		now: () => env.clock,
		uptime: () => env.clock,
		load: () => clone(state),
		save: value => { env.events.push('journal:' + value.phase); if (env.fail === 'journal') return false; state = clone(value); return true; },
		read: name => files[name],
		write: (name, raw) => { env.events.push('write:' + name); if (env.fail === name) { env.fail = ''; return false; } files[name] = raw; return true; },
		clean: () => !env.dirty,
		close: () => { env.events.push('close'); return env.fail !== 'close'; },
		watch: () => { env.events.push('watch'); return env.fail !== 'watch'; },
		reload: () => { env.events.push('reload'); if (env.fail === 'reload') { env.fail = ''; return false; } return true; },
		ready: () => env.ready,
		stage: () => ({ ok: true, after: Object.fromEntries(planner.configs.map(n => [n, 'after-' + n])),
			ssids: [{ ssid: settings.base_ssid, enabled: true }], radios: ['radio0', 'radio1'] })
	};
	return Object.assign(env, { io, begin: () => tx.begin(settings, io, planner) });
}
test('network write-ahead transaction requires matching timely confirmation and ready APs', () => {
	const env = fixture();
	const result = env.begin();
	assert.equal(result.phase, 'pending');
	assert.equal(env.state.deadline, 1180);
	assert.ok(env.events.indexOf('close') < env.events.indexOf('write:network'));
	assert.ok(env.events.indexOf('journal:pending') < env.events.indexOf('write:network'));
	assert.ok(env.events.indexOf('watch') < env.events.indexOf('write:network'));
	assert.equal(env.begin().code, 'network_transaction_pending');
	assert.equal(tx.confirm('foreign-id', env.io).code, 'network_confirmation_mismatch');
	env.ready = false;
	assert.equal(tx.confirm(result.transaction_id, env.io).code, 'wifi_not_ready');
	env.ready = true;
	assert.equal(tx.confirm(result.transaction_id, env.io).phase, 'confirmed');
	env.clock = 1300;
	assert.equal(tx.recover(env.io, false).idle, true);
	assert.equal(env.files.network, 'after-network');
	assert.equal(JSON.stringify(tx.status(env.io)).includes('before-network'), false);
});
test('watchdog timeout and reboot both restore full original files, including partial writes', () => {
	for (const boot of [true, false]) {
		const env = fixture(); env.begin();
		if (!boot) {
			assert.equal(tx.recover(env.io, false).pending, true);
			env.clock = 1180;
		} else env.files.wireless = 'before-wireless';
		assert.equal(tx.recover(env.io, boot).phase, 'rolled_back');
		for (const name of planner.configs) assert.equal(env.files[name], 'before-' + name);
	}
});
test('clock corrections cannot extend the confirmation deadline', () => {
	const env = fixture();
	env.io.now = () => 999999;
	env.begin();
	env.io.now = () => 0; // NTP moved wall time backwards, monotonic uptime still advances.
	env.clock = 1180;
	assert.equal(tx.recover(env.io, false).phase, 'rolled_back');
});
test('VPN gate stays closed for unconfirmed, conflicted or unreadable network state', () => {
	const env = fixture();
	assert.equal(tx.gate(env.io).ok, true);
	const pending = env.begin();
	assert.equal(tx.gate(env.io).code, 'network_confirmation_required');
	assert.equal(tx.confirm(pending.transaction_id, env.io).phase, 'confirmed');
	assert.equal(tx.gate(env.io).ok, true);
	env.state = { ...env.state, phase: 'rollback_conflict' };
	assert.equal(tx.gate(env.io).code, 'network_confirmation_required');
	env.state = false;
	assert.equal(tx.gate(env.io).code, 'network_journal_invalid');
});
test('failed writes/reload/watchdog rollback and concurrent edits are never overwritten', () => {
	for (const failure of ['network', 'wireless', 'dhcp', 'firewall', 'reload', 'watch']) {
		const env = fixture(); env.fail = failure;
		assert.equal(env.begin().ok, false, failure);
		for (const name of planner.configs) assert.equal(env.files[name], 'before-' + name, failure + ' ' + name);
	}
	const env = fixture(); env.begin(); env.files.wireless = 'user-changes'; env.clock = 1300;
	assert.equal(tx.recover(env.io, false).code, 'network_rollback_requires_attention');
	assert.equal(env.files.wireless, 'user-changes');
	assert.equal(env.state.phase, 'rollback_conflict');
	assert.equal(env.begin().code, 'network_transaction_pending');
});
test('bad journal, dirty UCI, guard failure and failed backup cause no config writes', () => {
	for (const failure of ['journal', 'close', 'dirty', 'invalid']) {
		const env = fixture();
		if (failure === 'dirty') env.dirty = true;
		else if (failure === 'invalid') env.state = { version: 999 };
		else env.fail = failure;
		assert.equal(env.begin().ok, false);
		assert.equal(env.events.some(item => item.startsWith('write:')), false);
	}
});
test('CLI, package and watchdog wire network recovery before boot networking; no password argv', () => {
	const ctl = fs.readFileSync(path.join(root, 'files/usr/sbin/autovpnctl'), 'utf8');
	const init = fs.readFileSync(path.join(root, 'files/etc/init.d/autovpn-networks'), 'utf8');
	const helper = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/network-helper.uc'), 'utf8');
	assert.match(ctl, /network-setup\|network-tick\|network-boot\|network-status/);
	assert.match(ctl, /network-confirm\)/);
	assert.match(init, /START=18/);
	assert.match(init, /autovpnctl network-boot/);
	assert.match(helper, /cursor\(ROOT \+ '\/stage', ROOT \+ '\/delta', ''\)/);
	assert.match(helper, /action == 'network-bootstrap'/);
	assert.doesNotMatch(helper, /popen\([^\n]*password/);
});
