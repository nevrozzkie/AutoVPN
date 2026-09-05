#!/usr/bin/ucode

'use strict';

import { access, chmod, mkdir, readfile, rename, unlink, writefile, lstat, error as fsError } from 'fs';
import { cursor } from 'uci';

const stateMachine = require('autovpn.state');
const journal = require('autovpn.journal');
const orchestration = require('autovpn.orchestration');
const processRunner = require('autovpn.process');
const MAX_ADAPTER_OUTPUT = 4096;
const MAX_SNAPSHOT_BYTES = 16384;

function configuration() {
	let uci = cursor();
	uci.load('autovpn');
	return {
		base_url: uci.get('autovpn', 'main', 'base_url') || '',
		router_id: uci.get('autovpn', 'main', 'router_id') || '',
		connect_timeout: orchestration.boundedInteger(uci.get('autovpn', 'main', 'connect_timeout'), 10, 1, 60),
		request_timeout: orchestration.boundedInteger(uci.get('autovpn', 'main', 'request_timeout'), 30, 2, 120),
		credential_file: orchestration.safePersistentPath(uci.get('autovpn', 'main', 'credential_file'), '/etc/autovpn/credentials'),
		state_dir: orchestration.safePersistentPath(uci.get('autovpn', 'main', 'state_dir'), '/etc/autovpn/state'),
		http_adapter: uci.get('autovpn', 'main', 'http_adapter') || '/usr/libexec/autovpn/http-adapter',
		runtime_adapter: uci.get('autovpn', 'main', 'runtime_adapter') || '/usr/libexec/autovpn/runtime-adapter',
	};
}

function ensureStateDirectory(path) {
	let parent = substr(path, 0, rindex(path, '/')) || '/etc/autovpn';
	if (!access(parent, 'f') && mkdir(parent) == null)
		return false;
	chmod(parent, 0o700);
	if (!access(path, 'f') && mkdir(path) == null)
		return false;
	chmod(path, 0o700);
	return true;
}

function storageFor(path) {
	return {
		read: function(limit) {
			return access(path, 'f') ? readfile(path, limit) : null;
		},
		decode: function(raw) {
			try {
				return json(raw);
			}
			catch (e) {
				return null;
			}
		},
		encode: function(value) {
			return sprintf('%J\n', value);
		},
		atomicWrite: function(raw) {
			let temporary = path + '.new';
			unlink(temporary);
			if (writefile(temporary, raw) == null)
				return false;
			if (chmod(temporary, 0o600) == null) {
				unlink(temporary);
				return false;
			}
			if (rename(temporary, path) == null) {
				unlink(temporary);
				return false;
			}
			chmod(path, 0o600);
			return true;
		},
	};
}

function loadState(config) {
	if (!ensureStateDirectory(config.state_dir))
		return null;
	let path = config.state_dir + '/journal.json';
	if (access(path, 'f') && chmod(path, 0o600) == null)
		return null;
	return journal.load(storageFor(path), stateMachine.initialState(), stateMachine.validateSnapshot);
}

function saveState(config, state) {
	return journal.store(storageFor(config.state_dir + '/journal.json'), state);
}

function adapterCall(argv) {
	let process = processRunner.popen(argv, 'r');
	if (process == null)
		return { ok: false, code: 'adapter_start_failed' };
	let raw = process.read(MAX_ADAPTER_OUTPUT + 1) || '';
	let status = process.close();
	if (length(raw) > MAX_ADAPTER_OUTPUT)
		return { ok: false, code: 'adapter_output_too_large' };
	let response;
	try {
		response = json(raw);
	}
	catch (e) {
		return { ok: false, code: 'adapter_invalid_json' };
	}
	if (type(response) != 'object')
		return { ok: false, code: 'adapter_invalid_response' };
	if (status != 0 || response.ok !== true)
		return {
			ok: false,
			code: type(response.code) == 'string' && match(response.code, /^[a-z0-9_]{1,64}$/) != null
				? response.code
				: 'adapter_failed',
		};
	return response;
}

function consumeSnapshot(config, response) {
	if (!orchestration.validResponsePath(config.state_dir, response.response_file))
		return { ok: false, code: 'invalid_response_file' };
	let raw = readfile(response.response_file, MAX_SNAPSHOT_BYTES + 1);
	unlink(response.response_file);
	if (raw == null)
		return { ok: false, code: 'snapshot_read_failed' };
	if (length(raw) > MAX_SNAPSHOT_BYTES)
		return { ok: false, code: 'snapshot_too_large' };
	let snapshot;
	try {
		snapshot = json(raw);
	}
	catch (e) {
		return { ok: false, code: 'snapshot_invalid_json' };
	}
	return type(snapshot) == 'object'
		? { ok: true, body: snapshot }
		: { ok: false, code: 'snapshot_invalid_json' };
}

