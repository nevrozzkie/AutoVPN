'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const stateMachine = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/state.uc'));
const journal = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/journal.uc'));
const health = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/health.uc'));
const orchestration = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/orchestration.uc'));
const fixture = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json'), 'utf8'));

function copy(value) {
	return JSON.parse(JSON.stringify(value));
}

function etag(character) {
	return `"${character.repeat(64)}"`;
}

function runtime(activeProfile = 'vless-reality') {
	return {
		active_profile: activeProfile,
		capabilities: {
			vless: true,
			hysteria2: false,
			amneziawg: false,
			zapret: false,
			policy_routing: true
		}
	};
}

function schema4WithIndependentAwgLanes() {
	const snapshot = copy(fixture);
	const primaryKey = Buffer.alloc(32, 1).toString('base64');
	const auxiliaryKey = Buffer.alloc(32, 2).toString('base64');
	function profile(privateKey, address, legacy) {
		const value = {
			protocol_version: 1,
			capabilities: {
				awg_obfuscation_v1: true,
				awg2_i_fields: false,
				obfuscation_fields: ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4']
			},
			interface: { private_key: privateKey, address, dns_servers: ['10.66.66.1'] },
			peer: {
				public_key: 'server-public-key', preshared_key: 'shared-secret',
				endpoint: { host: snapshot.server.endpoint, port: 51820 }, persistent_keepalive: 25
			},
			obfuscation: { Jc: 4, Jmin: 40, Jmax: 70, S1: 1, S2: 2, H1: 3, H2: 4, H3: 5, H4: 6 },
			route_allowed_ips: ['0.0.0.0/0', '::/0'], install_routes: false
		};
		if (legacy) value.legacy_amnezia_vpn_import_key = 'vpn://primary-import';
		return value;
	}
	snapshot.schema_version = 4;
	snapshot.protocols.amneziawg = { enabled: true, profile: profile(primaryKey, '10.66.66.2/32', true) };
	snapshot.protocols.amneziawg_aux = { enabled: true, profile: profile(auxiliaryKey, '10.66.66.3/32', false) };
	return snapshot;
}

function driveSuccessfulApply(state, snapshot, snapshotEtag) {
	assert.equal(stateMachine.receiveSnapshot(state, snapshot, snapshotEtag).ok, true);
	assert.equal(stateMachine.beginApply(state).ok, true);
	assert.equal(stateMachine.prepared(state).ok, true);
	assert.equal(stateMachine.activating(state).ok, true);
	assert.equal(stateMachine.activated(state).ok, true);
	assert.equal(stateMachine.verified(state, runtime()).ok, true);
}

test('snapshot v3 fixture is accepted and schema is the same version', () => {
	const schema = JSON.parse(fs.readFileSync(path.join(root, 'files/usr/share/autovpn/snapshot.schema.json'), 'utf8'));
	assert.deepEqual(schema.properties.schema_version.enum, [3, 4]);
	assert.deepEqual(stateMachine.validateSnapshot(copy(fixture)), { ok: true, errors: [] });
});

test('validator accepts the complete current server protocol shape', () => {
	const snapshot = copy(fixture);
	snapshot.protocols.hysteria2 = {
		enabled: true,
		outbound: {
			type: 'hysteria2',
			tag: 'hysteria2',
			server: snapshot.server.endpoint,
			server_port: 8443,
			password: 'test-hysteria-password',
			tls: { enabled: true, server_name: 'example.com', insecure: true },
			obfs: { type: 'salamander', password: 'test-obfs-password' }
		}
	};
	snapshot.protocols.amneziawg = {
		enabled: true,
		profile: {
			protocol_version: 1,
			capabilities: {
				awg_obfuscation_v1: true,
				awg2_i_fields: false,
				obfuscation_fields: ['Jc', 'Jmin', 'Jmax', 'S1', 'S2', 'H1', 'H2', 'H3', 'H4']
			},
			interface: {
				private_key: 'test-private-key',
				address: '10.66.66.8/32',
				dns_servers: ['10.66.66.1']
			},
			peer: {
				public_key: 'test-public-key',
				preshared_key: 'test-preshared-key',
				endpoint: { host: snapshot.server.endpoint, port: 51820 },
				persistent_keepalive: 25
			},
			obfuscation: { Jc: 4, Jmin: 40, Jmax: 70, S1: 1, S2: 2, H1: 3, H2: 4, H3: 5, H4: 6 },
			route_allowed_ips: ['0.0.0.0/0', '::/0'],
			install_routes: false,
			legacy_amnezia_vpn_import_key: 'vpn://test-import-key'
		}
	};
	assert.deepEqual(stateMachine.validateSnapshot(snapshot), { ok: true, errors: [] });
});

