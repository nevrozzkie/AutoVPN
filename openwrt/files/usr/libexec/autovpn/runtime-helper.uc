#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename, access } from 'fs';
import { cursor } from 'uci';

const machine = require('autovpn.state');
const journal = require('autovpn.journal');
const runtime = require('autovpn.runtime');
const processRunner = require('autovpn.process');
const ROOT = '/etc/autovpn/runtime';

function readJson(path, limit) {
	let raw = readfile(path, limit + 1);
	if (raw == null || length(raw) > limit) return null;
	try { return json(raw); } catch (e) { return null; }
}
function writePrivate(path, value) {
	let raw = sprintf('%J\n', value);
	if (readfile(path, 65537) == raw) return true;
	return writefile(path + '.new', raw) == length(raw) &&
		chmod(path + '.new', 0o600) != null && rename(path + '.new', path) != null;
}
function command(argv) {
	let child = processRunner.popen(argv, 'r');
	if (child == null) return false;
	child.read(1);
	return child.close() == 0;
}
function awgAvailable() {
	if ((access('/usr/bin/awg', 'x') !== true && access('/sbin/awg', 'x') !== true)) return false;
	if (access('/sys/module/amneziawg') === true) return true;
	return command(['/sbin/modprobe', 'amneziawg']) && access('/sys/module/amneziawg') === true;
}
function policy() {
	let uci = cursor();
	uci.load('autovpn');
	return {
		selection: uci.get('autovpn', 'runtime', 'selection') || 'auto',
		wan_device: uci.get('autovpn', 'runtime', 'wan_device') || '',
		dns_server: uci.get('autovpn', 'runtime', 'dns_server') || '1.1.1.1',
		direct_domains: uci.get('autovpn', 'runtime', 'direct_domains') || [],
		direct_cidrs: uci.get('autovpn', 'runtime', 'direct_cidrs') || [],
		/* subscription preserves the server's Hysteria TLS verification setting. */
		hysteria_tls_mode: uci.get('autovpn', 'runtime', 'hysteria_tls_mode') || 'subscription',
		/* Optional package: never make VLESS/Hysteria depend on it. */
		awg_available: awgAvailable(),
	};
}
function run() {
	let action = ARGV[0];
	if (type(ARGV[1]) != 'string' || match(ARGV[1], /^\/etc\/autovpn\/[A-Za-z0-9_.-]+\/journal\.json$/) == null)
		return { ok: false, code: 'invalid_journal_path' };
	let state = readJson(ARGV[1], 262144);
	if (!journal.validState(state, machine.validateSnapshot)) return { ok: false, code: 'journal_invalid' };
	let uci = cursor();
	uci.load('autovpn');
	let routerId = uci.get('autovpn', 'main', 'router_id');
	let slots = ['desired', 'applied', 'last_good'];
	for (let i = 0; i < length(slots); i++) {
		let entry = state[slots[i]];
		if (entry != null && entry.snapshot.router_id != routerId)
			return { ok: false, code: 'router_identity_mismatch' };
	}
	let entry = action == 'prepare' || action == 'activate' || action == 'verify' ? state.desired : state.applied;
	if (entry == null) return { ok: true, empty: true };
	let value;
	if (action == 'prepare') {
		let result = runtime.bundle(entry, policy(), machine);
		if (!result.ok) return result;
		value = result.value;
		if (!writePrivate(ROOT + '/prepared.json', value)) return { ok: false, code: 'runtime_write_failed' };
	}
	else {
		let file = action == 'activate' ? 'prepared' : 'current';
		value = readJson(ROOT + '/' + file + '.json', 65536);
		if (!runtime.matchesBundle(value, entry, machine)) {
			if (action != 'rollback' && action != 'restore') return { ok: false, code: 'runtime_bundle_mismatch' };
			value = readJson(ROOT + '/previous.json', 65536);
			if (!runtime.matchesBundle(value, entry, machine)) return { ok: false, code: 'rollback_bundle_missing' };
		}
	}
	if (action == 'activate') {
		let current = readJson(ROOT + '/current.json', 65536);
		if (state.applied != null && runtime.matchesBundle(current, state.applied, machine)) {
			if (!writePrivate(ROOT + '/previous.json', current)) return { ok: false, code: 'runtime_write_failed' };
		}
		else if (state.applied != null && !runtime.matchesBundle(readJson(ROOT + '/previous.json', 65536), state.applied, machine))
			return { ok: false, code: 'rollback_bundle_missing' };
	}
	if (action == 'activate' || action == 'rollback' || action == 'restore')
		if (!writePrivate(ROOT + '/current.json', value)) return { ok: false, code: 'runtime_write_failed' };
	if (action != 'verify' && action != 'status') {
		if (!writePrivate(ROOT + '/candidate.json', value.config))
			return { ok: false, code: 'runtime_write_failed' };
		/* This holds private keys and is only ever consumed by awg-apply. */
		if (!writePrivate(ROOT + '/awg.json', value.awg))
			return { ok: false, code: 'runtime_write_failed' };
	}
	return { ok: true, active_profile: value.profile, capabilities: value.capabilities };
}

let result;
try { result = run(); } catch (e) { result = { ok: false, code: 'runtime_helper_failed' }; }
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
