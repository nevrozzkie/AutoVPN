#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename, error } from 'fs';
import { cursor } from 'uci';

const planner = require('autovpn.networks');
const transaction = require('autovpn.network_transaction');
const processRunner = require('autovpn.process');
const ROOT = '/etc/autovpn/networks';
const CONFIGS = planner.configs;
function atomic(path, raw) {
	return writefile(path + '.new', raw) == length(raw) && chmod(path + '.new', 0o600) != null &&
		rename(path + '.new', path) != null;
}
function command(argv, limit) {
	/* Only fixed executables/arguments. No Wi-Fi passwords on a command line. */
	/* coreutils-timeout is a package dependency; stock BusyBox omits this applet. */
	let bounded = ['/usr/bin/timeout', '20'];
	for (let i = 0; i < length(argv); i++) push(bounded, argv[i]);
	let child = processRunner.popen(bounded, 'r');
	if (child == null) return null;
	let result = child.read(limit + 1) || '';
	let code = child.close();
	return code == 0 && length(result) <= limit ? result : null;
}
function decoded(argv) {
	let raw = command(argv, 65536);
	try { return json(raw); } catch (e) { return null; }
}
function clean() {
	let ctx = cursor();
	for (let i = 0; i < length(CONFIGS); i++) {
		if (!ctx.load(CONFIGS[i])) return false;
		let changes = ctx.changes(CONFIGS[i]);
		if (changes == null) return false;
		let names = keys(changes);
		for (let c = 0; c < length(names); c++)
			if (length(changes[names[c]]) > 0) return false;
	}
	return true;
}
function stage(before, settings, module) {
	for (let i = 0; i < length(CONFIGS); i++)
		if (!atomic(ROOT + '/stage/' + CONFIGS[i], before[CONFIGS[i]])) return { ok: false, code: 'network_stage_write_failed' };
	let ctx = cursor(ROOT + '/stage', ROOT + '/delta', '');
	let configs = {};
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		if (!ctx.load(name) || !ctx.revert(name) || !ctx.load(name)) return { ok: false, code: 'network_config_invalid' };
		configs[name] = [];
		ctx.foreach(name, null, function(section) { push(configs[name], section); });
	}
	let planned = module.plan(settings, configs, decoded(['/sbin/ip', '-j', '-4', 'route', 'show', 'table', 'main']));
	if (!planned.ok) return planned;
	for (let i = 0; i < length(planned.remove); i++)
		if (!ctx.delete(planned.remove[i].config, planned.remove[i].name)) return { ok: false, code: 'network_stage_failed' };
	for (let i = 0; i < length(planned.patches); i++) {
		let patch = planned.patches[i];
		/* Existing hardware/BSS sections: change the one planned option only. */
		if (!ctx.set(patch.config, patch.name, patch.option, patch.value))
			return { ok: false, code: 'network_stage_failed' };
	}
	for (let i = 0; i < length(planned.sections); i++) {
		let section = planned.sections[i];
		if (!ctx.set(section.config, section.name, section.section_type)) return { ok: false, code: 'network_stage_failed' };
		let options = keys(section.values);
		for (let o = 0; o < length(options); o++)
			if (!ctx.set(section.config, section.name, options[o], section.values[options[o]]))
				return { ok: false, code: 'network_stage_failed' };
	}
	let after = {};
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		if (!ctx.commit(name) || chmod(ROOT + '/stage/' + name, 0o600) == null)
			return { ok: false, code: 'network_stage_write_failed' };
		after[name] = readfile(ROOT + '/stage/' + name, 65537);
	}
	return { ok: true, after: after, ssids: planned.ssids, radios: planned.radios,
		wired_ports: planned.wired_ports || [] };
}
function ready(state) {
	let wireless = decoded(['/bin/ubus', 'call', 'network.wireless', 'status']);
	if (type(wireless) != 'object') return false;
	let primaryLan = false;
	for (let i = 0; i < length(state.ssids); i++)
		if (state.ssids[i].mode == 'direct' && state.ssids[i].primary_lan === true) primaryLan = true;
	for (let r = 0; r < length(state.radios); r++) {
		let radio = wireless[state.radios[r]];
		if (radio == null || radio.up !== true || type(radio.interfaces) != 'array') return false;
		let names = [];
		for (let i = 0; i < length(radio.interfaces); i++)
			if (radio.interfaces[i].ifname) push(names, radio.interfaces[i].section);
		for (let i = 0; i < length(state.ssids); i++)
			if (state.ssids[i].enabled && index(names, 'avpn_' + state.ssids[i].mode + '_' + state.radios[r]) < 0) return false;
	}
	for (let i = 0; i < length(state.ssids); i++) {
		let mode = state.ssids[i];
		if (!mode.enabled || (mode.mode == 'direct' && primaryLan)) continue;
		let bridge = { direct: 'br-avpnd', vpn: 'br-avpn', zapret: 'br-avpndz', vpn_zapret: 'br-avpnz' }[mode.mode];
		if (bridge == null || command(['/sbin/ip', 'link', 'show', 'dev', bridge], 4096) == null) return false;
	}
	let vpnBridge = decoded(['/bin/ubus', 'call', 'network.device', 'status', '{"name":"br-avpn"}']);
	if (type(vpnBridge) != 'object' || type(vpnBridge['bridge-members']) != 'array') return false;
	for (let i = 0; i < length(state.wired_ports || []); i++)
		if (index(vpnBridge['bridge-members'], state.wired_ports[i]) < 0) return false;
	return command(['/sbin/ip', 'link', 'show', 'dev', 'br-avpn'], 4096) != null &&
		(primaryLan || command(['/sbin/ip', 'link', 'show', 'dev', 'br-avpnd'], 4096) != null);
}
const io = {
	now: function() { return time(); },
	uptime: function() {
		let raw = readfile('/proc/uptime', 128);
		if (type(raw) != 'string' || match(raw, /^[0-9]+\.[0-9]+ /) == null) die('uptime_unavailable');
		return int(split(raw, ' ')[0]);
	},
	load: function() {
		let raw = readfile(ROOT + '/journal.json', 1048577);
		/* Stable ucode returns null + fs.error(), not false, on ENOENT. */
		if (raw == null) return error() == 'No such file or directory' ? null : false;
		try { return length(raw) <= 1048576 ? json(raw) : false; } catch (e) { return false; }
	},
	save: function(state) {
		let raw = sprintf('%J\n', state);
		/* Finish the durable backup before any live UCI write. */
		return length(raw) <= 1048576 && atomic(ROOT + '/journal.json', raw) &&
			command(['/bin/busybox', 'sync'], 4096) != null;
	},
	read: function(name) { return readfile('/etc/config/' + name, 65537); },
	write: function(name, raw) {
		return atomic('/etc/config/' + name, raw) && command(['/bin/busybox', 'sync'], 4096) != null;
	},
	clean: clean, stage: stage, ready: ready,
	close: function() { return command(['/usr/libexec/autovpn/runtime-adapter', 'fail-closed'], 4096) != null; },
	watch: function() {
		return command(['/etc/init.d/autovpn-networks', 'start'], 4096) != null &&
			command(['/etc/init.d/autovpn-networks', 'running'], 4096) != null;
	},
	reload: function() {
		let network = command(['/etc/init.d/network', 'reload'], 4096) != null;
		let firewall = command(['/etc/init.d/firewall', 'reload'], 4096) != null;
		/* Raw UCI replacement emits no config.change; network reload alone does not reload BSSes. */
		let wifi = command(['/sbin/wifi', 'reload'], 4096) != null;
		let dhcp = command(['/etc/init.d/dnsmasq', 'restart'], 4096) != null;
		return firewall && network && wifi && dhcp;
	},
};
let result;
try {
	let action = ARGV[0];
	if (action == 'network-setup' || action == 'network-bootstrap') {
		let ctx = cursor();
		ctx.load('autovpn');
		let settings = { base_ssid: ctx.get('autovpn', 'wifi', 'base_ssid'),
			password: ctx.get('autovpn', 'wifi', 'password'),
			primary_lan: action == 'network-bootstrap' || ctx.get('autovpn', 'wifi', 'primary_lan') == '1' };
		if (action == 'network-bootstrap') {
			settings.initial_setup = true;
			let state = io.load();
			if (!transaction.valid(state)) result = { ok: false, code: 'network_journal_invalid' };
			else if (state != null && (state.phase == 'pending' || state.phase == 'rollback_conflict'))
				result = { ok: false, code: 'network_transaction_pending' };
			else if (state != null && state.phase == 'confirmed') result = { ok: false, code: 'network_already_configured' };
			else if (state != null && state.phase == 'rolled_back') {
				if (!io.clean()) result = { ok: false, code: 'uncommitted_network_changes' };
				else {
					for (let i = 0; i < length(CONFIGS); i++)
						if (io.read(CONFIGS[i]) != state.before[CONFIGS[i]]) result = { ok: false, code: 'network_config_changed' };
				}
			}
		}
		if (result == null) result = transaction.begin(settings, io, planner);
	}
	else if (action == 'network-confirm') result = transaction.confirm(ARGV[1], io);
	else if (action == 'network-tick' || action == 'network-boot') result = transaction.recover(io, action == 'network-boot');
	else if (action == 'network-status') result = transaction.status(io);
	else if (action == 'network-gate') result = transaction.gate(io);
	else result = { ok: false, code: 'unknown_network_action' };
}
catch (e) { result = { ok: false, code: 'network_helper_failed' }; }
printf('%J\n', result);
exit(result.ok !== true ? 1 : result.idle ? 3 : 0);