test('validator accepts schema 4 with independent valid AWG lane identities', () => {
	const snapshot = schema4WithIndependentAwgLanes();
	assert.deepEqual(stateMachine.validateSnapshot(snapshot), { ok: true, errors: [] });
	assert.notEqual(snapshot.protocols.amneziawg.profile.interface.private_key,
		snapshot.protocols.amneziawg_aux.profile.interface.private_key);
	assert.notEqual(snapshot.protocols.amneziawg.profile.interface.address,
		snapshot.protocols.amneziawg_aux.profile.interface.address);
	assert.equal('legacy_amnezia_vpn_import_key' in snapshot.protocols.amneziawg_aux.profile, false);
});

test('schema 4 rejects shared AWG lane identity, auxiliary legacy import, and malformed auxiliary profiles', () => {
	const sharedKey = schema4WithIndependentAwgLanes();
	sharedKey.protocols.amneziawg_aux.profile.interface.private_key = sharedKey.protocols.amneziawg.profile.interface.private_key;
	assert.equal(stateMachine.validateSnapshot(sharedKey).ok, false);
	const sharedAddress = schema4WithIndependentAwgLanes();
	sharedAddress.protocols.amneziawg_aux.profile.interface.address = sharedAddress.protocols.amneziawg.profile.interface.address;
	assert.equal(stateMachine.validateSnapshot(sharedAddress).ok, false);
	const auxiliaryLegacy = schema4WithIndependentAwgLanes();
	auxiliaryLegacy.protocols.amneziawg_aux.profile.legacy_amnezia_vpn_import_key = 'vpn://not-allowed';
	assert.equal(stateMachine.validateSnapshot(auxiliaryLegacy).ok, false);
	const malformedAuxiliary = schema4WithIndependentAwgLanes();
	malformedAuxiliary.protocols.amneziawg_aux.profile = { protocol_version: 1 };
	assert.equal(stateMachine.validateSnapshot(malformedAuxiliary).ok, false);
});

test('schema 4 requires the auxiliary slot and schema 3 forbids it', () => {
	const missingAuxiliary = schema4WithIndependentAwgLanes();
	delete missingAuxiliary.protocols.amneziawg_aux;
	assert.equal(stateMachine.validateSnapshot(missingAuxiliary).ok, false);
	const v3ExtraAuxiliary = copy(fixture);
	v3ExtraAuxiliary.protocols.amneziawg_aux = { enabled: false, profile: null };
	assert.equal(stateMachine.validateSnapshot(v3ExtraAuxiliary).ok, false);
});

test('validator returns structured rejection for malformed nested objects', () => {
	const snapshot = copy(fixture);
	snapshot.server = null;
	const validation = stateMachine.validateSnapshot(snapshot);
	assert.equal(validation.ok, false);
	assert.equal(validation.errors.some(error => error.path === '$.server'), true);
});

test('validator requires the stable router identity', () => {
	const missing = copy(fixture);
	delete missing.router_id;
	assert.equal(stateMachine.validateSnapshot(missing).ok, false);
	const malformed = copy(fixture);
	malformed.router_id = 'wrong.router';
	const validation = stateMachine.validateSnapshot(malformed);
	assert.equal(validation.ok, false);
	assert.equal(validation.errors.some(error => error.path === '$.router_id'), true);
});

