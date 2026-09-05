'use strict';

const MAX_JOURNAL_BYTES = 262144;
const MAX_SAFE_INTEGER = 9007199254740991;
const PHASES = [
	'IDLE', 'READY', 'PREPARING', 'PREPARED', 'ACTIVATING', 'VERIFYING',
	'ROLLING_BACK', 'FAIL_CLOSED',
];
const CAPABILITY_NAMES = [
	'vless', 'hysteria2', 'amneziawg', 'zapret', 'policy_routing',
];

function isObject(value) {
	return type(value) == 'object';
}

function isInteger(value, minimum, maximum) {
	return type(value) == 'int' && value >= minimum && value <= maximum;
}

function hasExactKeys(value, expected) {
	if (!isObject(value))
		return false;
	let actual = sort(keys(value));
	let wanted = sort(expected);
	if (length(actual) != length(wanted))
		return false;
	for (let i = 0; i < length(wanted); i++)
		if (actual[i] != wanted[i])
			return false;
	return true;
}

function matches(value, expression) {
	return type(value) == 'string' && match(value, expression) != null;
}

function strongEtag(value) {
	return matches(value, /^"[0-9a-f]{64}"$/);
}

function validCapabilities(value) {
	if (!hasExactKeys(value, CAPABILITY_NAMES))
		return false;
	for (let i = 0; i < length(CAPABILITY_NAMES); i++)
		if (type(value[CAPABILITY_NAMES[i]]) != 'bool')
			return false;
	return true;
}

function sameCapabilities(left, right) {
	for (let i = 0; i < length(CAPABILITY_NAMES); i++) {
		let name = CAPABILITY_NAMES[i];
		if (left[name] !== right[name])
			return false;
	}
	return true;
}

function validEntry(value, sequence, snapshotValidator) {
	if (!hasExactKeys(value, ['etag', 'snapshot', 'attempt']) ||
		!strongEtag(value.etag) || !isInteger(value.attempt, 1, sequence))
		return false;
	let validation = snapshotValidator(value.snapshot);
	return isObject(validation) && validation.ok === true;
}

function validErrors(value) {
	if (type(value) != 'array' || length(value) > 64)
		return false;
	for (let i = 0; i < length(value); i++)
		if (!hasExactKeys(value[i], ['path', 'code']) ||
			!matches(value[i].path, /^\$[A-Za-z0-9_.\[\]-]{0,255}$/) ||
			!matches(value[i].code, /^[a-z0-9_]{1,64}$/))
			return false;
	return true;
}

function validRejected(value) {
	return hasExactKeys(value, ['revision', 'etag', 'code', 'errors']) &&
		(value.revision == null || isInteger(value.revision, 0, MAX_SAFE_INTEGER)) &&
		strongEtag(value.etag) && matches(value.code, /^[a-z0-9_]{1,64}$/) &&
		validErrors(value.errors);
}

function validDiagnostics(value, outcome) {
	if (!isObject(value))
		return false;
	if (outcome == 'APPLIED')
		return hasExactKeys(value, ['stage']) && value.stage == 'verified';
	return hasExactKeys(value, ['stage', 'code']) &&
		(value.stage == 'rolled_back' || value.stage == 'rollback_failed') &&
		matches(value.code, /^[a-z0-9_]{1,64}$/);
}

function validReportBody(value) {
	return hasExactKeys(value, [
		'schema_version', 'revision', 'snapshot_sha256', 'outcome',
		'active_profile', 'capabilities', 'diagnostics',
	]) && value.schema_version == 1 &&
		isInteger(value.revision, 0, MAX_SAFE_INTEGER) &&
		matches(value.snapshot_sha256, /^[0-9a-f]{64}$/) &&
		(value.outcome == 'APPLIED' || value.outcome == 'FAILED') &&
		(value.active_profile == null || matches(value.active_profile, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/)) &&
		validCapabilities(value.capabilities) &&
		validDiagnostics(value.diagnostics, value.outcome);
}

