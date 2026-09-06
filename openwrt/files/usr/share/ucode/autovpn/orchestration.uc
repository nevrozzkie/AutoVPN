'use strict';

function boundedInteger(value, fallback, minimum, maximum) {
	let parsed;
	if (type(value) == 'int')
		parsed = value;
	else if (type(value) == 'string' && match(value, /^[0-9]+$/) != null)
		parsed = int(value);
	else
		return fallback;
	return parsed >= minimum && parsed <= maximum ? parsed : fallback;
}

function validEtag(value) {
	return type(value) == 'string' && match(value, /^"[0-9a-f]{64}"$/) != null
		? value
		: '';
}

function safePersistentPath(value, fallback) {
	return type(value) == 'string' && match(value, /^\/etc\/autovpn\/[A-Za-z0-9_.-]+$/) != null
		? value
		: fallback;
}

function fetchArguments(config, appliedEtag) {
	return [
		config.http_adapter,
		'fetch',
		config.base_url,
		config.credential_file,
		config.state_dir,
		validEtag(appliedEtag),
		sprintf('%d', config.connect_timeout),
		sprintf('%d', config.request_timeout),
	];
}

function validResponsePath(stateDir, value) {
	if (type(stateDir) != 'string' || type(value) != 'string')
		return false;
	let prefix = stateDir + '/http-response.';
	return substr(value, 0, length(prefix)) == prefix &&
		match(substr(value, length(prefix)), /^[0-9]+$/) != null;
}

function putResultArguments(config, pending, journalPath) {
	return [
		config.http_adapter,
		'put-result',
		config.base_url,
		config.credential_file,
		pending.idempotency_key,
		pending.etag,
		journalPath,
		sprintf('%d', config.connect_timeout),
		sprintf('%d', config.request_timeout),
	];
}

function finalizeRefresh(applyResult, reportResult) {
	if (applyResult.outcome == 'ROLLED_BACK')
		return {
			ok: false,
			code: 'rolled_back',
			outcome: 'ROLLED_BACK',
			failure_code: applyResult.code || 'apply_failed',
			report_sent: reportResult.ok === true,
			report_error: reportResult.ok === true ? null : reportResult.code,
			state: applyResult.state,
		};
	if (applyResult.outcome == 'APPLIED') {
		if (reportResult.ok === true)
			return {
				ok: true,
				code: 'applied',
				outcome: 'APPLIED',
				report_sent: true,
				state: applyResult.state,
			};
		return {
			ok: false,
			code: 'apply_result_pending',
			outcome: 'APPLIED',
			report_sent: false,
			report_error: reportResult.code,
			state: applyResult.state,
		};
	}
	return applyResult;
}

function cloneValue(value) {
	return json(sprintf('%J', value));
}

function guardJournal(state, operations) {
	if (state != null)
		return { ok: true, state: state };
	let closed = operations.runtime('fail-closed');
	return {
		ok: false,
		code: 'journal_invalid',
		fail_closed_confirmed: closed.ok === true,
	};
}

function rollbackDesired(state, machine, operations, failureCode) {
	let working = cloneValue(state);
	let failed = machine.applyFailed(working, failureCode);
	if (!failed.ok && failed.code == 'invalid_phase')
		return failed;

	/*
	 * This write is best effort.  Rollback always consumes the durable journal,
	 * so a failed write deliberately leaves the preceding recoverable phase on
	 * disk instead of making the in-memory transition authoritative.
	 */
	operations.persist(failed);
	let rollback = operations.runtime('rollback');
	if (rollback.ok === true) {
		if (rollback.active_profile != null)
			working.active_profile = operations.activeProfile(rollback.active_profile);
		let rolledBack = machine.rolledBack(
			working,
			true,
			rollback.capabilities || operations.capabilities()
		);
		rolledBack.outcome = 'ROLLED_BACK';
		rolledBack.failure_code = failureCode;
		let stored = operations.persist(rolledBack);
		if (!stored.ok) {
			return {
				ok: false,
				code: stored.code,
				outcome: 'ROLLED_BACK',
				failure_code: failureCode,
				rollback_confirmed: true,
				state: rolledBack.state,
			};
		}
		return rolledBack;
	}

	/* This action has no journal argument and must be safe with corrupt storage. */
	let failClosed = operations.runtime('fail-closed');
	let closed = machine.rolledBack(working, false, {});
	closed.outcome = 'FAIL_CLOSED';
	closed.failure_code = failureCode;
	closed.fail_closed_confirmed = failClosed.ok === true;
	operations.persist(closed);
	if (failClosed.ok !== true)
		closed.code = 'fail_closed_unconfirmed';
	return closed;
}

function applyDesired(state, machine, operations) {
	let working = cloneValue(state);
	let transition = operations.persist(machine.beginApply(working));
	if (!transition.ok)
		return transition;

	let response = operations.runtime('prepare');
	if (!response.ok)
		return rollbackDesired(working, machine, operations, response.code);

	/* Capture the last durable phase before mutating the next candidate. */
	let checkpoint = cloneValue(working);
	transition = operations.persist(machine.prepared(working));
	if (!transition.ok)
		return rollbackDesired(checkpoint, machine, operations, transition.code);

	checkpoint = cloneValue(working);
	transition = operations.persist(machine.activating(working));
	if (!transition.ok)
		return rollbackDesired(checkpoint, machine, operations, transition.code);

	response = operations.runtime('activate');
	if (!response.ok)
		return rollbackDesired(working, machine, operations, response.code);

	checkpoint = cloneValue(working);
	transition = operations.persist(machine.activated(working));
	if (!transition.ok)
		return rollbackDesired(checkpoint, machine, operations, transition.code);

	response = operations.runtime('verify');
	if (!response.ok)
		return rollbackDesired(working, machine, operations, response.code);

	/* verified() promotes desired to applied; keep VERIFYING intact until commit. */
	checkpoint = cloneValue(working);
	let candidate = cloneValue(working);
	let committed = operations.persist(machine.verified(candidate, {
		active_profile: operations.activeProfile(response.active_profile),
		capabilities: response.capabilities || operations.capabilities(),
	}));
	if (!committed.ok)
		return rollbackDesired(checkpoint, machine, operations, committed.code);
	committed.outcome = 'APPLIED';
	return committed;
}

return {
	boundedInteger: boundedInteger,
	validEtag: validEtag,
	safePersistentPath: safePersistentPath,
	fetchArguments: fetchArguments,
	putResultArguments: putResultArguments,
	validResponsePath: validResponsePath,
	finalizeRefresh: finalizeRefresh,
	guardJournal: guardJournal,
	rollbackDesired: rollbackDesired,
	applyDesired: applyDesired,
};