test('304 and an already applied ETag are no-ops', () => {
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	const sequence = state.sequence;
	assert.equal(stateMachine.fetchedNotModified(state).noop, true);
	assert.equal(stateMachine.receiveSnapshot(state, copy(fixture), etag('a')).code, 'etag_noop');
	assert.equal(state.sequence, sequence);
	assert.equal(state.applied.snapshot.revision, 41);
});

test('validation rejection preserves applied state and records only safe errors', () => {
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	const invalid = copy(fixture);
	invalid.revision = 42;
	invalid.snapshot_sha256 = '2'.repeat(64);
	invalid.protocols.vless.outbound.server_port = 70000;
	invalid.unexpected = 'must be rejected';
	const result = stateMachine.receiveSnapshot(state, invalid, etag('b'));
	assert.equal(result.ok, false);
	assert.equal(result.code, 'snapshot_validation_failed');
	assert.equal(state.applied.snapshot.revision, 41);
	assert.equal(state.desired, null);
	assert.equal(state.rejected.revision, 42);
	assert.deepEqual(Object.keys(state.rejected.errors[0]).sort(), ['code', 'path']);
});

test('activation failure rolls back atomically to applied and persists FAILED report', () => {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-journal-'));
	const journalPath = path.join(directory, 'journal.json');
	const storage = {
		read: () => fs.existsSync(journalPath) ? fs.readFileSync(journalPath, 'utf8') : null,
		decode: JSON.parse,
		encode: value => `${JSON.stringify(value)}\n`,
		atomicWrite: raw => {
			const temporary = `${journalPath}.new`;
			fs.writeFileSync(temporary, raw, { mode: 0o600 });
			fs.renameSync(temporary, journalPath);
			return true;
		}
	};
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	stateMachine.reportAccepted(state, state.pending_report.idempotency_key);
	const next = copy(fixture);
	next.revision = 42;
	next.snapshot_sha256 = '2'.repeat(64);
	stateMachine.receiveSnapshot(state, next, etag('b'));
	stateMachine.beginApply(state);
	journal.store(storage, state);
	stateMachine.prepared(state);
	stateMachine.activating(state);
	stateMachine.applyFailed(state, 'activate_failed');
	stateMachine.rolledBack(state, true, runtime().capabilities);
	assert.equal(journal.store(storage, state), true);
	assert.equal(fs.existsSync(`${journalPath}.new`), false);
	const reloaded = journal.load(storage, null, stateMachine.validateSnapshot);
	assert.equal(reloaded.phase, 'IDLE');
	assert.equal(reloaded.applied.snapshot.revision, 41);
	assert.equal(reloaded.desired, null);
	assert.equal(reloaded.rejected.revision, 42);
	assert.equal(reloaded.pending_report.body.outcome, 'FAILED');
	assert.equal(reloaded.pending_report.body.diagnostics.stage, 'rolled_back');
});

test('successful apply moves old applied snapshot to last-good', () => {
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	stateMachine.reportAccepted(state, state.pending_report.idempotency_key);
	const next = copy(fixture);
	next.revision = 42;
	next.snapshot_sha256 = '2'.repeat(64);
	driveSuccessfulApply(state, next, etag('b'));
	assert.equal(state.applied.snapshot.revision, 42);
	assert.equal(state.last_good.snapshot.revision, 41);
	assert.equal(state.desired, null);
});

test('result reporting retries the exact journaled key and clears once', () => {
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	const report = copy(state.pending_report);
	assert.equal(report.idempotency_key, 'r41-' + '1'.repeat(16) + '-s1');
	assert.equal(report.etag, etag('a'));
	assert.equal(report.body.outcome, 'APPLIED');
	assert.equal(state.pending_report.idempotency_key, report.idempotency_key);
	assert.equal(stateMachine.reportAccepted(state, 'wrong-key').code, 'report_key_mismatch');
	assert.deepEqual(state.pending_report, report);
	assert.equal(stateMachine.reportAccepted(state, report.idempotency_key).ok, true);
	assert.equal(state.pending_report, null);
	assert.equal(stateMachine.reportAccepted(state, report.idempotency_key).noop, true);
});

