#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename, access } from 'fs';
import { cursor } from 'uci';

const machine = require('autovpn.state');
const journal = require('autovpn.journal');
const runtime = require('autovpn.runtime');
const processRunner = require('autovpn.process');
const lanes = require('autovpn.lanes');
let ROOT = null;
let LANE = null;

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
	let result = {
		selection: uci.get('autovpn', LANE.id == 'vpn_zapret' ? 'runtime_zapret' : 'runtime', 'selection') || 'auto',
		wan_device: uci.get('autovpn', 'runtime', 'wan_device') || '',
		dns_server: uci.get('autovpn', 'runtime', 'dns_server') || '1.1.1.1',
		direct_domains: uci.get('autovpn', 'runtime', 'direct_domains') || [],
		direct_cidrs: uci.get('autovpn', 'runtime', 'direct_cidrs') || [],
		/* subscription preserves the server's Hysteria TLS verification setting. */
		hysteria_tls_mode: uci.get('autovpn', 'runtime', 'hysteria_tls_mode') || 'subscription',
		/* Optional package: never make VLESS/Hysteria depend on it. */
		awg_available: awgAvailable(),
	};
	let enabled = uci.get('autovpn', 'runtime', 'zapret_enabled');
	if (enabled != null && enabled != '0' && enabled != '1') return {};
	if (enabled == '1') {
		let repeats = uci.get('autovpn', 'runtime', 'zapret_repeats') || '2';
		result.zapret = {
			vless: uci.get('autovpn', 'runtime', 'zapret_vless') || 'split',
			hysteria2: uci.get('autovpn', 'runtime', 'zapret_hysteria2') || 'fake',
			amneziawg: uci.get('autovpn', 'runtime', 'zapret_amneziawg') || 'off',
			repeats: match(repeats, /^[1-6]$/) != null ? int(repeats) : 0,
		};
	}
	return result;
}
function fixedProfile(value) {
	if (type(value) == 'object' && index(['vless-reality', 'hysteria2', 'amneziawg'], value.profile) >= 0)
		return value.profile;
	return null;
}
function upgrade(value, entry, preferred) {
	if (entry.snapshot.schema_version >= 4) {
		if (value.version == 3) return { ok: true, value: value };
		return runtime.bundle(entry, value.policy, machine, fixedProfile(value) || preferred, LANE.id);
	}
	if (value.version != 1) return { ok: true, value: value };
	let own = fixedProfile(value);
	return runtime.bundle(entry, value.policy, machine, own != null ? own : preferred, LANE.id);
}
function probeEntry(state) {
	if ((state.phase == 'ACTIVATING' || state.phase == 'VERIFYING') && state.desired != null) return state.desired;
	if ((state.phase == 'IDLE' || state.phase == 'ROLLING_BACK') && state.applied != null) return state.applied;
	return null;
}
function optionalAwgEntry(state) {
	if ((state.phase == 'ACTIVATING' || state.phase == 'VERIFYING') && state.desired != null)
		return state.desired;
	if ((state.phase == 'IDLE' || state.phase == 'ROLLING_BACK') && state.applied != null)
		return state.applied;
	return null;
}
function sameCommitPolicy(current, staged) {
	if (sprintf('%J', staged) == sprintf('%J', current)) return true;
	let downgraded = json(sprintf('%J', current));
	downgraded.awg_available = false;
	return current.awg_available === true && sprintf('%J', staged) == sprintf('%J', downgraded);
}
function emptyResult() {
	return { ok: true, empty: true, lane: LANE.id, legacy_transport: false };
}
function response(value, result) {
	result.lane = LANE.id;
	result.legacy_transport = value.version < 3;
	return result;
}
function unsupported(entry) {
	return LANE.id == 'vpn_zapret' && entry.snapshot.schema_version < 4;
}
function expectedVersion(entry) {
	return entry.snapshot.schema_version >= 4 ? 3 : 2;
}
function run() {
	let action = ARGV[0];
	let laneId = ARGV[3] == null ? 'vpn' : ARGV[3];
	LANE = lanes.get(laneId);
	if (LANE == null) return { ok: false, code: 'invalid_lane' };
	ROOT = LANE.root;
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
	if (action == 'disable-awg' || action == 'fallback-awg') {
		let activeEntry = optionalAwgEntry(state);
		if (activeEntry == null) return { ok: false, code: 'invalid_phase' };
		if (unsupported(activeEntry)) return emptyResult();
		let candidate = readJson(ROOT + '/candidate.json', 65536);
		let chosen = null;
		let file = null;
		let bundleFiles = ['failover', 'current'];
		for (let i = 0; i < length(bundleFiles); i++) {
			let name = bundleFiles[i];
			let value = readJson(ROOT + '/' + name + '.json', 65536);
			if (runtime.matchesBundle(value, activeEntry, machine, LANE.id) &&
				value.version == expectedVersion(activeEntry) &&
				sprintf('%J', value.config) == sprintf('%J', candidate)) {
				chosen = value;
				file = name;
				break;
			}
		}
		if (chosen == null) return { ok: false, code: 'runtime_bundle_mismatch' };
		if (action == 'disable-awg' && chosen.profile == 'amneziawg') return { ok: false, code: 'active_awg_required' };
		if (action == 'fallback-awg' && chosen.policy.selection != 'auto')
			return { ok: false, code: 'selection_not_auto' };
		let fallbackPolicy = json(sprintf('%J', chosen.policy));
		fallbackPolicy.awg_available = false;
		let preferred = action == 'fallback-awg' ? ARGV[2] : chosen.profile;
		if (action == 'fallback-awg') file = 'failover';
		let fallback = runtime.bundle(activeEntry, fallbackPolicy, machine, preferred, LANE.id);
		if (!fallback.ok || (action == 'disable-awg' && fallback.value.profile != chosen.profile))
			return { ok: false, code: 'selected_vpn_unavailable' };
		if (!writePrivate(ROOT + '/candidate.json', fallback.value.config) ||
			!writePrivate(ROOT + '/awg.json', null) ||
			!writePrivate(ROOT + '/zapret.json', fallback.value.zapret) ||
			!writePrivate(ROOT + '/' + file + '.json', fallback.value))
			return { ok: false, code: 'runtime_write_failed' };
		return response(fallback.value, { ok: true, active_profile: fallback.value.profile,
			capabilities: fallback.value.capabilities });
	}
	if (action == 'probe-info' || action == 'probe-info-live' || action == 'select-profile' || action == 'commit-profile') {
		let activeEntry = probeEntry(state);
		if (activeEntry == null) return { ok: false, code: 'invalid_phase' };
		if (unsupported(activeEntry)) return emptyResult();
		let current = readJson(ROOT + '/current.json', 65536);
		if (!runtime.matchesBundle(current, activeEntry, machine, LANE.id))
			return { ok: false, code: 'runtime_bundle_mismatch' };
		if (current.version != expectedVersion(activeEntry)) return { ok: false, code: 'runtime_upgrade_required' };
		if (action == 'probe-info-live' &&
			sprintf('%J', current.config) != sprintf('%J', readJson(ROOT + '/run.json', 65536)))
			return { ok: false, code: 'runtime_bundle_mismatch' };
		let identity = current.etag + ':' + current.attempt + ':' + current.profile;
		if (action == 'probe-info' || action == 'probe-info-live') return response(current, {
			ok: true,
			active_profile: current.profile,
			selection: current.policy.selection == 'auto' ? 'auto' : 'manual',
			candidates: current.candidates,
			identity: identity,
		});
		let requested = ARGV[2];
		if (index(['vless-reality', 'hysteria2', 'amneziawg'], requested) < 0)
			return { ok: false, code: 'unsupported_profile' };
		if (current.policy.selection != 'auto') return { ok: false, code: 'selection_not_auto' };
		if (index(current.candidates, requested) < 0) return { ok: false, code: 'selected_vpn_unavailable' };
		if (action == 'commit-profile') {
			let staged = readJson(ROOT + '/failover.json', 65536);
			let candidate = readJson(ROOT + '/candidate.json', 65536);
			if (!runtime.matchesBundle(staged, activeEntry, machine, LANE.id) ||
				staged.version != expectedVersion(activeEntry) ||
				staged.profile != requested || staged.policy.selection != 'auto' ||
				!sameCommitPolicy(current.policy, staged.policy) ||
				sprintf('%J', staged.config) != sprintf('%J', candidate))
				return { ok: false, code: 'failover_bundle_mismatch' };
			if (!writePrivate(ROOT + '/current.json', staged)) return { ok: false, code: 'runtime_write_failed' };
			return response(staged, { ok: true, active_profile: staged.profile,
				capabilities: staged.capabilities });
		}
		let selected = runtime.bundle(activeEntry, current.policy, machine, requested, LANE.id);
		if (!selected.ok) return selected;
		if (!writePrivate(ROOT + '/failover.json', selected.value) ||
			!writePrivate(ROOT + '/candidate.json', selected.value.config) ||
			!writePrivate(ROOT + '/zapret.json', selected.value.zapret) ||
			!writePrivate(ROOT + '/awg.json', selected.value.awg))
			return { ok: false, code: 'runtime_write_failed' };
		return response(selected.value, { ok: true, active_profile: selected.value.profile,
			capabilities: selected.value.capabilities });
	}
	let entry = action == 'prepare' || action == 'activate' || action == 'verify' ? state.desired : state.applied;
	if (entry == null) return emptyResult();
	if (unsupported(entry)) return emptyResult();
	let value;
	if (action == 'prepare') {
		let current = readJson(ROOT + '/current.json', 65536);
		let preferred = state.applied != null && runtime.matchesBundle(current, state.applied, machine, LANE.id)
			? fixedProfile(current) : null;
		let result = runtime.bundle(entry, policy(), machine, preferred, LANE.id);
		if (!result.ok) return result;
		value = result.value;
		if (!writePrivate(ROOT + '/prepared.json', value)) return { ok: false, code: 'runtime_write_failed' };
	}
	else {
		let file = action == 'activate' ? 'prepared' : 'current';
		value = readJson(ROOT + '/' + file + '.json', 65536);
		if (!runtime.matchesBundle(value, entry, machine, LANE.id)) {
			if (action != 'rollback' && action != 'restore') return { ok: false, code: 'runtime_bundle_mismatch' };
			value = readJson(ROOT + '/previous.json', 65536);
			if (!runtime.matchesBundle(value, entry, machine, LANE.id)) return { ok: false, code: 'rollback_bundle_missing' };
		}
		let preferred = null;
		if (action == 'activate') {
			let current = readJson(ROOT + '/current.json', 65536);
			if (state.applied != null && runtime.matchesBundle(current, state.applied, machine, LANE.id))
				preferred = fixedProfile(current);
		}
		let converted = upgrade(value, entry, preferred);
		if (!converted.ok) return converted;
		value = converted.value;
		if (action == 'activate' && !writePrivate(ROOT + '/prepared.json', value))
			return { ok: false, code: 'runtime_write_failed' };
	}
	if (action == 'activate') {
		let current = readJson(ROOT + '/current.json', 65536);
		if (state.applied != null && runtime.matchesBundle(current, state.applied, machine, LANE.id)) {
			if (!writePrivate(ROOT + '/previous.json', current)) return { ok: false, code: 'runtime_write_failed' };
		}
		else if (state.applied != null && !unsupported(state.applied) &&
			!runtime.matchesBundle(readJson(ROOT + '/previous.json', 65536), state.applied, machine, LANE.id))
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
		if (!writePrivate(ROOT + '/zapret.json', value.zapret))
			return { ok: false, code: 'runtime_write_failed' };
	}
	return response(value, { ok: true, active_profile: value.profile, capabilities: value.capabilities });
}

let result;
try { result = run(); } catch (e) { result = { ok: false, code: 'runtime_helper_failed' }; }
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
