#!/usr/bin/ucode
'use strict';

import { readfile, writefile, rename, chmod, mkdir, lstat } from 'fs';

const runner = require('autovpn.process');
const probes = require('autovpn.probes');
const HEALTH_DIR = '/var/run/autovpn-health';

function call(argv, limit) {
	let child = runner.popen(argv, 'r');
	if (child == null) return { raw: '', status: -1 };
	let raw = child.read(limit + 1) || '';
	let status = child.close();
	return { raw: length(raw) <= limit ? raw : '', status: status };
}
function probe(profile, target) {
	let response = call(probes.argumentsFor(profile, target), 80);
	return probes.parseResult(profile, target, response.raw, response.status);
}
function healthy(profile) {
	/* One site's outage is not enough to call the tunnel dead. */
	return probe(profile, 'health_google').status == 'ok' ||
		probe(profile, 'health_cloudflare').status == 'ok';
}
function readHealth() {
	let raw = readfile(HEALTH_DIR + '/state.json', 2049);
	try { return raw != null && length(raw) <= 2048 ? json(raw) : null; }
	catch (e) { return null; }
}
function saveHealth(value) {
	let directory = lstat(HEALTH_DIR);
	if (directory == null) {
		if (mkdir(HEALTH_DIR, 0o700) == null) return false;
	}
	else if (directory.type != 'directory' || directory.uid != 0) return false;
	if (chmod(HEALTH_DIR, 0o700) == null) return false;
	/* Directory is root-owned/private; state is volatile, never written to flash. */
	let raw = sprintf('%J', value);
	return writefile(HEALTH_DIR + '/state.new', raw) == length(raw) &&
		chmod(HEALTH_DIR + '/state.new', 0o600) != null &&
		rename(HEALTH_DIR + '/state.new', HEALTH_DIR + '/state.json') != null;
}
function run() {
	let action = ARGV[0];
	let target = ARGV[2] || 'youtube';
	if (index(['ping-all', 'health', 'recover', 'confirm', 'bootstrap'], action) < 0 ||
		(action == 'ping-all' && index(['youtube', 'instagram'], target) < 0))
		return { ok: false, code: 'invalid_probe_action' };
	let response = call(['/usr/bin/ucode', '/usr/libexec/autovpn/runtime-helper.uc',
		(action == 'confirm' || action == 'bootstrap') ? 'probe-info' : 'probe-info-live', ARGV[1]], 4096);
	let info;
	try { info = json(response.raw); } catch (e) { return { ok: false, code: 'probe_runtime_invalid' }; }
	if (response.status != 0 || info.ok !== true) return { ok: false, code: 'probe_runtime_unavailable' };
	if (type(info.candidates) != 'array' || length(info.candidates) > 3 ||
		index(info.candidates, info.active_profile) < 0 || type(info.identity) != 'string' || length(info.identity) > 512)
		return { ok: false, code: 'probe_runtime_invalid' };
	for (let candidate in info.candidates)
		if (index(probes.profiles, candidate) < 0) return { ok: false, code: 'probe_runtime_invalid' };
	if (action == 'confirm') {
		if (index(info.candidates, target) < 0 || info.selection != 'auto')
			return { ok: false, code: 'invalid_probe_profile' };
		return { ok: true, healthy: healthy(target) && healthy(target) };
	}
	if (action == 'bootstrap') {
		if (info.selection != 'auto') return { ok: false, code: 'selection_not_auto' };
		for (let candidate in info.candidates)
			if (candidate != 'amneziawg' && healthy(candidate) && healthy(candidate))
				return { ok: true, next_profile: candidate };
		return { ok: false, code: 'no_working_vpn' };
	}
	if (action == 'ping-all') {
		let results = [];
		for (let profile in probes.profiles)
			push(results, index(info.candidates, profile) >= 0 ? probe(profile, target) : {
				profile: profile, status: 'unavailable', latency_ms: null, http_status: null, code: 'not_configured_or_supported',
			});
		/* Diagnostics do not write health state or select/restart any tunnel. */
		return { ok: true, target: target, checked_at: time(), active_profile: info.active_profile, results: results };
	}
	let success = healthy(info.active_profile);
	if (success && action == 'recover') success = healthy(info.active_profile);
	let state = probes.observe(readHealth(), info.identity, success);
	if (!saveHealth(state)) return { ok: false, code: 'health_state_write_failed' };
	let next = null;
	if (!success && info.selection == 'auto' && (action == 'recover' || state.failures >= 3)) {
		for (let candidate in info.candidates) {
			if (candidate != info.active_profile && healthy(candidate) && healthy(candidate)) {
				next = candidate;
				break;
			}
		}
	}
	return { ok: true, active_profile: info.active_profile, next_profile: next,
		healthy: success, failed: !success && (action == 'recover' || state.failures >= 3), failures: state.failures };
}

let result;
try { result = run(); } catch (e) { result = { ok: false, code: 'probe_failed' }; }
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
