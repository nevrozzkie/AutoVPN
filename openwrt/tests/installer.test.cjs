'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../scripts/install.sh'), 'utf8');
const hash = data => crypto.createHash('sha256').update(data).digest('hex');

function fixture(t, options = {}) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-installer-test-'));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	const bin = path.join(root, 'bin');
	const assets = path.join(root, 'assets');
	const etc = path.join(root, 'etc-apk');
	fs.mkdirSync(bin);
	fs.mkdirSync(assets);
	fs.mkdirSync(path.join(etc, 'repositories.d'), { recursive: true });
	fs.mkdirSync(path.join(etc, 'keys'));
	fs.writeFileSync(path.join(etc, 'repositories.d/distfeeds.list'),
		'https://downloads.openwrt.org/releases/25.12.5/targets/mediatek/filogic/packages/packages.adb\n' +
		'https://downloads.openwrt.org/releases/25.12.5/packages/aarch64_cortex-a53/base/packages.adb\n' +
		'https://untrusted.invalid/feed/packages.adb\n' +
		'https://downloads.openwrt.org.evil.invalid/feed\n' +
		'https://downloads.openwrt.org/releases/24.10.0/feed\n');
	fs.writeFileSync(path.join(etc, 'keys/openwrt.pem'), 'official fixture key');
	const key = '-----BEGIN PUBLIC KEY-----\nfixture\n-----END PUBLIC KEY-----\n';
	fs.writeFileSync(path.join(assets, 'autovpn-signing.pem'), key);
	const names = options.awg ? ['autovpn-controller', 'amneziawg-tools', 'kmod-amneziawg'] : ['autovpn-controller'];
	const packages = names.map(name => {
		const data = 'test signed APK: ' + name;
		const filename = name + '-0.6.0-r1.apk';
		fs.writeFileSync(path.join(assets, filename), data);
		return { name, filename, sha256: hash(data) };
	});
	const manifest = {
		schema_version: 1, version: '0.6.0', release: '25.12.5',
		target: 'mediatek/filogic', architecture: 'aarch64_cortex-a53',
		kernel_release: '6.12.85', kernel_package: '6.12.85~fixture-r1',
		min_free_kib: 20000, min_tmp_kib: 32000,
		signing_key: 'autovpn-signing.pem', signing_key_sha256: hash(key),
		packages, capabilities: { amneziawg: !!options.awg }
	};
	if (options.mutate) options.mutate(manifest);
	const encoded = JSON.stringify(manifest);
	const manifestName = 'manifest-25.12.5-mediatek-filogic-aarch64_cortex-a53.json';
	fs.writeFileSync(path.join(assets, manifestName), encoded);
	const installer = source
		.replace('@AUTOVPN_RELEASE_BASE@', 'https://github.com/example/router/releases/download/v0.6.0')
		.replace('@AUTOVPN_SIGNING_KEY_SHA256@', hash(key))
		.replace('@AUTOVPN_MANIFEST_SHA256@', options.badManifestHash ? '0'.repeat(64) : hash(encoded))
		.replaceAll('/etc/apk/', etc + '/')
		.replaceAll('/lib/apk/', path.join(root, 'lib-apk') + '/');
	const script = path.join(root, 'install.sh');
	fs.writeFileSync(script, installer);
	const mock = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const env = process.env;
