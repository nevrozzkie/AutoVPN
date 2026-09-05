'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');
const zlib = require('node:zlib');

const sourcePath = path.join(__dirname, '../files/usr/libexec/autovpn/zapret-install');
const source = fs.readFileSync(sourcePath, 'utf8');
const sha256 = data => crypto.createHash('sha256').update(data).digest('hex');

function fixture(t, options = {}) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-zapret-test-'));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	const bin = path.join(root, 'bin');
	const assets = path.join(root, 'assets');
	const archiveRoot = path.join(root, 'archive/zapret2-v1.0.5');
	const targetParent = path.join(root, 'usr/lib');
	const target = path.join(targetParent, 'autovpn-zapret');
	fs.mkdirSync(bin);
	fs.mkdirSync(assets);
	fs.mkdirSync(path.join(archiveRoot, 'binaries/linux-arm64'), { recursive: true });
	fs.mkdirSync(path.join(archiveRoot, 'lua'), { recursive: true });
	fs.mkdirSync(targetParent, { recursive: true });
	const nfqws = Buffer.from('fixture static aarch64 nfqws2\n');
	const lib = Buffer.from('-- fixture zapret library\n');
	const antidpi = Buffer.from('-- fixture antidpi library\n');
	const license = Buffer.from('fixture MIT license\n');
	fs.writeFileSync(path.join(archiveRoot, 'binaries/linux-arm64/nfqws2'), nfqws);
	fs.writeFileSync(path.join(archiveRoot, 'lua/zapret-lib.lua.gz'), zlib.gzipSync(lib));
	if (!options.missingAntidpi)
		fs.writeFileSync(path.join(archiveRoot, 'lua/zapret-antidpi.lua.gz'), zlib.gzipSync(antidpi));
	const archive = path.join(assets, 'zapret2-v1.0.5-openwrt-embedded.tar.gz');
	const packed = spawnSync('tar', ['-czf', archive, '-C', path.join(root, 'archive'), 'zapret2-v1.0.5']);
	assert.equal(packed.status, 0, packed.stderr?.toString());
	fs.writeFileSync(path.join(assets, 'LICENSE.txt'), license);
	const meminfo = path.join(root, 'meminfo');
	fs.writeFileSync(meminfo, `MemAvailable:       ${options.memoryKiB || 65536} kB\n`);
	if (options.engineRunning)
		fs.writeFileSync(path.join(root, 'engine-service'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
	const log = path.join(root, 'calls.jsonl');
	const mock = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const env = process.env;
const record = value => fs.appendFileSync(env.MOCK_LOG, JSON.stringify(value) + '\\n');
if (name === 'id') process.stdout.write((env.MOCK_UID || '0') + '\\n');
else if (name === 'uname') process.stdout.write((env.MOCK_ARCH || 'aarch64') + '\\n');
else if (name === 'df') process.stdout.write('Filesystem 1024-blocks Used Available Capacity Mounted on\\nfixture 100000 1000 ' + (env.MOCK_FREE || '90000') + ' 1% /fixture\\n');
else if (name === 'stat') process.stdout.write((env.MOCK_STATE_UID || '0') + '\\n');
else if (name === 'curl') {
  const url = args.at(-1);
  const output = args[args.indexOf('--output') + 1];
  record({ name, url, args });
  if (env.MOCK_CURL_FAIL === '1') process.exit(22);
  if (env.MOCK_DELAY_MS) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(env.MOCK_DELAY_MS));
  const asset = url.includes('/releases/download/') ? 'zapret2-v1.0.5-openwrt-embedded.tar.gz' : 'LICENSE.txt';
  fs.copyFileSync(path.join(env.MOCK_ASSETS, asset), output);
}
else throw new Error('unexpected mock ' + name);
`;
	for (const name of ['id', 'uname', 'df', 'stat', 'curl'])
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	let scriptSource = source
		.replace("TARGET='/usr/lib/autovpn-zapret'", `TARGET='${target}'`)
		.replace("TARGET_PARENT='/usr/lib'", `TARGET_PARENT='${targetParent}'`)
		.replace("MEMINFO='/proc/meminfo'", `MEMINFO='${meminfo}'`)
		.replace("RAM_ROOT='/tmp/autovpn-zapret-install-state'", `RAM_ROOT='${path.join(root, 'ram-state')}'`)
		.replace("ENGINE_SERVICE='/etc/init.d/autovpn-zapret'", `ENGINE_SERVICE='${path.join(root, 'engine-service')}'`)
		.replace("ARCHIVE_SHA256='40040fef1747012a68f2dd5892b9a0bece91846e9bce37b59e35b641fdcb2a4e'", `ARCHIVE_SHA256='${sha256(fs.readFileSync(archive))}'`)
		.replace("LICENSE_SHA256='d089978dd77d53cb6aa5dff51cfdbff617e52dd44af1ac44a6df02c6644f17d5'", `LICENSE_SHA256='${sha256(license)}'`)
		.replace("NFQWS_SHA256='23267ff6d8fb3a68fdd91cf9bbd9616701e797a8ba832d77b3cfef91bcace3e4'", `NFQWS_SHA256='${sha256(nfqws)}'`)
		.replace("LIB_SHA256='b67a470f23b00a8d6e732c4e5135a39b224511e0b71809d5f4616adf62674980'", `LIB_SHA256='${sha256(lib)}'`)
		.replace("ANTIDPI_SHA256='31c9dd75b0bd55e98e5306293f2be81e9d2ecadcbbf9157394ff37dcff7dc85a'", `ANTIDPI_SHA256='${sha256(antidpi)}'`)
		.replaceAll('40040fef1747012a68f2dd5892b9a0bece91846e9bce37b59e35b641fdcb2a4e', sha256(fs.readFileSync(archive)))
		.replaceAll('d089978dd77d53cb6aa5dff51cfdbff617e52dd44af1ac44a6df02c6644f17d5', sha256(license))
		.replaceAll('23267ff6d8fb3a68fdd91cf9bbd9616701e797a8ba832d77b3cfef91bcace3e4', sha256(nfqws))
		.replaceAll('b67a470f23b00a8d6e732c4e5135a39b224511e0b71809d5f4616adf62674980', sha256(lib))
		.replaceAll('31c9dd75b0bd55e98e5306293f2be81e9d2ecadcbbf9157394ff37dcff7dc85a', sha256(antidpi));
	if (options.badArchivePin) scriptSource = scriptSource.replace(`ARCHIVE_SHA256='${sha256(fs.readFileSync(archive))}'`, `ARCHIVE_SHA256='${'0'.repeat(64)}'`);
	const script = path.join(root, 'zapret-install');
	fs.writeFileSync(script, scriptSource, { mode: 0o755 });
	return {
		root, target, log,
		run(args, env = {}) {
			const result = spawnSync('/bin/sh', [script, ...args], {
				encoding: 'utf8', timeout: 30000,
				env: { ...process.env, PATH: bin + ':' + process.env.PATH, MOCK_ASSETS: assets, MOCK_LOG: log, ...env }
			});
			assert.equal(result.error, undefined);
			const lines = result.stdout.trim().split('\n').filter(Boolean);
			assert.equal(lines.length, 1, `stdout must be one JSON line: ${result.stdout}`);
			return { ...result, json: JSON.parse(lines[0]), calls: fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [] };
		}
	};
}

async function waitForTerminal(env) {
	let status;
	for (let attempt = 0; attempt < 100; attempt++) {
		status = env.run(['status']).json;
		if (status.phase === 'ready' || status.phase === 'failed') {
			for (let cleanup = 0; cleanup < 20 && fs.existsSync(path.join(env.root, 'ram-state/job')); cleanup++)
				await new Promise(resolve => setTimeout(resolve, 25));
			return status;
		}
		await new Promise(resolve => setTimeout(resolve, 50));
	}
	assert.fail(`worker did not finish: ${JSON.stringify(status)}`);
}

test('source pins one official v1.0.5 arm64 bundle and never runs upstream installers', () => {
	assert.match(source, /VERSION='v1\.0\.5'/);
	assert.match(source, /github\.com\/bol-van\/zapret2\/releases\/download\/v1\.0\.5\/zapret2-v1\.0\.5-openwrt-embedded\.tar\.gz/);
	assert.match(source, /ARCHIVE_SHA256='40040fef1747012a68f2dd5892b9a0bece91846e9bce37b59e35b641fdcb2a4e'/);
	assert.match(source, /binaries\/linux-arm64\/nfqws2/);
	assert.match(source, /"\$\(uname -m 2>\/dev\/null\)" = aarch64/);
	assert.match(source, /MAX_ARCHIVE_KIB=5120/);
	assert.match(source, /ulimit -f "\$\(\(limit \* 2\)\)"/);
	assert.match(source, /archive 120/);
	assert.match(source, /license 30/);
	assert.doesNotMatch(source, /releases\/latest|\beval\b|install_bin\.sh|install_easy\.sh/);
	assert.ok(source.indexOf('mkdir "$candidate"') < source.indexOf('STAGE=$candidate'), 'cleanup ownership starts only after stage mkdir succeeds');
});

test('install verifies, extracts the allowlist, and publishes one complete bundle', async t => {
	const env = fixture(t);
	const result = env.run(['queue']);
	assert.equal(result.status, 0, result.stderr);
	assert.deepEqual(result.json, { ok: true, phase: 'queued', version: 'v1.0.5' });
	assert.deepEqual(await waitForTerminal(env), { ok: true, phase: 'ready', code: '', present: true, version: 'v1.0.5' });
	assert.deepEqual(fs.readdirSync(env.target).sort(), ['LICENSE.txt', 'nfqws2', 'receipt.json', 'zapret-antidpi.lua', 'zapret-lib.lua']);
	assert.equal(fs.statSync(path.join(env.target, 'nfqws2')).mode & 0o777, 0o755);
	assert.equal(fs.statSync(path.join(env.target, 'zapret-lib.lua')).mode & 0o777, 0o644);
	assert.deepEqual(env.run(['status']).calls.filter(call => call.name === 'curl').map(call => call.url), [
		'https://github.com/bol-van/zapret2/releases/download/v1.0.5/zapret2-v1.0.5-openwrt-embedded.tar.gz',
		'https://raw.githubusercontent.com/bol-van/zapret2/v1.0.5/docs/LICENSE.txt'
	]);
	const status = env.run(['status']);
	assert.deepEqual(status.json, { ok: true, phase: 'ready', code: '', present: true, version: 'v1.0.5' });
	const again = env.run(['queue']);
	assert.deepEqual(again.json, { ok: true, phase: 'ready', already_present: true, version: 'v1.0.5' });
});

test('a digest failure leaves no active or partial bundle', async t => {
	const env = fixture(t, { badArchivePin: true });
	assert.equal(env.run(['queue']).status, 0);
	assert.deepEqual(await waitForTerminal(env), { ok: true, phase: 'failed', code: 'archive_checksum_mismatch', present: false, version: 'v1.0.5' });
	assert.equal(fs.existsSync(env.target), false);
	assert.equal(fs.readdirSync(path.dirname(env.target)).some(name => name.startsWith('.autovpn-zapret-v1.0.5.')), false);
});

test('download failures leave no target and expose no downloader output', async t => {
	const env = fixture(t);
	const result = env.run(['queue'], { MOCK_CURL_FAIL: '1' });
	assert.equal(result.status, 0);
	assert.deepEqual(await waitForTerminal(env), { ok: true, phase: 'failed', code: 'archive_download_failed', present: false, version: 'v1.0.5' });
	assert.equal(fs.existsSync(env.target), false);
});

test('a missing selected archive member fails closed before staging', async t => {
	const env = fixture(t, { missingAntidpi: true });
	assert.equal(env.run(['queue']).status, 0);
	assert.deepEqual(await waitForTerminal(env), { ok: true, phase: 'failed', code: 'extract_failed', present: false, version: 'v1.0.5' });
	assert.equal(fs.existsSync(env.target), false);
});

test('queue returns immediately, rejects a duplicate, and retains ready status', async t => {
	const env = fixture(t);
	const queued = env.run(['queue'], { MOCK_DELAY_MS: '400' });
	assert.equal(queued.status, 0, queued.stderr);
	assert.deepEqual(queued.json, { ok: true, phase: 'queued', version: 'v1.0.5' });
	const duplicate = env.run(['queue']);
	assert.equal(duplicate.status, 75);
	assert.deepEqual(duplicate.json, { ok: false, code: 'install_busy' });
	let status;
	for (let attempt = 0; attempt < 80; attempt++) {
		status = env.run(['status']).json;
		if (status.phase === 'ready' || status.phase === 'failed') break;
		await new Promise(resolve => setTimeout(resolve, 50));
	}
	assert.deepEqual(status, { ok: true, phase: 'ready', code: '', present: true, version: 'v1.0.5' });
	for (let attempt = 0; attempt < 20 && fs.existsSync(path.join(env.root, 'ram-state/job')); attempt++)
		await new Promise(resolve => setTimeout(resolve, 25));
	assert.equal(fs.existsSync(path.join(env.root, 'ram-state/job')), false);
});

test('unsupported architecture and resource failures happen before download', async t => {
	const wrongArch = fixture(t);
	let result = wrongArch.run(['queue'], { MOCK_ARCH: 'x86_64' });
	assert.deepEqual(result.json, { ok: false, code: 'unsupported_architecture' });
	assert.equal(result.calls.some(call => call.name === 'curl'), false);
	const lowMemory = fixture(t, { memoryKiB: 1024 });
	result = lowMemory.run(['queue']);
	assert.equal(result.status, 0);
	assert.deepEqual(await waitForTerminal(lowMemory), { ok: true, phase: 'failed', code: 'insufficient_memory', present: false, version: 'v1.0.5' });
	assert.equal(result.calls.some(call => call.name === 'curl'), false);
});

test('an active engine is rejected before queueing or downloading', t => {
	const env = fixture(t, { engineRunning: true });
	const result = env.run(['queue']);
	assert.deepEqual(result.json, { ok: false, code: 'engine_running' });
	assert.equal(result.calls.some(call => call.name === 'curl'), false);
	assert.equal(fs.existsSync(path.join(env.root, 'ram-state/job')), false);
});

test('a non-root precreated RAM state directory is never adopted', t => {
	const env = fixture(t);
	const state = path.join(env.root, 'ram-state');
	fs.mkdirSync(state);
	fs.writeFileSync(path.join(state, 'owner'), 'autovpn-zapret-install-v1\n');
	const result = env.run(['queue'], { MOCK_STATE_UID: '501' });
	assert.deepEqual(result.json, { ok: false, code: 'unsafe_install_state' });
	assert.equal(fs.existsSync(path.join(state, 'job')), false);
});

test('foreign directories and symlink targets are rejected without modification', t => {
	for (const kind of ['foreign', 'symlink']) {
		const env = fixture(t);
		if (kind === 'foreign') {
			fs.mkdirSync(env.target);
			fs.writeFileSync(path.join(env.target, 'keep'), 'user data');
		} else {
			fs.symlinkSync(path.join(env.root, 'elsewhere'), env.target);
		}
		const result = env.run(['queue']);
		assert.deepEqual(result.json, { ok: false, code: 'unsafe_existing_bundle' });
		assert.equal(result.calls.some(call => call.name === 'curl'), false);
		if (kind === 'foreign') assert.equal(fs.readFileSync(path.join(env.target, 'keep'), 'utf8'), 'user data');
		else assert.equal(fs.lstatSync(env.target).isSymbolicLink(), true);
	}
});

test('a modified owned bundle is rejected and preserved', async t => {
	const env = fixture(t);
	assert.equal(env.run(['queue']).status, 0);
	assert.equal((await waitForTerminal(env)).phase, 'ready');
	const changed = '-- locally changed\n';
	fs.writeFileSync(path.join(env.target, 'zapret-lib.lua'), changed);
	const downloads = env.run(['status']).calls.filter(call => call.name === 'curl').length;
	const result = env.run(['queue']);
	assert.deepEqual(result.json, { ok: false, code: 'unsafe_existing_bundle' });
	assert.equal(fs.readFileSync(path.join(env.target, 'zapret-lib.lua'), 'utf8'), changed);
	assert.equal(result.calls.filter(call => call.name === 'curl').length, downloads);
});

test('arguments are data, not shell syntax, and every response is sanitized JSON', t => {
	const env = fixture(t);
	const marker = path.join(env.root, 'injected');
	for (const args of [[`queue;touch ${marker}`], ['queue', marker], [], ['--help'], ['worker']]) {
		const result = env.run(args);
		assert.notEqual(result.status, 0);
		assert.match(result.json.code, /^invalid_(action|arguments|worker_invocation)$/);
	}
	assert.equal(fs.existsSync(marker), false);
	const state = path.join(env.root, 'ram-state');
	fs.mkdirSync(state);
	fs.writeFileSync(path.join(state, 'owner'), 'autovpn-zapret-install-v1\n');
	fs.writeFileSync(path.join(state, 'status'), 'failed:bad\"},\"injected\":true\n');
	assert.deepEqual(env.run(['status']).json, { ok: true, phase: 'failed', code: 'invalid_install_state', present: false, version: 'v1.0.5' });
});