function safeCapabilities(value) {
	return stateMachine.normalizeCapabilities(type(value) == 'object' ? value : {});
}

function safeActiveProfile(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/) != null
		? value
		: null;
}

function runtimeCapabilities(config) {
	let response = adapterCall([config.runtime_adapter, 'capabilities']);
	return response.ok ? safeCapabilities(response.capabilities) : safeCapabilities({});
}

function persist(config, transition) {
	if (!transition.ok && transition.code == 'invalid_phase')
		return transition;
	if (!saveState(config, transition.state))
		return { ok: false, code: 'journal_write_failed', state: transition.state };
	return transition;
}

function runtimeAction(config, action) {
	let argv = [config.runtime_adapter, action];
	if (action != 'fail-closed')
		push(argv, config.state_dir + '/journal.json');
	return adapterCall(argv);
}

function operationsFor(config) {
	return {
		persist: function(transition) {
			return persist(config, transition);
		},
		runtime: function(action) {
			return runtimeAction(config, action);
		},
		capabilities: function() {
			return runtimeCapabilities(config);
		},
		activeProfile: function(value) {
			return safeActiveProfile(value);
		},
	};
}

function reportPending(config, state) {
	if (state.pending_report == null)
		return { ok: true, noop: true, state: state };
	let response = adapterCall(orchestration.putResultArguments(
		config,
		state.pending_report,
		config.state_dir + '/journal.json'
	));
	if (!response.ok)
		return { ok: false, code: response.code, state: state };
	return persist(config, stateMachine.reportAccepted(state, state.pending_report.idempotency_key));
}

function rollback(config, state, failureCode) {
	return orchestration.rollbackDesired(state, stateMachine, operationsFor(config), failureCode);
}

function applyDesired(config, state) {
	return orchestration.applyDesired(state, stateMachine, operationsFor(config));
}

function recover(config, state) {
	if (!stateMachine.recoverRequired(state))
		return { ok: true, noop: true, state: state };
	return rollback(config, state, 'interrupted_apply');
}

function restoreRuntime(config, state) {
	let response = runtimeAction(config, 'restore');
	if (!response.ok)
		runtimeAction(config, 'fail-closed');
	return { ok: response.ok === true, code: response.code || 'runtime_restored', state: state };
}

function applyPolicy(config, state) {
	let recovered = recover(config, state);
	if (!recovered.ok) return recovered;
	state = recovered.state;
	let reported = reportPending(config, state);
	if (!reported.ok) return reported;
	if (state.applied == null) return { ok: false, code: 'snapshot_not_applied', state: state };
	let accepted = stateMachine.receiveSnapshot(state, state.applied.snapshot, state.applied.etag, true);
	if (!accepted.ok || !saveState(config, state))
		return { ok: false, code: accepted.code || 'journal_write_failed', state: state };
	let applied = applyDesired(config, state);
	if (!applied.ok && applied.outcome != 'ROLLED_BACK' && applied.outcome != 'FAIL_CLOSED') return applied;
	return orchestration.finalizeRefresh(applied, reportPending(config, applied.state));
}

