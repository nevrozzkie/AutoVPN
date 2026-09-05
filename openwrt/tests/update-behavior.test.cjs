'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const {spawnSync} = require('node:child_process');
const test = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../files/usr/libexec/autovpn/update-helper'), 'utf8');
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function fixture(t, options = {}) {
	const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-update-test-'));
	t.after(() => fs.rmSync(dir, {recursive: true, force: true}));
	const bin = path.join(dir, 'bin'), root = path.join(dir, 'autovpn'), assets = path.join(dir, 'assets');
	for (const directory of [bin, root, assets, path.join(root, 'state')]) fs.mkdirSync(directory);
	for (const name of ['apk', 'curl', 'jsonfilter', 'ubus', 'uname', 'df', 'lock', 'sync', 'uci', 'ucode', 'runtime']) {
		fs.copyFileSync(path.join(__dirname, 'fake-update.cjs'), path.join(bin, name));
		fs.chmodSync(path.join(bin, name), 0o755);
	}
	const key = '-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n';
	const body = 'signed APK fixture';
	const filename = 'autovpn-controller-0.8.0-r1.apk';
	fs.writeFileSync(path.join(assets, filename), body);
	fs.writeFileSync(path.join(assets, 'manifest-25.12.5-mediatek-filogic-aarch64_cortex-a53.json'), JSON.stringify({
		schema_version: 1, release: '25.12.5', target: 'mediatek/filogic', architecture: 'aarch64_cortex-a53',
		kernel_release: '6.12.85', kernel_package: '6.12.85~fixture', signing_key_sha256: hash(key),
		packages: [{name: 'autovpn-controller', version: '0.8.0-r1', filename, sha256: hash(body)}]
	}));
	fs.writeFileSync(path.join(root, 'release-signing.pem'), key);
	fs.writeFileSync(path.join(root, 'release.json'), JSON.stringify({schema_version: 1,
		release_base: 'https://github.com/example/router/releases/download/v0.7.0',
		signing_key_sha256: hash(key), installed_version: '0.7.0-r1'}));
	const uptime = path.join(dir, 'uptime');
	fs.writeFileSync(uptime, '100.00 20.00\n');
	const rewritten = source.replaceAll('/etc/autovpn', root)
		.replaceAll('/var/lock', path.join(dir, 'locks')).replaceAll('/proc/uptime', uptime)
		.replaceAll('/usr/bin/ucode', path.join(bin, 'ucode'))
		.replaceAll('/usr/libexec/autovpn/runtime-adapter', path.join(bin, 'runtime'));
	const helper = path.join(dir, 'helper');
	fs.writeFileSync(helper, rewritten, {mode: 0o755});
	const commit = path.join(dir, 'installed'), log = path.join(dir, 'log');
	const env = {...process.env, PATH: bin + ':' + process.env.PATH, FAKE_ASSETS: assets, FAKE_COMMIT: commit, FAKE_LOG: log, ...options};
	const run = (args, extra = {}) => {
		const result = spawnSync('/bin/sh', [helper, ...args], {encoding: 'utf8', env: {...env, ...extra}, timeout: 20000});
		assert.equal(result.error, undefined);
		return result;
	};
	return {run, root, commit, gate: path.join(root, 'state/update.lock'),
		status: () => JSON.parse(run(['status']).stdout),
		calls: () => fs.readFileSync(log, 'utf8').trim().split('\n').map(JSON.parse),
		check: () => {
			const result = run(['check-worker', 'v0.8.0']);
			assert.equal(result.status, 0, result.stdout + result.stderr);
			return JSON.parse(result.stdout).id;
		}
	};
}

async function poll(f, phase) {
	for (let i = 0; i < 30; i++) {
		const state = f.status();
		if (state.phase === phase) return state;
		if (state.phase === 'failed') assert.fail(JSON.stringify(state));
		await new Promise(resolve => setTimeout(resolve, 100));
	}
	assert.fail('Update did not reach ' + phase + ': ' + JSON.stringify(f.status()));
}

test('manual check and apply queues execute real shell workers and reach ready', async t => {
	const f = fixture(t);
	assert.equal(f.run(['check', 'v0.8.0']).status, 0);
	const checked = await poll(f, 'checked');
	assert.equal(fs.existsSync(f.gate), false);
	assert.equal(f.run(['queue', checked.candidate_id]).status, 0);
	const ready = await poll(f, 'ready');
	assert.equal(ready.current_version, '0.8.0-r1');
	assert.equal(JSON.parse(fs.readFileSync(f.gate)).phase, 'ready');
	assert.equal(f.calls().filter(c => c.name === 'apk' && c.args.includes('add') && !c.args.includes('--simulate')).length, 1);
	assert.ok(f.calls().some(c => c.name === 'uci' && c.args.includes('autovpn.main.enabled=0')));
	const before = fs.readFileSync(f.gate, 'utf8');
	assert.notEqual(f.run(['check-worker', 'v0.8.0']).status, 0);
	assert.equal(fs.readFileSync(f.gate, 'utf8'), before);
});

test('unsafe dependency plan is rejected by worker before package commit', t => {
	const f = fixture(t, {FAKE_BAD_PLAN: '1'});
	const id = f.check();
	const result = f.run(['worker', id]);
	assert.notEqual(result.status, 0);
	assert.match(result.stdout, /update_dependency_plan_unsafe/);
	assert.equal(fs.existsSync(f.commit), false);
	assert.equal(JSON.parse(fs.readFileSync(f.gate)).phase, 'failed');
});

test('failed installation preserves identity/hash for an explicit successful retry', t => {
	const f = fixture(t);
	const id = f.check();
	const failed = f.run(['worker', id], {FAKE_ADD_FAIL: '1'});
	assert.notEqual(failed.status, 0);
	const gate = JSON.parse(fs.readFileSync(f.gate));
	assert.equal(gate.phase, 'failed');
	assert.equal(gate.tag, 'v0.8.0');
	assert.equal(gate.sha256.length, 64);
	const retry = f.check();
	const installed = f.run(['worker', retry]);
	assert.equal(installed.status, 0, installed.stdout + installed.stderr);
	assert.equal(f.status().phase, 'ready');
});

test('busy check and failed download cannot alter a live plan or stop VPN', t => {
	const f = fixture(t);
	f.check();
	const state = path.join(f.root, 'state/update-status.json');
	const before = fs.readFileSync(state, 'utf8');
	assert.notEqual(f.run(['check-worker', 'v0.8.0'], {FAKE_LOCK_BUSY: '1'}).status, 0);
	assert.equal(fs.readFileSync(state, 'utf8'), before);
	assert.notEqual(f.run(['check-worker', 'v0.8.0'], {FAKE_DOWNLOAD_FAIL: '1'}).status, 0);
	assert.equal(fs.existsSync(f.gate), false);
	assert.equal(f.calls().some(c => c.name === 'runtime'), false);
});

test('a same-version no-op cannot falsely repair a failed installation', t => {
	const f = fixture(t);
	const id = f.check();
	f.run(['worker', id], {FAKE_ADD_FAIL: '1'});
	fs.writeFileSync(f.commit, '0.8.0-r1');
	const retry = f.check();
	const result = f.run(['worker', retry]);
	assert.notEqual(result.status, 0);
	assert.match(result.stdout, /update_recovery_needs_newer_release/);
	assert.equal(JSON.parse(fs.readFileSync(f.gate)).phase, 'failed');
});
