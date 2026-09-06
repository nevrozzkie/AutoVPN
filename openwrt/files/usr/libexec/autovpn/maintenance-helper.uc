#!/usr/bin/ucode

'use strict';

/*
 * Destructive maintenance is deliberately a small, closed-world operation.
 * It only ever owns the package's default secret/state locations; accepting a
 * path from UCI here would turn a LuCI maintenance button into an arbitrary
 * root-file remover.
 */
import { access, chmod, error as fsError, lstat, mkdir, readfile, rename, stat, unlink, writefile } from 'fs';
import { cursor } from 'uci';
const processRunner = require('autovpn.process');

const ROOT = '/etc/autovpn';
const STATE = ROOT + '/state';
const CREDENTIAL = ROOT + '/credentials';
const RUNTIME = ROOT + '/runtime';
const RUNTIME_ZAPRET = ROOT + '/runtime-zapret';
const REQUEST_LIMIT = 4096;

function validNonce(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9_-]{16,48}$/) != null;
}

function validRouterId(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9_-]{8,64}$/) != null;
}

function validCredential(value) {
	return type(value) == 'string' && length(value) <= 256 &&
		match(value, /^avrt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{43,128}$/) != null;
}

function validBaseUrl(value) {
	return type(value) == 'string' && length(value) <= 255 &&
		match(value, /^https:\/\/[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?(\/[A-Za-z0-9._~!$&'()*+,;=:@%\/-]*)?$/) != null;
}

function presence(path) {
	let info = lstat(path);
	if (info != null) return { present: true, error: false };
	return fsError() == 'No such file or directory'
		? { present: false, error: false }
		: { present: false, error: true };
}

function atomic(path, value) {
	let temporary = path + '.new';
	unlink(temporary);
	if (writefile(temporary, value) != length(value) || chmod(temporary, 0o600) == null || rename(temporary, path) == null) {
		unlink(temporary);
		return false;
	}
	return chmod(path, 0o600) != null;
}

function uciClean(ctx, name) {
	let changes = ctx.changes(name);
	if (changes == null) return false;
	let sections = keys(changes);
	for (let i = 0; i < length(sections); i++)
		if (length(changes[sections[i]]) > 0) return false;
	return true;
}

function removeOwned(path) {
	let exists = presence(path);
	if (exists.error) return false;
	return !exists.present || unlink(path) != null;
}

function privateDirectories() {
	if (!access(ROOT, 'f') && mkdir(ROOT) == null) return false;
	if (!access(STATE, 'f') && mkdir(STATE) == null) return false;
	if (!access(RUNTIME, 'f') && mkdir(RUNTIME) == null) return false;
	if (!access(RUNTIME_ZAPRET, 'f') && mkdir(RUNTIME_ZAPRET) == null) return false;
	return chmod(ROOT, 0o700) != null && chmod(STATE, 0o700) != null &&
		chmod(RUNTIME, 0o700) != null && chmod(RUNTIME_ZAPRET, 0o700) != null;
}

function resultPath(nonce) { return STATE + '/maintenance-result.' + nonce + '.json'; }

function saveResult(nonce, response) {
	return validNonce(nonce) && privateDirectories() && atomic(resultPath(nonce), sprintf('%J\n', response));
}

function safeConfig(ctx) {
	if (!ctx.load('autovpn')) return null;
	/* Do not make a destructive action work through redirected paths/adapters. */
	if ((ctx.get('autovpn', 'main', 'credential_file') || CREDENTIAL) != CREDENTIAL ||
		(ctx.get('autovpn', 'main', 'state_dir') || STATE) != STATE ||
		(ctx.get('autovpn', 'main', 'runtime_adapter') || '/usr/libexec/autovpn/runtime-adapter') != '/usr/libexec/autovpn/runtime-adapter' ||
		(ctx.get('autovpn', 'main', 'http_adapter') || '/usr/libexec/autovpn/http-adapter') != '/usr/libexec/autovpn/http-adapter' ||
		!uciClean(ctx, 'autovpn'))
		return null;
	return {
		base_url: ctx.get('autovpn', 'main', 'base_url') || '',
		router_id: ctx.get('autovpn', 'main', 'router_id') || '',
		enabled: ctx.get('autovpn', 'main', 'enabled') == '1',
	};
}

function confirmedNetworks() {
	let file = lstat(ROOT + '/networks/journal.json');
	if (file == null || file.type != 'file') return false;
	let raw = readfile(ROOT + '/networks/journal.json', 1048577);
	let journal;
	try { journal = raw != null ? json(raw) : null; } catch (e) { journal = null; }
	if (type(journal) != 'object' || journal.phase != 'confirmed') return false;
	/* Reuse the authoritative network transaction validator before destructive
	 * maintenance; the phase string alone is not enough. */
	let process = processRunner.popen(['/usr/libexec/autovpn/network-helper.uc', 'network-gate'], 'r');
	if (process == null) return false;
	let output = process.read(4097) || '';
	let status = process.close();
	let response;
	try { response = json(output); } catch (e) { response = null; }
	return status == 0 && type(response) == 'object' && response.ok === true;
}

function closeRuntime() {
	let process = processRunner.popen(['/usr/libexec/autovpn/runtime-adapter', 'fail-closed'], 'r');
	if (process == null) return false;
	let raw = process.read(4097) || '';
	let status = process.close();
	let response;
	try { response = json(raw); } catch (e) { response = null; }
	return status == 0 && type(response) == 'object' && response.ok === true;
}

function writeLock(action, phase) {
	if (!atomic(STATE + '/maintenance.lock', sprintf('%J\n', {
		schema_version: 1, action: action, phase: phase
	}))) return false;
	return syncFilesystem();
}

function syncFilesystem() {
	let process = processRunner.popen(['/bin/sync'], 'r');
	if (process == null) return false;
	process.read(1);
	return process.close() == 0;
}

function persistDisabled(ctx) {
	return ctx.set('autovpn', 'main', 'enabled', '0') && ctx.commit('autovpn') && syncFilesystem();
}

function clearRuntimeState() {
	/* Enumerated files only.  Never recursively remove /etc/autovpn. */
	let files = [
		STATE + '/journal.json',
		RUNTIME + '/prepared.json', RUNTIME + '/current.json', RUNTIME + '/previous.json',
		RUNTIME + '/failover.json',
		RUNTIME + '/candidate.json', RUNTIME + '/run.json', RUNTIME + '/awg.json', RUNTIME + '/zapret.json',
		RUNTIME + '/awg-run.json', RUNTIME + '/awg-owned', RUNTIME + '/awg.conf',
		RUNTIME_ZAPRET + '/prepared.json', RUNTIME_ZAPRET + '/current.json', RUNTIME_ZAPRET + '/previous.json',
		RUNTIME_ZAPRET + '/failover.json',
		RUNTIME_ZAPRET + '/candidate.json', RUNTIME_ZAPRET + '/run.json', RUNTIME_ZAPRET + '/awg.json', RUNTIME_ZAPRET + '/zapret.json',
		RUNTIME_ZAPRET + '/awg-run.json', RUNTIME_ZAPRET + '/awg-owned', RUNTIME_ZAPRET + '/awg.conf'
	];
	for (let i = 0; i < length(files); i++)
		if (!removeOwned(files[i]) || !removeOwned(files[i] + '.new')) return false;
	return removeOwned(CREDENTIAL + '.new');
}

function readRequest() {
	let raw = readfile('/dev/stdin', REQUEST_LIMIT + 1);
	if (raw == null || length(raw) > REQUEST_LIMIT) return null;
	try { return json(raw); } catch (e) { return null; }
}

function updateBinding(ctx, request) {
	if (!ctx.set('autovpn', 'main', 'base_url', request.base_url) ||
		!ctx.set('autovpn', 'main', 'router_id', request.router_id) ||
		!ctx.set('autovpn', 'main', 'enabled', '0') ||
		!ctx.set('autovpn', 'main', 'setup_prepared', '1') || !ctx.commit('autovpn'))
		return false;
	return atomic(CREDENTIAL, request.credential + '\n');
}

function resetConfig(ctx) {
	/* WAN and Wi-Fi sections are intentionally untouched. */
	return ctx.set('autovpn', 'main', 'base_url', '') &&
		ctx.set('autovpn', 'main', 'router_id', '') &&
		ctx.set('autovpn', 'main', 'enabled', '0') &&
		ctx.set('autovpn', 'main', 'setup_prepared', '0') &&
		ctx.set('autovpn', 'main', 'poll_interval', '300') &&
		ctx.set('autovpn', 'main', 'connect_timeout', '10') &&
		ctx.set('autovpn', 'main', 'request_timeout', '30') &&
		ctx.set('autovpn', 'runtime', 'selection', 'auto') &&
		ctx.set('autovpn', 'runtime_zapret', 'selection', 'auto') &&
		ctx.set('autovpn', 'runtime', 'dns_server', '1.1.1.1') &&
		ctx.set('autovpn', 'runtime', 'ru_bypass', '1') &&
		ctx.set('autovpn', 'runtime', 'direct_domains', ['ru', 'xn--p1ai']) &&
		(ctx.get('autovpn', 'runtime', 'direct_cidrs') == null || ctx.delete('autovpn', 'runtime', 'direct_cidrs')) &&
		ctx.set('autovpn', 'runtime', 'hysteria_tls_mode', 'subscription') &&
		ctx.set('autovpn', 'runtime', 'zapret_enabled', '0') &&
		ctx.set('autovpn', 'runtime', 'zapret_vless', 'split') &&
		ctx.set('autovpn', 'runtime', 'zapret_hysteria2', 'fake') &&
		ctx.set('autovpn', 'runtime', 'zapret_amneziawg', 'off') &&
		ctx.set('autovpn', 'runtime', 'zapret_repeats', '2') &&
		ctx.set('autovpn', 'health', 'probe_url', 'https://connectivitycheck.gstatic.com/generate_204') &&
		ctx.set('autovpn', 'health', 'failure_threshold', '3') &&
		ctx.set('autovpn', 'health', 'success_threshold', '2') &&
		ctx.set('autovpn', 'health', 'cooldown_seconds', '60') &&
		ctx.set('autovpn', 'health', 'manual_override', 'auto') &&
		ctx.commit('autovpn');
}

function perform(request) {
	if (type(request) != 'object' || (request.action != 'rotate' && request.action != 'rebind' && request.action != 'reset'))
		return { ok: false, code: 'maintenance_request_invalid' };
	let ctx = cursor();
	let config = safeConfig(ctx);
	if (config == null) return { ok: false, code: 'maintenance_path_invalid' };
	if (!privateDirectories()) return { ok: false, code: 'maintenance_storage_failed' };
	let update = presence(STATE + '/update.lock');
	if (update.error) return { ok: false, code: 'update_gate_invalid' };
	if (update.present) return { ok: false, code: 'update_in_progress' };
	let lock = presence(STATE + '/maintenance.lock');
	if (lock.error) return { ok: false, code: 'maintenance_lock_invalid' };
	/* Rotate has no recovery semantics. An explicitly reconfirmed reset/rebind
	 * may safely retry its own interrupted operation without SSH. */
	if (lock.present && request.action == 'rotate') return { ok: false, code: 'maintenance_pending' };
	if (request.action == 'rotate') {
		if (!validCredential(request.credential) || !validBaseUrl(config.base_url) || !validRouterId(config.router_id))
			return { ok: false, code: 'rotation_invalid' };
		if (!atomic(CREDENTIAL, request.credential + '\n') || !syncFilesystem()) return { ok: false, code: 'credential_write_failed' };
		/* The controller lock excludes a concurrent refresh. Keep the active
		 * tunnel running; it will use the atomically replaced token next poll. */
		return { ok: true, rotated: true, requires_activation: false };
	}
	if (!confirmedNetworks()) return { ok: false, code: 'networks_not_confirmed' };
	if (request.action == 'rebind') {
		if (request.confirmation != 'REBIND' || !validBaseUrl(request.base_url) || !validRouterId(request.router_id) || !validCredential(request.credential))
			return { ok: false, code: 'rebind_invalid' };
		if (!writeLock('rebind', 'running') || !persistDisabled(ctx) || !closeRuntime()) return { ok: false, code: 'fail_closed_unconfirmed' };
		/* Erase old server state before writing a new identity: no later restore may revive it. */
		if (!clearRuntimeState()) { writeLock('rebind', 'failed'); return { ok: false, code: 'state_clear_failed' }; }
		if (!updateBinding(ctx, request)) { writeLock('rebind', 'failed'); return { ok: false, code: 'binding_write_failed' }; }
		if (!writeLock('rebind', 'ready')) return { ok: false, code: 'maintenance_lock_failed' };
		return { ok: true, rebound: true, requires_activation: true };
	}
	if (request.confirmation != 'RESET') return { ok: false, code: 'reset_confirmation_required' };
	if (!writeLock('reset', 'running') || !persistDisabled(ctx) || !closeRuntime()) return { ok: false, code: 'fail_closed_unconfirmed' };
	if (!clearRuntimeState() || !removeOwned(CREDENTIAL)) { writeLock('reset', 'failed'); return { ok: false, code: 'state_clear_failed' }; }
	if (!resetConfig(ctx)) { writeLock('reset', 'failed'); return { ok: false, code: 'config_write_failed' }; }
	if (!writeLock('reset', 'ready')) return { ok: false, code: 'maintenance_lock_failed' };
	return { ok: true, reset: true, requires_activation: false };
}

let nonce = ARGV[0];
let response;
try {
	response = validNonce(nonce) ? perform(readRequest()) : { ok: false, code: 'invalid_maintenance_nonce' };
} catch (e) {
	response = { ok: false, code: 'maintenance_failed' };
}
if (!saveResult(nonce, response)) exit(1);
exit(response.ok === true ? 0 : 1);