test('interrupted transactional phases require recovery', () => {
	const state = stateMachine.initialState();
	stateMachine.receiveSnapshot(state, copy(fixture), etag('a'));
	stateMachine.beginApply(state);
	assert.equal(stateMachine.recoverRequired(state), true);
	stateMachine.applyFailed(state, 'interrupted_apply');
	stateMachine.rolledBack(state, false, {});
	assert.equal(state.phase, 'FAIL_CLOSED');
	assert.equal(state.last_error, 'rollback_failed');
	assert.equal(stateMachine.recoverRequired(state), true);
});

test('HTTPS probe observations use 3-down, 2-up hysteresis and cooldown', () => {
	const settings = { failure_threshold: 3, success_threshold: 2, cooldown_seconds: 60 };
	const state = health.initialHealth();
	health.observe(state, true, 1, settings);
	assert.equal(state.status, 'unknown');
	health.observe(state, true, 2, settings);
	assert.equal(state.status, 'up');
	health.observe(state, false, 3, settings);
	health.observe(state, false, 4, settings);
	assert.equal(state.status, 'up');
	health.observe(state, false, 5, settings);
	assert.equal(state.status, 'down');
	health.observe(state, true, 6, settings);
	health.observe(state, true, 7, settings);
	assert.equal(state.status, 'down');
	health.observe(state, true, 65, settings);
	assert.equal(state.status, 'up');
});

test('manual override never bypasses fail-closed health selection', () => {
	const candidates = ['vless-reality', 'hysteria2'];
	const states = {
		'vless-reality': { status: 'down' },
		hysteria2: { status: 'up' }
	};
	assert.equal(health.selectOutbound(candidates, states, 'auto'), 'hysteria2');
	assert.equal(health.selectOutbound(candidates, states, 'vless-reality'), null);
	assert.equal(health.selectOutbound(candidates, states, 'hysteria2'), 'hysteria2');
});

test('HTTP adapter argv contains normalized bounded timeouts and conditional ETag', () => {
	const config = {
		http_adapter: '/usr/libexec/autovpn/http-adapter',
		base_url: 'https://vpn.example',
		credential_file: '/etc/autovpn/credentials',
		state_dir: '/etc/autovpn/state',
		connect_timeout: orchestration.boundedInteger('9', 10, 1, 60),
		request_timeout: orchestration.boundedInteger('45', 30, 2, 120)
	};
	assert.equal(orchestration.boundedInteger('11seconds', 10, 1, 60), 10);
	assert.equal(orchestration.boundedInteger('0', 10, 1, 60), 10);
	assert.equal(orchestration.boundedInteger('121', 30, 2, 120), 30);
	assert.equal(orchestration.safePersistentPath('/', '/etc/autovpn/state'), '/etc/autovpn/state');
	assert.equal(orchestration.safePersistentPath('/etc/autovpn/../shadow', '/etc/autovpn/state'), '/etc/autovpn/state');
	assert.equal(orchestration.safePersistentPath('/etc/autovpn/custom-state', '/etc/autovpn/state'), '/etc/autovpn/custom-state');
	assert.deepEqual(orchestration.fetchArguments(config, ''), [
		config.http_adapter, 'fetch', config.base_url, config.credential_file, config.state_dir, '', '9', '45'
	]);
	assert.deepEqual(orchestration.fetchArguments(config, 'unquoted-etag'), [
		config.http_adapter, 'fetch', config.base_url, config.credential_file, config.state_dir, '', '9', '45'
	]);
	assert.deepEqual(orchestration.fetchArguments(config, etag('a')), [
		config.http_adapter, 'fetch', config.base_url, config.credential_file, config.state_dir, etag('a'), '9', '45'
	]);
	assert.equal(orchestration.validResponsePath(config.state_dir, '/etc/autovpn/state/http-response.42'), true);
	assert.equal(orchestration.validResponsePath(config.state_dir, '/etc/autovpn/state/../credentials'), false);
	assert.deepEqual(orchestration.putResultArguments(config, {
		idempotency_key: 'r41-test-s1',
		etag: etag('a')
	}, '/etc/autovpn/state/journal.json'), [
		config.http_adapter,
		'put-result',
		config.base_url,
		config.credential_file,
		'r41-test-s1',
		etag('a'),
		'/etc/autovpn/state/journal.json',
		'9',
		'45'
	]);
});