function refresh(config, state) {
	let recovered = recover(config, state);
	if (!recovered.ok)
		return recovered;
	state = recovered.state;
	/* Restore the committed runtime before network I/O, also when the server is offline. */
	let restored = restoreRuntime(config, state);

	let reported = reportPending(config, state);
	if (!reported.ok)
		return reported;
	state = reported.state;
	if (!config.base_url || !config.router_id || !access(config.credential_file, 'r'))
		return { ok: false, code: 'not_configured', state: state };
	if (chmod(config.credential_file, 0o600) == null)
		return { ok: false, code: 'credential_permissions_failed', state: state };
	if (match(config.base_url, /^https:\/\/[^\/?#]+/) == null)
		return { ok: false, code: 'invalid_base_url', state: state };
	if (match(config.router_id, /^[A-Za-z0-9_-]{8,64}$/) == null)
		return { ok: false, code: 'invalid_router_id', state: state };

	let etag = state.applied == null ? '' : state.applied.etag;
	let response = adapterCall(orchestration.fetchArguments(config, etag));
	if (!response.ok)
		return { ok: false, code: response.code, state: state };
	if (response.status == 304)
		return restored.ok ? stateMachine.fetchedNotModified(state) : restored;
	if (response.status != 200)
		return { ok: false, code: 'unexpected_snapshot_response', state: state };
	let snapshot = consumeSnapshot(config, response);
	if (!snapshot.ok)
		return { ok: false, code: snapshot.code, state: state };
	if (snapshot.body.router_id != config.router_id)
		return { ok: false, code: 'router_identity_mismatch', state: state };

	let accepted = stateMachine.receiveSnapshot(state, snapshot.body, response.etag);
	if (!accepted.ok) {
		if (accepted.code == 'snapshot_validation_failed')
			saveState(config, state);
		return accepted;
	}
	if (accepted.noop)
		return restored.ok ? accepted : restored;
	if (!saveState(config, state))
		return { ok: false, code: 'journal_write_failed', state: state };

	let applied = applyDesired(config, state);
	state = applied.state;
	if (!applied.ok && applied.outcome != 'ROLLED_BACK' && applied.outcome != 'FAIL_CLOSED')
		return applied;
	let reportedAfterApply = reportPending(config, state);
	return orchestration.finalizeRefresh(applied, reportedAfterApply);
}

function summary(config, state) {
	let runtime = runtimeAction(config, 'status');
	function entry(value) {
		return value == null ? null : {
			revision: value.snapshot.revision,
			snapshot_sha256: value.snapshot.snapshot_sha256,
			etag: value.etag,
		};
	}
	return {
		ok: true,
		runtime_running: runtime.ok === true,
		runtime_error: runtime.ok === true ? null : runtime.code,
		runtime_profile: runtime.ok === true ? safeActiveProfile(runtime.active_profile) : null,
		runtime_zapret: runtime.ok === true && runtime.capabilities?.zapret === true,
		router_id: config.router_id,
		phase: state.phase,
		sequence: state.sequence,
		credential_configured: access(config.credential_file, 'r') === true,
		desired: entry(state.desired),
		applied: entry(state.applied),
		last_good: entry(state.last_good),
		rejected: state.rejected == null ? null : {
			revision: state.rejected.revision,
			code: state.rejected.code,
			errors: state.rejected.errors,
		},
		pending_report: state.pending_report == null ? null : {
			idempotency_key: state.pending_report.idempotency_key,
			outcome: state.pending_report.body.outcome,
		},
		capabilities: safeCapabilities(state.capabilities),
		active_profile: state.active_profile,
		last_error: state.last_error,
	};
}

function maintenanceBlocked() {
	for (let path in ['/etc/autovpn/state/maintenance.lock', '/etc/autovpn/state/update.lock']) {
		if (lstat(path) != null || fsError() != 'No such file or directory') return true;
	}
	return false;
}

let config = configuration();
let command = ARGV[0] || 'status';
/* Never read/recover an old journal while maintenance owns the runtime. */
if (maintenanceBlocked()) {
	let result = command == 'stop' ? runtimeAction(config, 'fail-closed') : {
		ok: command == 'status', code: 'maintenance_locked', phase: 'MAINTENANCE',
		runtime_running: false, runtime_error: 'maintenance_locked', router_id: config.router_id,
		credential_configured: access(config.credential_file, 'r') === true,
	};
	printf('%J\n', result);
	exit(result.ok === true ? 0 : 1);
}
let state = loadState(config);
let journalGuard = orchestration.guardJournal(state, operationsFor(config));
if (!journalGuard.ok) {
	printf('%J\n', journalGuard);
	exit(65);
}

let result;
if (command == 'status')
	result = summary(config, state);
else if (command == 'recover')
	result = recover(config, state);
else if (command == 'apply-policy')
	result = applyPolicy(config, state);
else if (command == 'stop')
	result = runtimeAction(config, 'fail-closed');
else if (command == 'refresh')
	result = refresh(config, state);
else if (command == 'health-tick' || command == 'ping-all') {
	if (state.phase != 'IDLE' || state.applied == null)
		result = { ok: false, code: 'runtime_not_ready' };
	else if (command == 'ping-all') {
		let target = ARGV[1] || 'youtube';
		result = index(['youtube', 'instagram'], target) < 0 ? { ok: false, code: 'invalid_probe_target' } :
			adapterCall([config.runtime_adapter, 'ping-all', config.state_dir + '/journal.json', target]);
	}
	else {
		result = runtimeAction(config, 'health-tick');
		if (result.ok && safeActiveProfile(result.active_profile) != state.active_profile) {
			state.active_profile = safeActiveProfile(result.active_profile);
			/* Apply reports are immutable; failover updates local status only. */
			if (!saveState(config, state)) result = { ok: false, code: 'journal_write_failed' };
		}
	}
}
else
	result = { ok: false, code: 'unknown_command' };

if (command == 'status' || command == 'ping-all')
	printf('%J\n', result);
else
	printf('%J\n', {
		ok: result.ok === true,
		code: result.code || null,
		outcome: result.outcome || null,
		failure_code: result.failure_code || null,
		report_sent: result.report_sent === true,
		phase: result.state == null ? state.phase : result.state.phase,
	});
exit(result.ok === true ? 0 : 1);
