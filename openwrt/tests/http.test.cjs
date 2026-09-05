'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const adapter = path.join(root, 'files/usr/libexec/autovpn/http-adapter');
const fakeBin = path.join(root, 'tests/fake-transport');
const http = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/http.uc'));
const snapshot = fs.readFileSync(path.join(__dirname, 'fixtures/snapshot-v3.json'), 'utf8');
const token = `avrt_${'i'.repeat(8)}.${'s'.repeat(43)}`;
const snapshotEtag = `"${'a'.repeat(64)}"`;

function fixture(status, headers, body) {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-http.'));
	fs.writeFileSync(path.join(directory, 'status'), status);
	fs.writeFileSync(path.join(directory, 'headers'), headers);
	fs.writeFileSync(path.join(directory, 'body'), body);
	return directory;
}

function runAdapter(args, directory, credentialFile, stateDirectory) {
	return childProcess.spawnSync(adapter, args, {
		encoding: 'utf8',
		env: {
			PATH: `${fakeBin}:${process.env.PATH}`,
			AUTOVPN_TEST_ROOT: root,
			AUTOVPN_FAKE_HTTP: directory,
			AUTOVPN_FAKE_STATE: stateDirectory
		}
	});
}

function credentialFile(directory) {
	const file = path.join(directory, 'credential');
	fs.writeFileSync(file, `${token}\n`, { mode: 0o600 });
	return file;
}

function curlConfig(directory) {
	return fs.readFileSync(path.join(directory, 'curl.conf.copy'), 'utf8');
}

function assertPrivateTemporaryCleanup(directory) {
	const workDirectory = fs.readFileSync(path.join(directory, 'work-dir'), 'utf8');
	assert.equal(fs.existsSync(workDirectory), false);
}

test('URL normalization accepts only bounded HTTPS URLs without userinfo, query or fragment', () => {
	assert.equal(http.normalizeBaseUrl('https://vpn.example///'), 'https://vpn.example');
	assert.equal(http.endpoint('https://vpn.example/prefix/', 'snapshot'), 'https://vpn.example/prefix/api/v2/router/snapshot');
	assert.equal(http.endpoint('https://vpn.example:8443', 'apply-result', 'boot-1~retry'), 'https://vpn.example:8443/api/v2/router/apply-results/boot-1~retry');
	for (const invalid of [
		'http://vpn.example',
		'https://user@vpn.example',
		'https://vpn.example/?x=1',
		'https://vpn.example/#fragment',
		'https://vpn.example/a//b',
		'https://vpn.example/../admin',
		'https://a.-invalid.example',
		'https://vpn.example:0'
	])
		assert.equal(http.normalizeBaseUrl(invalid), null, invalid);
});