test('rolled-back apply can report FAILED but refresh remains unsuccessful', () => {
	const state = stateMachine.initialState();
	driveSuccessfulApply(state, copy(fixture), etag('a'));
	stateMachine.reportAccepted(state, state.pending_report.idempotency_key);
	const next = copy(fixture);
	next.revision = 42;
	next.snapshot_sha256 = '2'.repeat(64);
	stateMachine.receiveSnapshot(state, next, etag('b'));
	stateMachine.beginApply(state);
	stateMachine.prepared(state);
	stateMachine.activating(state);
	stateMachine.applyFailed(state, 'activate_failed');
	const rolledBack = stateMachine.rolledBack(state, true, runtime().capabilities);
	rolledBack.outcome = 'ROLLED_BACK';
	const pendingKey = state.pending_report.idempotency_key;
	const reported = stateMachine.reportAccepted(state, pendingKey);
	const final = orchestration.finalizeRefresh(rolledBack, reported);
	assert.equal(state.pending_report, null);
	assert.deepEqual({
		ok: final.ok,
		code: final.code,
		outcome: final.outcome,
		failure_code: final.failure_code,
		report_sent: final.report_sent
	}, {
		ok: false,
		code: 'rolled_back',
		outcome: 'ROLLED_BACK',
		failure_code: 'activate_failed',
		report_sent: true
	});
});

function desiredState() {
	const state = stateMachine.initialState();
	assert.equal(stateMachine.receiveSnapshot(state, copy(fixture), etag('a')).ok, true);
	return state;
}

function faultedApply(failedPhase, runtimeFailures = {}) {
	let durable = copy(desiredState());
	let storageFailed = false;
	const calls = [];
	const rollbackJournals = [];
	const result = orchestration.applyDesired(durable, stateMachine, {
		persist(transition) {
			if (transition.state.phase === failedPhase)
				storageFailed = true;
			if (storageFailed)
				return { ok: false, code: 'journal_write_failed', state: transition.state };
			durable = copy(transition.state);
			return transition;
		},
		runtime(action) {
			calls.push(action);
			if (action === 'rollback')
				rollbackJournals.push(copy(durable));
			if (runtimeFailures[action])
				return { ok: false, code: runtimeFailures[action] };
			if (action === 'verify')
				return { ok: true, ...runtime() };
			return { ok: true };
		},
		capabilities() {
			return runtime().capabilities;
		},
		activeProfile(value) {
			return value;
		}
	});
	return { result, durable, calls, rollbackJournals };
}

test('persist failures after prepare, activate and verify immediately roll back from durable pre-commit state', () => {
	for (const scenario of [
		{ phase: 'PREPARED', durablePhase: 'PREPARING', calls: ['prepare', 'rollback'] },
		{ phase: 'VERIFYING', durablePhase: 'ACTIVATING', calls: ['prepare', 'activate', 'rollback'] },
		{ phase: 'IDLE', durablePhase: 'VERIFYING', calls: ['prepare', 'activate', 'verify', 'rollback'] }
	]) {
		const run = faultedApply(scenario.phase);
		assert.equal(run.result.ok, false, scenario.phase);
		assert.equal(run.result.code, 'journal_write_failed', scenario.phase);
		assert.equal(run.result.outcome, 'ROLLED_BACK', scenario.phase);
		assert.equal(run.result.rollback_confirmed, true, scenario.phase);
		assert.deepEqual(run.calls, scenario.calls, scenario.phase);
		assert.equal(run.durable.phase, scenario.durablePhase, scenario.phase);
		assert.equal(run.rollbackJournals[0].phase, scenario.durablePhase, scenario.phase);
		assert.equal(run.rollbackJournals[0].applied, null, scenario.phase);
		assert.equal(run.rollbackJournals[0].desired.snapshot.revision, fixture.revision, scenario.phase);
	}
});

