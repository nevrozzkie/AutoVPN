#!/usr/bin/ucode
'use strict';

/* Installer-only entry point. Two private stdin lines, never secret argv/env. */
import { readfile, lstat, error as fsError } from 'fs';
import { cursor } from 'uci';

const planner = require('autovpn.networks');
const transaction = require('autovpn.network_transaction');
const runner = require('autovpn.process');
const JOURNAL = '/etc/autovpn/networks/journal.json';

function fail(code) { return { ok: false, code: code }; }
function absent(path) {
	return lstat(path) == null && fsError() == 'No such file or directory';
}
function clean(ctx) {
	let configs = ['autovpn', 'network', 'wireless', 'dhcp', 'firewall'];
	for (let i = 0; i < length(configs); i++) {
		if (!ctx.load(configs[i])) return false;
		let changes = ctx.changes(configs[i]);
		if (changes == null) return false;
		let names = keys(changes);
		for (let n = 0; n < length(names); n++) if (length(changes[names[n]]) > 0) return false;
	}
	return true;
}
function journal() {
	if (absent(JOURNAL)) return { ok: true, state: null };
	let info = lstat(JOURNAL);
	if (info == null || info.type != 'file') return fail('network_journal_invalid');
	let raw = readfile(JOURNAL, 1048577);
	let state;
	try { state = raw != null && length(raw) <= 1048576 ? json(raw) : false; } catch (e) { state = false; }
	return state != null && transaction.valid(state) ? { ok: true, state: state } : fail('network_journal_invalid');
}
function sameConfig(state, slot) {
	let names = planner.configs;
	for (let i = 0; i < length(names); i++)
		if (readfile('/etc/config/' + names[i], 65537) != state[slot][names[i]]) return false;
	return true;
}
function callNetwork() {
	let child = runner.popen(['/usr/bin/ucode', '/usr/libexec/autovpn/network-helper.uc', 'network-bootstrap'], 'r');
	if (child == null) return fail('network_start_failed');
	let raw = child.read(4097) || '';
	let code = child.close();
	let result;
	try { result = length(raw) <= 4096 ? json(raw) : null; } catch (e) { result = null; }
	return type(result) == 'object' && type(result.ok) == 'bool' && (code == 0 || !result.ok)
		? result : fail('network_invalid_response');
}
function run() {
	let ctx = cursor();
	if (!clean(ctx)) return fail('uncommitted_network_changes');
	if (!absent('/etc/autovpn/state/maintenance.lock') || !absent('/etc/autovpn/state/update.lock'))
		return fail('maintenance_pending');
	if (ctx.get('autovpn', 'main', 'enabled') == '1' || ctx.get('autovpn', 'main', 'setup_prepared') == '1' ||
		(ctx.get('autovpn', 'main', 'base_url') || '') != '' || (ctx.get('autovpn', 'main', 'router_id') || '') != '' ||
		!absent('/etc/autovpn/credentials') || !absent('/etc/autovpn/state/journal.json'))
		return fail('existing_installation');
	let loaded = journal();
	if (!loaded.ok) return loaded;
	let state = loaded.state;
	if (ARGV[0] == 'resume' && (state == null || (state.phase == 'rolled_back' && sameConfig(state, 'before'))))
		return { ok: true, phase: 'not_configured' };
	if (ARGV[0] == 'confirm' || ARGV[0] == 'resume') {
		/* A confirmed journal is durable user consent. Recover only its missing
		 * completion marker, without asking for or changing the Wi-Fi key. */
		if (state == null || state.phase != 'confirmed' || (ARGV[0] == 'confirm' && state.id != ARGV[1]) ||
			ctx.get('autovpn', 'wifi', 'primary_lan') != '1' || !sameConfig(state, 'after'))
			return fail('bootstrap_confirmation_mismatch');
		if (!ctx.set('autovpn', 'wifi', 'bootstrap_completed', '1') || !ctx.commit('autovpn'))
			return fail('bootstrap_commit_failed');
		return { ok: true, phase: 'confirmed', transaction_id: state.id };
	}
	if (ARGV[0] != 'configure') return fail('invalid_bootstrap_action');
	if (ctx.get('autovpn', 'wifi', 'bootstrap_completed') == '1') return fail('existing_installation');
	if (state != null && (state.phase != 'rolled_back' || !sameConfig(state, 'before')))
		return fail('network_already_configured');
	let raw = readfile('/dev/stdin', 257);
	if (raw == null || length(raw) > 256) return fail('invalid_wifi_input');
	let lines = split(raw, '\n');
	if (length(lines) != 3 || lines[2] != '') return fail('invalid_wifi_input');
	let settings = { base_ssid: lines[0], password: lines[1] };
	let valid = planner.validSettings(settings);
	if (!valid.ok) return valid;
	if (!ctx.set('autovpn', 'wifi', 'base_ssid', settings.base_ssid) ||
		!ctx.set('autovpn', 'wifi', 'password', settings.password) ||
		!ctx.set('autovpn', 'wifi', 'primary_lan', '1') ||
		!ctx.set('autovpn', 'wifi', 'bootstrap_completed', '0') || !ctx.commit('autovpn'))
		return fail('bootstrap_commit_failed');
	return callNetwork();
}

let result;
try { result = run(); } catch (e) { result = fail('wifi_bootstrap_failed'); }
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
