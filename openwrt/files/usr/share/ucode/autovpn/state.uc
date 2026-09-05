'use strict';

const lanes = require('autovpn.lanes');

const CAPABILITY_NAMES = [
	'vless',
	'hysteria2',
	'amneziawg',
	'zapret',
	'policy_routing',
];

function isObject(value) {
	return type(value) == 'object';
}

function isString(value, minimum, maximum) {
	return type(value) == 'string' && length(value) >= minimum && length(value) <= maximum;
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

function addError(errors, path, code) {
	push(errors, { path: path, code: code });
}

function validateTls(tls, path, errors, realityRequired) {
	let expected = realityRequired
		? ['enabled', 'server_name', 'utls', 'reality']
		: ['enabled', 'server_name', 'insecure'];
	if (!hasExactKeys(tls, expected)) {
		addError(errors, path, 'invalid_schema');
		return;
	}
	if (tls.enabled !== true || !isString(tls.server_name, 1, 253))
		addError(errors, path, 'invalid_tls');
	if (!realityRequired) {
		if (type(tls.insecure) != 'bool')
			addError(errors, path + '.insecure', 'invalid_type');
		return;
	}
	if (!hasExactKeys(tls.utls, ['enabled', 'fingerprint']) ||
		tls.utls.enabled !== true || !isString(tls.utls.fingerprint, 1, 64))
		addError(errors, path + '.utls', 'invalid_schema');
	if (!hasExactKeys(tls.reality, ['enabled', 'public_key', 'short_id']) ||
		tls.reality.enabled !== true ||
		!isString(tls.reality.public_key, 1, 256) ||
		!isString(tls.reality.short_id, 1, 32))
		addError(errors, path + '.reality', 'invalid_schema');
}

function validateVless(value, endpoint, errors) {
	if (!hasExactKeys(value, ['type', 'tag', 'server', 'server_port', 'uuid', 'flow', 'tls'])) {
		addError(errors, '$.protocols.vless.outbound', 'invalid_schema');
		return;
	}
	if (value.type != 'vless' || value.tag != 'vless-reality' || value.server != endpoint)
		addError(errors, '$.protocols.vless.outbound', 'invalid_identity');
	if (!isInteger(value.server_port, 1, 65535))
		addError(errors, '$.protocols.vless.outbound.server_port', 'invalid_port');
	if (!matches(value.uuid, /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/))
		addError(errors, '$.protocols.vless.outbound.uuid', 'invalid_uuid');
	if (value.flow != 'xtls-rprx-vision')
		addError(errors, '$.protocols.vless.outbound.flow', 'unsupported_flow');
	validateTls(value.tls, '$.protocols.vless.outbound.tls', errors, true);
}

function validateHysteria(value, endpoint, errors) {
	if (!isObject(value)) {
		addError(errors, '$.protocols.hysteria2.outbound', 'invalid_schema');
		return;
	}
	let expected = value.obfs == null
		? ['type', 'tag', 'server', 'server_port', 'password', 'tls']
		: ['type', 'tag', 'server', 'server_port', 'password', 'tls', 'obfs'];
	if (!hasExactKeys(value, expected)) {
		addError(errors, '$.protocols.hysteria2.outbound', 'invalid_schema');
		return;
	}
	if (value.type != 'hysteria2' || value.tag != 'hysteria2' || value.server != endpoint)
		addError(errors, '$.protocols.hysteria2.outbound', 'invalid_identity');
	if (!isInteger(value.server_port, 1, 65535) || !isString(value.password, 1, 512))
		addError(errors, '$.protocols.hysteria2.outbound', 'invalid_endpoint');
	validateTls(value.tls, '$.protocols.hysteria2.outbound.tls', errors, false);
	if (value.obfs != null &&
		(!hasExactKeys(value.obfs, ['type', 'password']) || value.obfs.type != 'salamander' ||
		 !isString(value.obfs.password, 1, 512)))
		addError(errors, '$.protocols.hysteria2.outbound.obfs', 'invalid_schema');
}

function validateAwg(value, endpoint, errors, auxiliary) {
	let expected = [
		'protocol_version', 'capabilities', 'interface', 'peer', 'obfuscation',
		'route_allowed_ips', 'install_routes',
	];
	if (!auxiliary) push(expected, 'legacy_amnezia_vpn_import_key');
	if (!hasExactKeys(value, expected)) {
		addError(errors, '$.protocols.amneziawg.profile', 'invalid_schema');
		return;
	}
	if (value.protocol_version != 1 || value.install_routes !== false ||
		type(value.route_allowed_ips) != 'array' || length(value.route_allowed_ips) != 2 ||
		value.route_allowed_ips[0] != '0.0.0.0/0' || value.route_allowed_ips[1] != '::/0')
		addError(errors, '$.protocols.amneziawg.profile', 'unsupported_profile');
	if (!hasExactKeys(value.capabilities, ['awg_obfuscation_v1', 'awg2_i_fields', 'obfuscation_fields']) ||
		value.capabilities.awg_obfuscation_v1 !== true || value.capabilities.awg2_i_fields !== false ||
		type(value.capabilities.obfuscation_fields) != 'array')
		addError(errors, '$.protocols.amneziawg.profile.capabilities', 'invalid_schema');
	if (!hasExactKeys(value.interface, ['private_key', 'address', 'dns_servers']) ||
		!isString(value.interface.private_key, 1, 512) || !isString(value.interface.address, 1, 64) ||
		type(value.interface.dns_servers) != 'array')
		addError(errors, '$.protocols.amneziawg.profile.interface', 'invalid_schema');
	if (!hasExactKeys(value.peer, ['public_key', 'preshared_key', 'endpoint', 'persistent_keepalive']) ||
		!isString(value.peer.public_key, 1, 512) || !isString(value.peer.preshared_key, 1, 512) ||
		!isInteger(value.peer.persistent_keepalive, 0, 65535) ||
		!hasExactKeys(value.peer.endpoint, ['host', 'port']) ||
		value.peer.endpoint.host != endpoint || !isInteger(value.peer.endpoint.port, 1, 65535))
		addError(errors, '$.protocols.amneziawg.profile.peer', 'invalid_schema');
	if (!hasExactKeys(value.obfuscation, ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4']))
		addError(errors, '$.protocols.amneziawg.profile.obfuscation', 'invalid_schema');
	else
		for (let name in value.obfuscation)
			if (!isInteger(value.obfuscation[name], 0, 4294967295))
				addError(errors, '$.protocols.amneziawg.profile.obfuscation.' + name, 'invalid_integer');
	if (!auxiliary && !isString(value.legacy_amnezia_vpn_import_key, 1, 16384))
		addError(errors, '$.protocols.amneziawg.profile.legacy_amnezia_vpn_import_key', 'invalid_string');
}

function validateProtocolSlot(slot, payloadName, errors, validateEnabled) {
	if (!hasExactKeys(slot, ['enabled', payloadName]) || type(slot.enabled) != 'bool') {
		addError(errors, '$.protocols', 'invalid_schema');
		return false;
	}
	if (!slot.enabled && slot[payloadName] != null)
		addError(errors, '$.protocols.' + payloadName, 'must_be_null_when_disabled');
	if (slot.enabled && !isObject(slot[payloadName]))
		addError(errors, '$.protocols.' + payloadName, 'required_when_enabled');
	return slot.enabled && isObject(slot[payloadName]);
}

function validateSnapshot(snapshot) {
	let errors = [];
	if (!hasExactKeys(snapshot, [
		'schema_version', 'router_id', 'revision', 'snapshot_sha256', 'applied_at', 'published_at',
		'client', 'server', 'protocols',
	])) {
		addError(errors, '$', 'invalid_schema');
		return { ok: false, errors: errors };
	}
	if (snapshot.schema_version != 3 && snapshot.schema_version != 4)
		addError(errors, '$.schema_version', 'unsupported_version');
	if (!matches(snapshot.router_id, /^[A-Za-z0-9_-]{8,64}$/))
		addError(errors, '$.router_id', 'invalid_router_id');
	if (!isInteger(snapshot.revision, 0, 9007199254740991))
		addError(errors, '$.revision', 'invalid_revision');
	if (!matches(snapshot.snapshot_sha256, /^[0-9a-f]{64}$/))
		addError(errors, '$.snapshot_sha256', 'invalid_sha256');
	if (!isString(snapshot.applied_at, 1, 64) ||
		!(snapshot.published_at == null || isString(snapshot.published_at, 1, 64)))
		addError(errors, '$.applied_at', 'invalid_timestamp');
	if (!hasExactKeys(snapshot.client, ['id', 'name']) ||
		!isInteger(snapshot.client.id, 1, 2147483647) || !isString(snapshot.client.name, 1, 128))
		addError(errors, '$.client', 'invalid_client');
	if (!hasExactKeys(snapshot.server, ['endpoint']) || !isString(snapshot.server.endpoint, 1, 253))
		addError(errors, '$.server', 'invalid_server');
	let protocolKeys = ['vless', 'hysteria2', 'amneziawg'];
	if (snapshot.schema_version == 4) push(protocolKeys, 'amneziawg_aux');
	if (!hasExactKeys(snapshot.protocols, protocolKeys)) {
		addError(errors, '$.protocols', 'invalid_schema');
		return { ok: false, errors: errors };
	}
	let endpoint = isObject(snapshot.server) ? snapshot.server.endpoint : null;
	if (validateProtocolSlot(snapshot.protocols.vless, 'outbound', errors))
		validateVless(snapshot.protocols.vless.outbound, endpoint, errors);
	if (validateProtocolSlot(snapshot.protocols.hysteria2, 'outbound', errors))
		validateHysteria(snapshot.protocols.hysteria2.outbound, endpoint, errors);
	if (validateProtocolSlot(snapshot.protocols.amneziawg, 'profile', errors))
		validateAwg(snapshot.protocols.amneziawg.profile, endpoint, errors);
	if (snapshot.schema_version == 4) {
		if (validateProtocolSlot(snapshot.protocols.amneziawg_aux, 'profile', errors))
			validateAwg(snapshot.protocols.amneziawg_aux.profile, endpoint, errors, true);
		if (length(errors) == 0) {
			let pair = lanes.validatePair(snapshot.protocols.amneziawg.profile, snapshot.protocols.amneziawg_aux.profile);
			if (!pair.ok) addError(errors, '$.protocols.amneziawg_aux.profile', pair.code);
		}
	}

	return { ok: length(errors) == 0, errors: errors };
}

function emptyCapabilities() {
	return {
		vless: false,
		hysteria2: false,
		amneziawg: false,
		zapret: false,
		policy_routing: false,
	};
}

function initialState() {
	return {
		journal_version: 1,
		sequence: 0,
		phase: 'IDLE',
		desired: null,
		applied: null,
		last_good: null,
		rejected: null,
		pending_report: null,
		capabilities: emptyCapabilities(),
		active_profile: null,
		last_error: null,
	};
}

function bump(state) {
	state.sequence += 1;
	return state;
}

function receiveSnapshot(state, snapshot, etag, force) {
	if (!matches(etag, /^"[0-9a-f]{64}"$/))
		return { ok: false, code: 'invalid_etag', state: state };
	let validation = validateSnapshot(snapshot);
	if (!validation.ok) {
		state.rejected = {
			revision: type(snapshot) == 'object' && isInteger(snapshot.revision, 0, 9007199254740991)
				? snapshot.revision
				: null,
			etag: etag,
			code: 'snapshot_validation_failed',
			errors: validation.errors,
		};
		state.last_error = 'snapshot_validation_failed';
		return { ok: false, code: 'snapshot_validation_failed', state: bump(state), errors: validation.errors };
	}
	if (force !== true && state.applied != null && state.applied.etag == etag)
		return { ok: true, noop: true, code: 'etag_noop', state: state };
	if (state.applied != null && snapshot.revision < state.applied.snapshot.revision)
		return { ok: false, code: 'revision_regression', state: state };
	state.desired = { etag: etag, snapshot: snapshot, attempt: state.sequence + 1 };
	state.rejected = null;
	state.last_error = null;
	state.phase = 'READY';
	return { ok: true, noop: false, state: bump(state) };
}

function fetchedNotModified(state) {
	return { ok: true, noop: true, code: 'not_modified', state: state };
}

function beginApply(state) {
	if (state.phase != 'READY' || state.desired == null)
		return { ok: false, code: 'invalid_phase', state: state };
	state.phase = 'PREPARING';
	state.last_error = null;
	return { ok: true, state: bump(state) };
}

function prepared(state) {
	if (state.phase != 'PREPARING')
		return { ok: false, code: 'invalid_phase', state: state };
	state.phase = 'PREPARED';
	return { ok: true, state: bump(state) };
}

function activating(state) {
	if (state.phase != 'PREPARED')
		return { ok: false, code: 'invalid_phase', state: state };
	state.phase = 'ACTIVATING';
	return { ok: true, state: bump(state) };
}

function activated(state) {
	if (state.phase != 'ACTIVATING')
		return { ok: false, code: 'invalid_phase', state: state };
	state.phase = 'VERIFYING';
	return { ok: true, state: bump(state) };
}

function normalizeCapabilities(value) {
	let result = emptyCapabilities();
	if (!isObject(value))
		return result;
	for (let i = 0; i < length(CAPABILITY_NAMES); i++) {
		let name = CAPABILITY_NAMES[i];
		result[name] = value[name] === true;
	}
	return result;
}

function reportFor(entry, outcome, activeProfile, capabilities, diagnostics) {
	let suffix = substr(entry.snapshot.snapshot_sha256, 0, 16);
	return {
		idempotency_key: 'r' + entry.snapshot.revision + '-' + suffix + '-s' + entry.attempt,
		etag: entry.etag,
		body: {
			schema_version: 1,
			revision: entry.snapshot.revision,
			snapshot_sha256: entry.snapshot.snapshot_sha256,
			outcome: outcome,
			active_profile: activeProfile,
			capabilities: normalizeCapabilities(capabilities),
			diagnostics: diagnostics,
		},
	};
}

function verified(state, runtime) {
	if (state.phase != 'VERIFYING' || state.desired == null)
		return { ok: false, code: 'invalid_phase', state: state };
	let previous = state.applied;
	let current = state.desired;
	state.last_good = previous;
	state.applied = current;
	state.desired = null;
	state.phase = 'IDLE';
	state.capabilities = normalizeCapabilities(runtime.capabilities);
	state.active_profile = runtime.active_profile || null;
	state.last_error = null;
	state.pending_report = reportFor(
		current,
		'APPLIED',
		state.active_profile,
		state.capabilities,
		{ stage: 'verified' }
	);
	return { ok: true, state: bump(state) };
}

function applyFailed(state, code) {
	if (state.desired == null)
		return { ok: false, code: 'invalid_phase', state: state };
	state.phase = 'ROLLING_BACK';
	state.last_error = code;
	return { ok: true, state: bump(state) };
}

function rolledBack(state, rollbackOk, capabilities) {
	if (state.phase != 'ROLLING_BACK' || state.desired == null)
		return { ok: false, code: 'invalid_phase', state: state };
	let failed = state.desired;
	let failure = state.last_error || 'apply_failed';
	state.rejected = {
		revision: failed.snapshot.revision,
		etag: failed.etag,
		code: failure,
		errors: [],
	};
	if (rollbackOk)
		state.desired = null;
	state.phase = rollbackOk ? 'IDLE' : 'FAIL_CLOSED';
	state.capabilities = normalizeCapabilities(capabilities);
	state.active_profile = rollbackOk && state.applied != null ? state.active_profile : null;
	state.last_error = rollbackOk ? failure : 'rollback_failed';
	state.pending_report = reportFor(
		failed,
		'FAILED',
		state.active_profile,
		state.capabilities,
		{ stage: rollbackOk ? 'rolled_back' : 'rollback_failed', code: state.last_error }
	);
	return { ok: rollbackOk, state: bump(state), code: state.last_error };
}

function reportAccepted(state, idempotencyKey) {
	if (state.pending_report == null)
		return { ok: true, noop: true, state: state };
	if (state.pending_report.idempotency_key != idempotencyKey)
		return { ok: false, code: 'report_key_mismatch', state: state };
	state.pending_report = null;
	return { ok: true, noop: false, state: bump(state) };
}

function recoverRequired(state) {
	return state.phase == 'PREPARING' || state.phase == 'PREPARED' ||
		state.phase == 'ACTIVATING' || state.phase == 'VERIFYING' ||
		state.phase == 'ROLLING_BACK' || state.phase == 'FAIL_CLOSED';
}

return {
	CAPABILITY_NAMES: CAPABILITY_NAMES,
	validateSnapshot: validateSnapshot,
	initialState: initialState,
	receiveSnapshot: receiveSnapshot,
	fetchedNotModified: fetchedNotModified,
	beginApply: beginApply,
	prepared: prepared,
	activating: activating,
	activated: activated,
	verified: verified,
	applyFailed: applyFailed,
	rolledBack: rolledBack,
	reportAccepted: reportAccepted,
	recoverRequired: recoverRequired,
	normalizeCapabilities: normalizeCapabilities,
};