test('fetch keeps secrets and body out of argv/env/stdout and configures strict curl defaults', () => {
	const directory = fixture('200', `HTTP/1.1 200 OK\r\nETag: ${snapshotEtag}\r\n\r\n`, snapshot);
	const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-state.'));
	const credentials = credentialFile(directory);
	const result = runAdapter(['fetch', 'https://vpn.example/root/', credentials, '/etc/autovpn/test-state', '', '9', '45'], directory, credentials, stateDirectory);
	assert.equal(result.status, 0, result.stderr || result.stdout);
	const metadata = JSON.parse(result.stdout);
	assert.deepEqual({ ok: metadata.ok, status: metadata.status, etag: metadata.etag }, { ok: true, status: 200, etag: snapshotEtag });
	assert.equal(result.stdout.includes(token), false);
	assert.equal(result.stdout.includes('test-password'), false);
	assert.equal(result.stderr, '');
	const argv = fs.readFileSync(path.join(directory, 'curl-argv.json'), 'utf8');
	assert.deepEqual(JSON.parse(argv), ['--disable', '--config', JSON.parse(argv)[2]]);
	assert.equal(argv.includes(token), false);
	assert.equal(fs.readFileSync(path.join(directory, 'secret-in-env'), 'utf8'), 'no');
	assert.equal(fs.readFileSync(path.join(directory, 'config-mode'), 'utf8'), String(0o600));
	assert.equal(fs.readFileSync(path.join(directory, 'work-dir-mode'), 'utf8'), String(0o700));
	const config = curlConfig(directory);
	assert.match(config, /url = "https:\/\/vpn\.example\/root\/api\/v2\/router\/snapshot"/);
	assert.match(config, /header = "Authorization: Bearer avrt_/);
	assert.match(config, /header = "Accept: application\/json"/);
	assert.doesNotMatch(config, /If-None-Match/);
	for (const expected of ['no-location', 'no-insecure', 'proto = "=https"', 'proto-redir = "=https"', 'noproxy = "*"', 'connect-timeout = 9', 'max-time = 45', 'max-filesize = 16384'])
		assert.equal(config.includes(expected), true, expected);
	const handoff = path.join(stateDirectory, path.basename(metadata.response_file));
	assert.equal(fs.statSync(handoff).mode & 0o777, 0o600);
	assert.equal(fs.readFileSync(handoff, 'utf8'), snapshot);
	assertPrivateTemporaryCleanup(directory);
});

test('conditional GET emits If-None-Match and accepts only an empty 304 with strong ETag', () => {
	const directory = fixture('304', `HTTP/1.1 304 Not Modified\r\nETag: ${snapshotEtag}\r\n\r\n`, '');
	const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-state.'));
	const result = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', snapshotEtag, '10', '30'], directory, null, stateDirectory);
	assert.equal(result.status, 0, result.stdout);
	assert.deepEqual(JSON.parse(result.stdout), { ok: true, status: 304, etag: snapshotEtag });
	assert.match(curlConfig(directory), new RegExp(`If-None-Match: \\\\"${'a'.repeat(64)}\\\\"`));
	assertPrivateTemporaryCleanup(directory);

	fs.writeFileSync(path.join(directory, 'headers'), 'HTTP/1.1 304 Not Modified\r\n\r\n');
	const rejected = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', '', '10', '30'], directory, null, stateDirectory);
	assert.equal(rejected.status, 1);
	assert.equal(JSON.parse(rejected.stdout).code, 'invalid_304_response');
	fs.writeFileSync(path.join(directory, 'headers'), `HTTP/1.1 304 Not Modified\r\nETag: ${snapshotEtag}\r\n\r\n`);
	const unsolicited = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', '', '10', '30'], directory, null, stateDirectory);
	assert.equal(unsolicited.status, 1);
	assert.equal(JSON.parse(unsolicited.stdout).code, 'invalid_304_response');
});

test('fetch rejects oversized, malformed JSON, malformed ETag and non-allowlisted status', () => {
	const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-state.'));
	for (const scenario of [
		{ status: '200', headers: `ETag: ${snapshotEtag}\r\n`, body: `{"x":"${'z'.repeat(16384)}"}`, code: 'response_too_large' },
		{ status: '200', headers: `ETag: ${snapshotEtag}\r\n`, body: '[1]', code: 'invalid_response_json' },
		{ status: '200', headers: 'ETag: W/"weak"\r\n', body: '{}', code: 'invalid_response_etag' },
		{ status: '200', headers: `X-Padding: ${'h'.repeat(16384)}\r\n`, body: '{}', code: 'response_headers_too_large' },
		{ status: '418', headers: '', body: '{}', code: 'unexpected_http_status' }
	]) {
		const directory = fixture(scenario.status, scenario.headers, scenario.body);
		const result = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', '', '10', '30'], directory, null, stateDirectory);
		assert.equal(result.status, 1, scenario.code);
		assert.equal(JSON.parse(result.stdout).code, scenario.code);
		assertPrivateTemporaryCleanup(directory);
	}
});

test('transport failures use an allowlisted code and still clean private files', () => {
	const directory = fixture('000', '', '');
	fs.writeFileSync(path.join(directory, 'exit'), '60');
	const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-state.'));
	const result = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', '', '10', '30'], directory, null, stateDirectory);
	assert.equal(result.status, 1);
	assert.deepEqual(JSON.parse(result.stdout), { ok: false, code: 'tls_verification_failed' });
	assert.equal(result.stderr, '');
	assertPrivateTemporaryCleanup(directory);
});

test('chunked GET body and headers are terminated at the 16385-byte streaming sink', () => {
	const stateDirectory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-fake-state.'));
	for (const stream of ['body', 'headers']) {
		const directory = fixture('200', `HTTP/1.1 200 OK\r\nETag: ${snapshotEtag}\r\n\r\n`, '{}');
		fs.writeFileSync(path.join(directory, `chunked-${stream}-size`), String(1024 * 1024));
		const result = runAdapter(['fetch', 'https://vpn.example', credentialFile(directory), '/etc/autovpn/test-state', '', '10', '30'], directory, null, stateDirectory);
		assert.equal(result.status, 1, stream);
		assert.equal(JSON.parse(result.stdout).code, stream === 'body' ? 'response_too_large' : 'response_headers_too_large');
		assert.equal(fs.readFileSync(path.join(directory, `${stream}-writer-terminated`), 'utf8'), 'yes');
		assert.equal(Number(fs.readFileSync(path.join(directory, `${stream}-attempted`), 'utf8')) < 1024 * 1024, true);
		assert.equal(Number(fs.readFileSync(path.join(directory, `peak-${stream}`), 'utf8')) <= 16385, true);
		assertPrivateTemporaryCleanup(directory);
	}
});

test('chunked PUT response is terminated at the same bounded sink', () => {
	const body = {
		schema_version: 1,
		revision: 41,
		snapshot_sha256: '1'.repeat(64),
		outcome: 'APPLIED',
		active_profile: 'vless-reality',
		capabilities: { vless: true, hysteria2: false, amneziawg: false, zapret: false, policy_routing: true },
		diagnostics: { stage: 'verified' }
	};
	const key = 'chunked-put-1';
	const directory = fixture('200', `HTTP/1.1 200 OK\r\nETag: ${snapshotEtag}\r\n\r\n`, '{}');
	fs.writeFileSync(path.join(directory, 'chunked-body-size'), String(1024 * 1024));
	const journal = path.join(directory, 'journal.json');
	fs.writeFileSync(journal, JSON.stringify({ pending_report: { idempotency_key: key, etag: snapshotEtag, body } }), { mode: 0o600 });
	const result = runAdapter(['put-result', 'https://vpn.example', credentialFile(directory), key, snapshotEtag, journal, '10', '30'], directory, null, directory);
	assert.equal(result.status, 1);
	assert.equal(JSON.parse(result.stdout).code, 'response_too_large');
	assert.equal(fs.readFileSync(path.join(directory, 'body-writer-terminated'), 'utf8'), 'yes');
	assert.equal(Number(fs.readFileSync(path.join(directory, 'body-attempted'), 'utf8')) < 1024 * 1024, true);
	assert.equal(Number(fs.readFileSync(path.join(directory, 'peak-body'), 'utf8')) <= 16385, true);
	assertPrivateTemporaryCleanup(directory);
});

test('PUT replays the exact pending journal body and validates the documented 200 response', () => {
	const requestBody = {
		schema_version: 1,
		revision: 41,
		snapshot_sha256: '1'.repeat(64),
		outcome: 'FAILED',
		active_profile: null,
		capabilities: { vless: false, hysteria2: false, amneziawg: false, zapret: false, policy_routing: false },
		diagnostics: { stage: 'rolled_back', code: 'activate_failed' }
	};
	const key = 'r41-replay~1';
	const response = {
		schema_version: 1,
		result_id: 7,
		idempotency_key: key,
		revision: requestBody.revision,
		snapshot_sha256: requestBody.snapshot_sha256,
		outcome: requestBody.outcome,
		active_profile: requestBody.active_profile,
		accepted_at: '2026-09-04T10:00:00Z',
		replayed: false
	};
	const directory = fixture('200', `HTTP/1.1 200 OK\r\nETag: ${snapshotEtag}\r\n\r\n`, JSON.stringify(response));
	const journal = path.join(directory, 'journal.json');
	fs.writeFileSync(journal, JSON.stringify({ pending_report: { idempotency_key: key, etag: snapshotEtag, body: requestBody } }), { mode: 0o600 });
	const result = runAdapter(['put-result', 'https://vpn.example', credentialFile(directory), key, snapshotEtag, journal, '7', '21'], directory, null, directory);
	assert.equal(result.status, 0, result.stdout);
	assert.deepEqual(JSON.parse(result.stdout), { ok: true, status: 200, replayed: false });
	const firstRequest = fs.readFileSync(path.join(directory, 'request-body.copy'), 'utf8');
	assert.equal(firstRequest, JSON.stringify(requestBody));
	assert.equal(fs.readFileSync(path.join(directory, 'request-mode'), 'utf8'), String(0o600));
	assert.equal(fs.readFileSync(path.join(directory, 'secret-in-env'), 'utf8'), 'no');
	const config = curlConfig(directory);
	assert.match(config, new RegExp(`/api/v2/router/apply-results/${key}`));
	assert.match(config, /request = "PUT"/);
	assert.match(config, /header = "Content-Type: application\/json"/);
	assert.match(config, /If-Match: \\"[a-f0-9]{64}\\"/);
	assert.match(config, /data-binary = "@\/tmp\/autovpn-http\./);
	assertPrivateTemporaryCleanup(directory);

	response.replayed = true;
	fs.writeFileSync(path.join(directory, 'body'), JSON.stringify(response));
	const replay = runAdapter(['put-result', 'https://vpn.example', credentialFile(directory), key, snapshotEtag, journal, '7', '21'], directory, null, directory);
	assert.equal(replay.status, 0, replay.stdout);
	assert.deepEqual(JSON.parse(replay.stdout), { ok: true, status: 200, replayed: true });
	assert.equal(fs.readFileSync(path.join(directory, 'request-body.copy'), 'utf8'), firstRequest);
	assertPrivateTemporaryCleanup(directory);
});

test('PUT extracts a valid small pending report from a bounded journal larger than 16 KiB', () => {
	const body = {
		schema_version: 1,
		revision: 42,
		snapshot_sha256: '2'.repeat(64),
		outcome: 'APPLIED',
		active_profile: 'vless-reality',
		capabilities: { vless: true, hysteria2: false, amneziawg: false, zapret: false, policy_routing: true },
		diagnostics: { stage: 'verified' }
	};
	const key = 'large-journal-1';
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-large-journal.'));
	const journalPath = path.join(directory, 'journal.json');
	const journal = {
		applied: { snapshot: { legacy_amnezia_vpn_import_key: 'x'.repeat(12000) } },
		last_good: { snapshot: { legacy_amnezia_vpn_import_key: 'y'.repeat(12000) } },
		pending_report: { idempotency_key: key, etag: snapshotEtag, body }
	};
	const raw = JSON.stringify(journal);
	assert.equal(Buffer.byteLength(raw) > http.MAX_BODY_BYTES, true);
	assert.equal(Buffer.byteLength(raw) < http.MAX_JOURNAL_BYTES, true);
	fs.writeFileSync(journalPath, raw, { mode: 0o600 });
	const requestPath = path.join(directory, 'request.json');
	const configPath = path.join(directory, 'curl.conf');
	const result = http.run([
		'prepare-put', 'https://vpn.example', credentialFile(directory), key, snapshotEtag,
		journalPath, '10', '30', configPath, path.join(directory, 'headers'),
		path.join(directory, 'response'), requestPath
	], {
		read(file, limit) {
			try {
				return fs.readFileSync(file).subarray(0, limit).toString();
			}
			catch (_) {
				return null;
			}
		},
		writePrivate(file, value) {
			fs.writeFileSync(file, value, { mode: 0o600 });
			return true;
		}
	});
	assert.deepEqual(result, { ok: true });
	assert.deepEqual(JSON.parse(fs.readFileSync(requestPath, 'utf8')), body);
});

test('PUT rejects mismatched journal inputs, 201, conflicts and malformed replay response', () => {
	const body = {
		schema_version: 1, revision: 1, snapshot_sha256: '1'.repeat(64), outcome: 'APPLIED', active_profile: 'vless-reality',
		capabilities: { vless: true, hysteria2: false, amneziawg: false, zapret: false, policy_routing: true }, diagnostics: { stage: 'verified' }
	};
	const key = 'apply-1';
	for (const scenario of [
		{ status: '201', response: {}, code: 'unexpected_http_status' },
		{ status: '409', response: {}, code: 'http_409' },
		{ status: '412', response: {}, code: 'http_412' },
		{ status: '200', response: { replayed: true }, code: 'invalid_apply_response' }
	]) {
		const directory = fixture(scenario.status, `ETag: ${snapshotEtag}\r\n`, JSON.stringify(scenario.response));
		const journal = path.join(directory, 'journal.json');
		fs.writeFileSync(journal, JSON.stringify({ pending_report: { idempotency_key: key, etag: snapshotEtag, body } }));
		const result = runAdapter(['put-result', 'https://vpn.example', credentialFile(directory), key, snapshotEtag, journal, '10', '30'], directory, null, directory);
		assert.equal(result.status, 1);
		assert.equal(JSON.parse(result.stdout).code, scenario.code);
		assertPrivateTemporaryCleanup(directory);
	}

	const directory = fixture('200', `ETag: ${snapshotEtag}\r\n`, '{}');
	const journal = path.join(directory, 'journal.json');
	fs.writeFileSync(journal, JSON.stringify({ pending_report: { idempotency_key: key, etag: snapshotEtag, body } }));
	const mismatch = runAdapter(['put-result', 'https://vpn.example', credentialFile(directory), 'different-key', snapshotEtag, journal, '10', '30'], directory, null, directory);
	assert.equal(mismatch.status, 1);
	assert.equal(JSON.parse(mismatch.stdout).code, 'invalid_request');
});