function validPending(value) {
	return hasExactKeys(value, ['idempotency_key', 'etag', 'body']) &&
		matches(value.idempotency_key, /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/) &&
		strongEtag(value.etag) && validReportBody(value.body);
}

function reportAttempt(report, sequence) {
	let prefix = 'r' + report.body.revision + '-' +
		substr(report.body.snapshot_sha256, 0, 16) + '-s';
	if (substr(report.idempotency_key, 0, length(prefix)) != prefix)
		return null;
	let suffix = substr(report.idempotency_key, length(prefix));
	if (!matches(suffix, /^[0-9]+$/))
		return null;
	let attempt = int(suffix);
	return isInteger(attempt, 1, sequence) ? attempt : null;
}

function reportMatchesEntry(report, entry) {
	return entry != null && report.etag == entry.etag &&
		report.body.revision == entry.snapshot.revision &&
		report.body.snapshot_sha256 == entry.snapshot.snapshot_sha256 &&
		reportAttempt(report, entry.attempt) == entry.attempt;
}

function validState(state, snapshotValidator) {
	if (!hasExactKeys(state, [
		'journal_version', 'sequence', 'phase', 'desired', 'applied', 'last_good',
		'rejected', 'pending_report', 'capabilities', 'active_profile', 'last_error',
	]) || state.journal_version != 1 ||
		!isInteger(state.sequence, 0, MAX_SAFE_INTEGER) ||
		type(snapshotValidator) != 'function' ||
		!validCapabilities(state.capabilities) ||
		!(state.active_profile == null || matches(state.active_profile, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/)) ||
		!(state.last_error == null || matches(state.last_error, /^[a-z0-9_]{1,64}$/)))
		return false;

	let knownPhase = false;
	for (let i = 0; i < length(PHASES); i++)
		if (state.phase == PHASES[i])
			knownPhase = true;
	if (!knownPhase)
		return false;

	if (state.desired != null && !validEntry(state.desired, state.sequence, snapshotValidator))
		return false;
	if (state.applied != null && !validEntry(state.applied, state.sequence, snapshotValidator))
		return false;
	if (state.last_good != null && !validEntry(state.last_good, state.sequence, snapshotValidator))
		return false;
	if (state.last_good != null && state.applied == null)
		return false;
	if (state.active_profile != null && state.applied == null)
		return false;
	if (state.phase == 'FAIL_CLOSED' && state.active_profile != null)
		return false;
	if (state.rejected != null && !validRejected(state.rejected))
		return false;

	let needsDesired = state.phase != 'IDLE';
	if ((needsDesired && state.desired == null) || (!needsDesired && state.desired != null))
		return false;

	if (state.pending_report != null) {
		if (!validPending(state.pending_report) ||
			reportAttempt(state.pending_report, state.sequence) == null ||
			state.pending_report.body.active_profile != state.active_profile ||
			!sameCapabilities(state.pending_report.body.capabilities, state.capabilities))
			return false;
		if (state.pending_report.body.outcome == 'APPLIED') {
			if (state.phase != 'IDLE' || !reportMatchesEntry(state.pending_report, state.applied))
				return false;
		}
		else if ((state.phase != 'IDLE' && state.phase != 'FAIL_CLOSED') ||
			state.rejected == null || state.pending_report.etag != state.rejected.etag ||
			state.pending_report.body.revision != state.rejected.revision)
			return false;
	}

	return true;
}

function load(storage, fallback, snapshotValidator) {
	let raw = storage.read(MAX_JOURNAL_BYTES + 1);
	if (raw == null)
		return fallback;
	if (type(raw) != 'string' || raw == '' || length(raw) > MAX_JOURNAL_BYTES)
		return null;
	let parsed = storage.decode(raw);
	return validState(parsed, snapshotValidator) ? parsed : null;
}

function store(storage, state) {
	return storage.atomicWrite(storage.encode(state));
}

return {
	MAX_JOURNAL_BYTES: MAX_JOURNAL_BYTES,
	validState: validState,
	load: load,
	store: store,
};