test('rollback failure invokes the journal-independent fail-closed action', () => {
	let durable = copy(desiredState());
	const calls = [];
	const result = orchestration.applyDesired(durable, stateMachine, {
		persist(transition) {
			durable = copy(transition.state);
			return transition;
		},
		runtime(action) {
			calls.push(action);
			if (action === 'activate')
				return { ok: false, code: 'activate_failed' };
			if (action === 'rollback')
				return { ok: false, code: 'rollback_failed' };
			return { ok: true };
		},
		capabilities() {
			return runtime().capabilities;
		},
		activeProfile(value) {
			return value;
		}
	});
	assert.deepEqual(calls, ['prepare', 'activate', 'rollback', 'fail-closed']);
	assert.equal(result.ok, false);
	assert.equal(result.outcome, 'FAIL_CLOSED');
	assert.equal(result.fail_closed_confirmed, true);
	assert.equal(durable.phase, 'FAIL_CLOSED');
});

test('unconfirmed fail-closed is never reported as a safe rollback', () => {
	const operations = {
		persist: transition => transition,
		runtime(action) {
			return action === 'fail-closed'
				? { ok: false, code: 'firewall_failed' }
				: { ok: false, code: 'rollback_failed' };
		},
		capabilities: () => runtime().capabilities,
		activeProfile: value => value
	};
	const result = orchestration.rollbackDesired(desiredState(), stateMachine, operations, 'prepare_failed');
	assert.equal(result.ok, false);
	assert.equal(result.code, 'fail_closed_unconfirmed');
	assert.equal(result.outcome, 'FAIL_CLOSED');
	assert.equal(result.fail_closed_confirmed, false);
});

test('journal loader rejects complete JSON with unknown fields, phases or inconsistent slots', () => {
	function loadValue(value) {
		return journal.load({
			read: () => JSON.stringify(value),
			decode: JSON.parse
		}, null, stateMachine.validateSnapshot);
	}

	const incomplete = { journal_version: 1 };
	assert.equal(loadValue(incomplete), null);
	assert.equal(journal.load({ read: () => '', decode: JSON.parse }, null, stateMachine.validateSnapshot), null);

	const unknownField = stateMachine.initialState();
	unknownField.secret_extension = true;
	assert.equal(loadValue(unknownField), null);

	const unknownPhase = stateMachine.initialState();
	unknownPhase.phase = 'COMMITTING';
	assert.equal(loadValue(unknownPhase), null);

	const inconsistent = stateMachine.initialState();
	inconsistent.phase = 'VERIFYING';
	assert.equal(loadValue(inconsistent), null);

	const pendingMismatch = stateMachine.initialState();
	driveSuccessfulApply(pendingMismatch, copy(fixture), etag('a'));
	pendingMismatch.pending_report.idempotency_key = 'unrelated-key';
	assert.equal(loadValue(pendingMismatch), null);

	const valid = desiredState();
	assert.deepEqual(loadValue(valid), valid);
});

test('invalid journal guard invokes fail-closed without exposing journal state', () => {
	const calls = [];
	const result = orchestration.guardJournal(null, {
		runtime(action) {
			calls.push(action);
			return { ok: true };
		}
	});
	assert.deepEqual(calls, ['fail-closed']);
	assert.deepEqual(result, {
		ok: false,
		code: 'journal_invalid',
		fail_closed_confirmed: true
	});
	assert.equal(Object.hasOwn(result, 'state'), false);
});
