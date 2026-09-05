#!/usr/bin/ucode

'use strict';

/*
 * First-run setup deliberately receives the pairing token on stdin.  It must
 * never become a process argument, UCI value, log line, or RPC result.
 */
import { access, chmod, error as fsError, mkdir, readfile, rename, stat, unlink, writefile } from 'fs';
import { cursor } from 'uci';

const ROOT = '/etc/autovpn';
const REQUEST_LIMIT = 4096;
const setupPolicy = require('autovpn.setup-policy');

function validRouterId(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9_-]{8,64}$/) != null;
}

function validCredential(value) {
	if (type(value) != 'string' || length(value) > 256) return false;
	let parts = match(value, /^avrt_([A-Za-z0-9_-]{8,64})\.([A-Za-z0-9_-]{43,128})$/);
	return parts != null;
}

function validBaseUrl(value) {
	/* No userinfo, fragments or whitespace: this is the API origin, not a URL to display. */
	return type(value) == 'string' && length(value) <= 255 &&
		match(value, /^https:\/\/[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(?::[0-9]{1,5})?(?:\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?$/) != null;
}

function validSsid(value) {
	return type(value) == 'string' && length(value) > 0 && length(value) <= 21 &&
		match(value, /[\x00-\x1f\x7f]/) == null;
}

function validWifiKey(value) {
	if (type(value) != 'string') return false;
	return (length(value) >= 8 && length(value) <= 63 && match(value, /^[\x20-\x7e]+$/) != null) ||
		(length(value) == 64 && match(value, /^[0-9A-Fa-f]{64}$/) != null);
}

function validNonce(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9_-]{16,48}$/) != null;
}

function validDevice(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9_.:-]{1,31}$/) != null &&
		access('/sys/class/net/' + value, 'f');
}

function configuredWan(value) {
	/* The RPC facade resolves the actual l3_device and supplies it in the request. */
	return validDevice(value) ? value : null;
}

function credentialPath(ctx) {
	let value = ctx.get('autovpn', 'main', 'credential_file') || '/etc/autovpn/credentials';
	/* The first-run wizard intentionally owns only the package default secret path. */
	return value == '/etc/autovpn/credentials' ? value : null;
}

function uciClean(ctx, name) {
	let changes = ctx.changes(name);
	if (changes == null) return false;
	let sections = keys(changes);
	for (let i = 0; i < length(sections); i++)
		if (length(changes[sections[i]]) > 0) return false;
	return true;
}

function presence(path) {
	let info = stat(path);
	if (info != null) return { present: true, error: false };
	/* ucode fs.stat() returns null for both ENOENT and other errors. Never treat the latter as absent. */
	return fsError() == 'No such file or directory'
		? { present: false, error: false }
		: { present: false, error: true };
}