const log = item => fs.appendFileSync(env.MOCK_LOG, JSON.stringify(item) + '\\n');
const output = value => process.stdout.write(typeof value === 'string' ? value + '\\n' : JSON.stringify(value));
if (name === 'id') output('0');
else if (name === 'uname') output(env.MOCK_KERNEL || '6.12.85');
else if (name === 'df') output('Filesystem 1024-blocks Used Available Capacity Mounted on\\nfixture 100000 1000 ' + (env.MOCK_FREE || '90000') + ' 1% /fixture');
else if (name === 'ubus') output({release:{version:'25.12.5',distribution:'OpenWrt',target:'mediatek/filogic'}});
else if (name === 'jsonfilter') {
  try {
    let value = JSON.parse(fs.readFileSync(args[args.indexOf('-i')+1], 'utf8'));
    const expr = args[args.indexOf('-e')+1];
    for (const part of expr.slice(1).match(/([a-z_][a-z0-9_]*|[0-9]+)/g) || []) value = value[part];
    if (value === undefined) process.exit(1);
    output(value);
  } catch (e) { process.exit(1); }
} else if (name === 'curl') {
  const url = args.at(-1);
  log({name,args});
  if (env.MOCK_DOWNLOAD_FAIL === '1') process.exit(22);
  fs.copyFileSync(path.join(env.MOCK_ASSETS, path.basename(url)), args[args.indexOf('--output')+1]);
} else if (name === 'apk') {
  if (args.includes('--print-arch')) output('aarch64_cortex-a53');
  else if (args.includes('query')) output([{name:'kernel',version:'6.12.85~fixture-r1'}]);
  else {
    const entry = {name,args};
    if (args.includes('--repositories-file')) entry.repositories = fs.readFileSync(args[args.indexOf('--repositories-file')+1], 'utf8');
    log(entry);
    if (args.includes('verify') && env.MOCK_BAD_SIGNATURE === '1') process.exit(1);
    if (args.includes('--simulate') && env.MOCK_BAD_PLAN === '1') process.exit(1);
    if (args.includes('add') && !args.includes('--simulate') && env.MOCK_BAD_COMMIT === '1') process.exit(1);
    output('OK fixture APK');
  }
} else throw new Error('Unexpected mock tool: ' + name);
`;
	for (const name of ['id', 'uname', 'df', 'ubus', 'jsonfilter', 'curl', 'apk']) {
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	}
	const logFile = path.join(root, 'calls.jsonl');
	return {
		run(args = [], env = {}) {
			const result = spawnSync('/bin/sh', [script, ...args], {
				encoding: 'utf8', timeout: 30000,
				env: { ...process.env, PATH: bin + ':' + process.env.PATH, MOCK_ASSETS: assets, MOCK_LOG: logFile, ...env }
			});
			assert.equal(result.error, undefined);
			const calls = fs.existsSync(logFile) ? fs.readFileSync(logFile, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
			return { ...result, calls };
		}
	};
}

function noCommit(result) {
	assert.equal(result.calls.some(call => call.name === 'apk' && call.args.includes('add') && !call.args.includes('--simulate')), false);
}

test('source template cannot execute an unpublished installer', () => {
	const result = spawnSync('/bin/sh', ['-c', source], { encoding: 'utf8' });
	assert.notEqual(result.status, 0);
	assert.match(result.stderr, /unpublished source template/);
});

test('check-only verifies signatures and simulates without installing; custom feeds excluded', t => {
	const result = fixture(t).run(['--check']);
	assert.equal(result.status, 0, result.stderr);
	assert.match(result.stdout, /nothing installed/);
	noCommit(result);
	assert.ok(result.calls.some(call => call.args.includes('verify')));
	const plans = result.calls.filter(call => call.repositories);
	assert.ok(plans.length >= 2);
	for (const call of plans) {
		assert.doesNotMatch(call.repositories, /untrusted|evil|24\.10/);
		assert.match(call.repositories, /downloads\.openwrt\.org\/releases\/25\.12\.5/);
	}
});

test('install commits exact signed package set with kernel pinned, without configuring Wi-Fi', t => {
	const result = fixture(t, { awg: true }).run();
	assert.equal(result.status, 0, result.stderr);
	const commits = result.calls.filter(call => call.name === 'apk' && call.args.includes('add') && !call.args.includes('--simulate'));
	assert.equal(commits.length, 1);
	assert.ok(commits[0].args.includes('kernel=6.12.85~fixture-r1'));
	assert.equal(commits[0].args.filter(arg => arg.endsWith('.apk')).length, 3);
	assert.match(result.stdout, /LuCI.*Setup/);
	assert.doesNotMatch(source, /uci (set|commit)|sysupgrade|mtd write|--allow-untrusted|--force-overwrite/);
});

for (const [description, options, env, message] of [
	['manifest tampering', { badManifestHash: true }, {}, /Checksum mismatch/],
	['kernel mismatch', {}, { MOCK_KERNEL: 'different' }, /Kernel release mismatch/],
	['insufficient space', {}, { MOCK_FREE: '100' }, /Not enough writable flash/],
	['bad package hash', { mutate: m => { m.packages[0].sha256 = '0'.repeat(64); } }, {}, /Checksum mismatch/],
	['signature rejection', {}, { MOCK_BAD_SIGNATURE: '1' }, /Invalid package signature/],
	['dependency rejection', {}, { MOCK_BAD_PLAN: '1' }, /dependency\/ABI check failed/],
	['partial download failure', {}, { MOCK_DOWNLOAD_FAIL: '1' }, /Download failed/],
	['unsafe filename', { mutate: m => { m.packages[0].filename = '../foreign.apk'; } }, {}, /Unsafe package filename/],
	['incomplete AWG release', { mutate: m => { m.capabilities.amneziawg = true; } }, {}, /Inconsistent AmneziaWG/],
]) {
	test(description + ' fails before package commit', t => {
		const result = fixture(t, options).run([], env);
		assert.notEqual(result.status, 0);
		assert.match(result.stderr, message);
		noCommit(result);
	});
}

test('APK commit failure is not reported as installed', t => {
	const result = fixture(t).run([], { MOCK_BAD_COMMIT: '1' });
	assert.notEqual(result.status, 0);
	assert.match(result.stderr, /Package installation failed/);
	assert.doesNotMatch(result.stdout, /Installed\. Open LuCI/);
});
