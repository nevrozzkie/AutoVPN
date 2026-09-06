'use strict';

const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join, resolve } = require('node:path');
const test = require('node:test');

const root = resolve(__dirname, '..');
const rpcSource = readFileSync(join(root, 'files/usr/share/rpcd/ucode/luci.autovpn'), 'utf8')
	.replace(/^#![^\n]*\n/, '')
	.replace(/^import .*?;\s*$/gm, '')
	.replace("const processRunner = require('autovpn.process');", 'const processRunner = injectedProcess;');

function ucodeType(value) {
	if (value === null || value === undefined) return null;
	if (Array.isArray(value)) return 'array';
	if (typeof value === 'number') return Number.isInteger(value) ? 'int' : 'double';
	if (typeof value === 'boolean') return 'bool';
	return typeof value;
}

function fixture(options = {}) {
	const files = new Map(Object.entries(options.files || {}));
	const calls = [];
	const nonce = 'nonce-0123456789';
	const maintenanceResult = '/etc/autovpn/state/maintenance-result.' + nonce + '.json';
	const behavior = options.behavior || (() => ({ status: 0, output: JSON.stringify({ ok: true }) }));
	const processRunner = {
		popen(argv, mode) {
			const call = { argv: [...argv], mode, input: '' };
			calls.push(call);
			const response = behavior(call, files, maintenanceResult);
			if (response === null) return null;
			return {
				read: () => response.output || '',
				write: value => {
					call.input += value;
					return response.wrote === undefined ? Buffer.byteLength(value) : response.wrote;
				},
				close: () => {
					if (response.result !== undefined) files.set(maintenanceResult, response.result);
					return response.status === undefined ? 0 : response.status;
				}
			};
		}
	};
	const module = new Function(
		'readfile', 'unlink', 'injectedProcess', 'type', 'length', 'match', 'json', 'sprintf', 'push',
		rpcSource
	)(
		path => files.get(path) ?? null,
		path => files.delete(path),
		processRunner,
		ucodeType,
		value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
		(value, expression) => value.match(expression),
		JSON.parse,
		(format, value) => {
			if (format !== '%J\n') throw new Error('unexpected sprintf format ' + format);
			return JSON.stringify(value) + '\n';
		},
		(array, value) => array.push(value)
	);
	return { api: module['luci.autovpn'], calls, files, nonce, maintenanceResult };
}

function maintenanceRequest(nonce, extra = {}) {
	return {
		args: {
			nonce,
			action: 'rotate',
			base_url: 'https://vpn.example',
			router_id: 'router_123',
			credential: 'avrt_abcdefgh.' + 's'.repeat(43),
			confirmation: '',
			...extra
		}
	};
}

test('setup RPC reports controller contention instead of a missing setup result', () => {
	const env = fixture({ behavior: call => call.mode === 'r'
		? { output: JSON.stringify({ l3_device: 'pppoe-wan' }) }
		: { status: 75 } });
	const result = env.api.setup_configure.call(maintenanceRequest(env.nonce));
	assert.deepEqual(result, { ok: false, code: 'busy' });
	assert.equal(env.files.size, 0);
});

test('maintenance RPC sends the credential only through stdin, never argv or its result', () => {
	const env = fixture({
		behavior: () => ({ status: 0, result: JSON.stringify({ ok: true, rotated: true }) })
	});
	const request = maintenanceRequest(env.nonce);
	const response = env.api.maintenance_action.call(request);
	const call = env.calls[0];

	assert.deepEqual(response, { ok: true, rotated: true });
	assert.deepEqual(call.argv, ['/usr/sbin/autovpnctl', 'maintenance', env.nonce]);
	assert.equal(call.mode, 'w');
	assert.match(call.input, new RegExp(request.args.credential));
	assert.doesNotMatch(JSON.stringify(call.argv), new RegExp(request.args.credential));
	assert.doesNotMatch(JSON.stringify(response), new RegExp(request.args.credential));
});

test('maintenance RPC discards a stale nonce result before invoking its child', () => {
	const nonce = 'nonce-0123456789';
	const stalePath = '/etc/autovpn/state/maintenance-result.' + nonce + '.json';
	const env = fixture({
		files: { [stalePath]: JSON.stringify({ ok: true, rotated: true }) },
		behavior: () => ({ status: 1, wrote: 0 })
	});

	assert.deepEqual(env.api.maintenance_action.call(maintenanceRequest(nonce)), {
		ok: false, code: 'maintenance_result_missing'
	});
	assert.equal(env.files.has(stalePath), false);
});

test('a non-zero maintenance child cannot claim success through a result file', () => {
	const env = fixture({
		behavior: () => ({ status: 1, result: JSON.stringify({ ok: true, reset: true }) })
	});

	assert.deepEqual(env.api.maintenance_action.call(maintenanceRequest(env.nonce, {
		action: 'reset', confirmation: 'RESET', credential: ''
	})), { ok: false, code: 'maintenance_failed' });
});

test('maintenance RPC limits nonce/action before it opens a process', () => {
	const env = fixture();
	assert.deepEqual(env.api.maintenance_action.call(maintenanceRequest('short')), {
		ok: false, code: 'invalid_nonce'
	});
	assert.deepEqual(env.api.maintenance_action.call(maintenanceRequest('x'.repeat(49))), {
		ok: false, code: 'invalid_nonce'
	});
	assert.deepEqual(env.api.maintenance_action.call(maintenanceRequest(env.nonce, { action: 'delete-all' })), {
		ok: false, code: 'invalid_maintenance_action'
	});
	assert.equal(env.calls.length, 0);
});

test('update RPC validates candidate identifiers and tags and dispatches only fixed helper actions', () => {
	const env = fixture();
	assert.deepEqual(env.api.update_check.call({ args: { tag: '../bad' } }), {
		ok: false, code: 'invalid_update_tag'
	});
	assert.deepEqual(env.api.update_apply.call({ args: { candidate_id: 'not-an-id' } }), {
		ok: false, code: 'invalid_update_id'
	});
	assert.equal(env.calls.length, 0);

	env.api.update_check.call({ args: { tag: 'v0.6.0' } });
	env.api.update_apply.call({ args: { candidate_id: '123-deadbeefcafe' } });
	env.api.maintenance_resume.call({ args: {} });
	assert.deepEqual(env.calls.map(call => call.argv), [
		['/usr/sbin/autovpnctl', 'update-check', 'v0.6.0'],
		['/usr/sbin/autovpnctl', 'update-apply', '123-deadbeefcafe'],
		['/usr/sbin/autovpnctl', 'maintenance-resume']
	]);
});

function runControllerGate(command, lstatResult, lstatError = 'No such file or directory') {
	const controllerPath = join(root, 'files/usr/libexec/autovpn/controller.uc');
	const events = [];
	const output = [];
	const source = readFileSync(controllerPath, 'utf8')
		.replace(/^#![^\n]*\n/, '')
		.replace(/^import .*?;\s*$/gm, '')
		.replace("const stateMachine = require('autovpn.state');", 'const stateMachine = injectedState;')
		.replace("const journal = require('autovpn.journal');", 'const journal = injectedJournal;')
		.replace("const orchestration = require('autovpn.orchestration');", 'const orchestration = injectedOrchestration;')
		.replace("const processRunner = require('autovpn.process');", 'const processRunner = injectedProcess;')
		/* Ucode `for (let value in array)` iterates values.  Node iterates indexes. */
		.replace("for (let path in ['/etc/autovpn/state/maintenance.lock', '/etc/autovpn/state/update.lock'])", "for (let path of ['/etc/autovpn/state/maintenance.lock', '/etc/autovpn/state/update.lock'])");
	const stopped = {};
	try {
		new Function(
			'access', 'chmod', 'mkdir', 'readfile', 'rename', 'unlink', 'writefile', 'lstat', 'fsError', 'cursor',
			'injectedState', 'injectedJournal', 'injectedOrchestration', 'injectedProcess', 'type', 'length', 'match', 'push', 'json', 'sprintf', 'substr', 'rindex', 'printf', 'exit', 'ARGV',
			source
		)(
			() => true, () => true, () => true, () => null, () => true, () => true, () => true,
			path => { events.push(['lstat', path]); return lstatResult; },
			() => lstatError,
			() => ({ load: () => true, get: () => '' }),
			{ initialState: () => ({ phase: 'NORMAL' }), validateSnapshot: () => true, normalizeCapabilities: value => value },
			{ load: () => { events.push(['journal.load']); return { phase: 'NORMAL' }; }, store: () => true },
			{
				boundedInteger: (_, fallback) => fallback,
				safePersistentPath: (_, fallback) => fallback,
				guardJournal: () => { events.push(['guardJournal']); return { ok: false, code: 'normal_path' }; }
			},
			{ popen: argv => {
				events.push(['popen', ...argv]);
				return { read: () => JSON.stringify({ ok: true }), close: () => 0 };
			} },
			ucodeType,
			value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
			(value, expression) => value.match(expression),
			(array, value) => array.push(value), JSON.parse,
			(format, value) => format === '%J\n' ? JSON.stringify(value) + '\n' : JSON.stringify(value),
			(value, start, count) => count === undefined ? value.substring(start) : value.substring(start, start + count),
			value => value.lastIndexOf('/'),
			(format, value) => output.push(format === '%J\n' ? JSON.stringify(value) : String(value)),
			code => { stopped.code = code; throw stopped; },
			[command]
		);
	} catch (error) {
		if (error !== stopped) throw error;
	}
	return { events, output: JSON.parse(output[0]), exitCode: stopped.code };
}

test('controller maintenance gate blocks journal work and redacts status before normal startup', () => {
	const result = runControllerGate('status', { type: 'file' });
	assert.deepEqual(result.output, {
		ok: true, code: 'maintenance_locked', phase: 'MAINTENANCE',
		runtime_running: false, runtime_error: 'maintenance_locked', router_id: '', credential_configured: true
	});
	assert.equal(result.exitCode, 0);
	assert.equal(result.events.some(event => event[0] === 'journal.load' || event[0] === 'guardJournal'), false);
	assert.equal(result.events.some(event => event[0] === 'popen'), false);
	assert.equal('desired' in result.output || 'applied' in result.output || 'last_good' in result.output, false);
});

test('controller maintenance gate rejects mutations, but stop still fail-closes without a journal', () => {
	for (const command of ['refresh', 'recover', 'apply-policy']) {
		const result = runControllerGate(command, { type: 'file' });
		assert.equal(result.output.ok, false);
		assert.equal(result.output.code, 'maintenance_locked');
		assert.equal(result.exitCode, 1);
		assert.equal(result.events.some(event => event[0] === 'journal.load' || event[0] === 'popen'), false);
	}
	const stopped = runControllerGate('stop', { type: 'symlink' });
	assert.deepEqual(stopped.events.filter(event => event[0] === 'popen'), [
		['popen', '/usr/libexec/autovpn/runtime-adapter', 'fail-closed']
	]);
	assert.equal(stopped.exitCode, 0);
	assert.equal(stopped.events.some(event => event[0] === 'journal.load' || event[0] === 'guardJournal'), false);
});

test('controller permits only definite ENOENT gates; lstat errors remain fail-closed', () => {
	const absent = runControllerGate('status', null);
	assert.equal(absent.events.some(event => event[0] === 'journal.load'), true);
	assert.equal(absent.events.some(event => event[0] === 'guardJournal'), true);
	assert.equal(absent.output.code, 'normal_path');
	assert.equal(absent.exitCode, 65);

	const unreadable = runControllerGate('status', null, 'Permission denied');
	assert.equal(unreadable.output.code, 'maintenance_locked');
	assert.equal(unreadable.exitCode, 0);
	assert.equal(unreadable.events.some(event => event[0] === 'journal.load' || event[0] === 'guardJournal'), false);
});