function firstRunAllowed(ctx, identityMatches, credential) {
	let prepared = ctx.get('autovpn', 'main', 'setup_prepared') == '1';
	let controller = presence(ROOT + '/state/journal.json');
	let networkFile = presence(ROOT + '/networks/journal.json');
	let secret = presence(credential);
	let network = networkFile.present ? readfile(ROOT + '/networks/journal.json', 1048577) : null;
	let networkState;
	let networkInvalid = networkFile.error || secret.error || controller.error || (networkFile.present && network == null);
	try { networkState = network != null ? json(network) : null; } catch (e) { networkState = null; networkInvalid = true; }
	if (networkFile.present && (type(networkState) != 'object' ||
		(networkState.phase != 'pending' && networkState.phase != 'confirmed' && networkState.phase != 'rolled_back')))
		networkInvalid = true;
	return setupPolicy.firstRunAllowed({
		uci_clean: uciClean(ctx, 'autovpn'), enabled: ctx.get('autovpn', 'main', 'enabled') == '1',
		controller_journal: controller.present, network_phase: networkState != null ? networkState.phase : null,
		network_invalid: networkInvalid, prepared: prepared, identity_matches: identityMatches,
		credential_present: secret.present
	});
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

function resultPath(nonce) { return ROOT + '/state/setup-result.' + nonce + '.json'; }

function saveResult(nonce, response) {
	if (!validNonce(nonce)) return false;
	if (!access(ROOT + '/state', 'f') && mkdir(ROOT + '/state') == null) return false;
	chmod(ROOT, 0o700);
	chmod(ROOT + '/state', 0o700);
	return atomic(resultPath(nonce), sprintf('%J\n', response));
}

function configure(nonce) {
	if (!validNonce(nonce)) return { ok: false, code: 'invalid_setup_nonce' };
	let raw = readfile('/dev/stdin', REQUEST_LIMIT + 1);
	if (raw == null || length(raw) > REQUEST_LIMIT) return { ok: false, code: 'setup_request_invalid' };
	let request;
	try { request = json(raw); } catch (e) { return { ok: false, code: 'setup_request_invalid' }; }
	if (type(request) != 'object' || !validBaseUrl(request.base_url) || !validRouterId(request.router_id) ||
		!validCredential(request.credential) || !validSsid(request.base_ssid) || !validWifiKey(request.password))
		return { ok: false, code: 'setup_request_invalid' };
	let wan = configuredWan(request.wan_device);
	if (wan == null) return { ok: false, code: 'wan_not_ready' };
	let ctx = cursor();
	if (!ctx.load('autovpn')) return { ok: false, code: 'config_load_failed' };
	let path = credentialPath(ctx);
	if (path == null) return { ok: false, code: 'credential_path_invalid' };
	let existing = firstRunAllowed(ctx,
		(ctx.get('autovpn', 'main', 'base_url') || '') == request.base_url &&
		(ctx.get('autovpn', 'main', 'router_id') || '') == request.router_id, path);
	if (!existing.ok) return existing;
	let directory = substr(path, 0, rindex(path, '/'));
	if (!access(directory, 'f') && mkdir(directory) == null) return { ok: false, code: 'credential_directory_failed' };
	chmod(directory, 0o700);
	/* Commit non-secret preactivation state first; a failed secret write stays safely disabled and is retryable. */
	if (!ctx.set('autovpn', 'main', 'base_url', request.base_url) ||
		!ctx.set('autovpn', 'main', 'router_id', request.router_id) ||
		!ctx.set('autovpn', 'main', 'enabled', '0') ||
		!ctx.set('autovpn', 'main', 'setup_prepared', '1') ||
		!ctx.set('autovpn', 'runtime', 'wan_device', wan) ||
		!ctx.set('autovpn', 'wifi', 'base_ssid', request.base_ssid) ||
		!ctx.set('autovpn', 'wifi', 'password', request.password) || !ctx.commit('autovpn'))
		return { ok: false, code: 'config_write_failed' };
	if (!atomic(path, request.credential + '\n')) return { ok: false, code: 'credential_write_failed' };
	return { ok: true, configured: true, wan_device: wan };
}

function activate() {
	let network = readfile('/etc/autovpn/networks/journal.json', 1048577);
	let state;
	try { state = network != null ? json(network) : null; } catch (e) { state = null; }
	if (type(state) != 'object' || state.phase != 'confirmed') return { ok: false, code: 'networks_not_confirmed' };
	let ctx = cursor();
	if (!ctx.load('autovpn')) return { ok: false, code: 'config_load_failed' };
	let path = credentialPath(ctx);
	if (path == null) return { ok: false, code: 'setup_incomplete' };
	let existing = firstRunAllowed(ctx, true, path);
	if (!existing.ok || existing.retry !== true) return { ok: false, code: existing.code || 'setup_incomplete' };
	let credential = path != null ? readfile(path, 257) : null;
	if (!validBaseUrl(ctx.get('autovpn', 'main', 'base_url')) || !validRouterId(ctx.get('autovpn', 'main', 'router_id')) ||
		!validCredential(trim(credential || ''))) return { ok: false, code: 'setup_incomplete' };
	if (!ctx.set('autovpn', 'main', 'enabled', '1') ||
		!ctx.set('autovpn', 'main', 'setup_prepared', '0') || !ctx.commit('autovpn')) return { ok: false, code: 'config_write_failed' };
	return { ok: true, enabled: true };
}

let action = ARGV[0];
let nonce = ARGV[1];
let response;
try {
	response = action == 'configure' ? configure(nonce) : action == 'activate' ? activate() : { ok: false, code: 'unknown_setup_action' };
}
catch (e) { response = { ok: false, code: 'setup_failed' }; }

if (action == 'configure') {
	/* The caller reads this private result after closing the stdin pipe. */
	if (!saveResult(nonce, response)) exit(1);
}
else printf('%J\n', response);
exit(response.ok === true ? 0 : 1);
