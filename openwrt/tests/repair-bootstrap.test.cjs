'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const template = fs.readFileSync(path.join(__dirname, '../scripts/repair-bootstrap.sh'), 'utf8');
const digest = value => crypto.createHash('sha256').update(value).digest('hex');

function fixture(t, options = {}) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-repair-test-'));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	const bin = path.join(root, 'bin');
	const assets = path.join(root, 'assets');
	const trust = path.join(root, 'etc-autovpn');
	const apkRoot = path.join(root, 'etc-apk');
	const libApk = path.join(root, 'lib-apk');
	for (const directory of [bin, assets, trust, apkRoot, libApk]) fs.mkdirSync(directory, { recursive: true });
	fs.writeFileSync(path.join(apkRoot, 'arch'), options.archFile || 'aarch64_cortex-a53\n');

	const key = '-----BEGIN PUBLIC KEY-----\nexisting fixture key\n-----END PUBLIC KEY-----\n';
	const keyHash = digest(key);
	const controller = 'signed controller fixture';
	const controllerName = 'autovpn-controller-0.14.1-r1.apk';
	fs.writeFileSync(path.join(assets, controllerName), controller);
	for (const name of ['kmod-amneziawg-6.12.85-r1.apk', 'amneziawg-tools-1-r1.apk']) {
		fs.writeFileSync(path.join(assets, name), 'must never be downloaded');
	}
	const manifestName = 'manifest-25.12.5-mediatek-filogic-aarch64_cortex-a53.json';
	const manifest = {
		schema_version: 1,
		release: options.manifestRelease || '25.12.5',
		target: options.manifestTarget || 'mediatek/filogic',
		architecture: options.manifestArchitecture || 'aarch64_cortex-a53',
		kernel_release: options.manifestKernelRelease || '6.12.85',
		kernel_package: options.manifestKernelPackage || '6.12.85~fixture-r1',
		signing_key_sha256: options.manifestKeyHash || keyHash,
		packages: [
			{ name: 'autovpn-controller', filename: controllerName, sha256: digest(controller), version: '0.14.1-r1' },
			{ name: 'amneziawg-tools', filename: 'amneziawg-tools-1-r1.apk', sha256: '1'.repeat(64) },
			{ name: 'kmod-amneziawg', filename: 'kmod-amneziawg-6.12.85-r1.apk', sha256: '2'.repeat(64) },
		],
	};
	if (options.mutateManifest) options.mutateManifest(manifest);
	const manifestBytes = JSON.stringify(manifest);
	fs.writeFileSync(path.join(assets, manifestName), manifestBytes);

	const receipt = {
		schema_version: 1,
		release_base: options.receiptBase || 'https://github.com/example/AutoVPN/releases/download/router-v0.14.0-openwrt-25.12.5-r1',
		signing_key_sha256: options.receiptKeyHash || keyHash,
		installed_version: options.receiptVersion || '0.14.0-r3',
	};
	fs.writeFileSync(path.join(trust, 'release.json'), JSON.stringify(receipt) + '\n', { mode: 0o600 });
	fs.writeFileSync(path.join(trust, 'release-signing.pem'), options.keyBytes || key, { mode: 0o600 });
	const receiptBefore = fs.readFileSync(path.join(trust, 'release.json'));
	const keyBefore = fs.readFileSync(path.join(trust, 'release-signing.pem'));

	const updateLock = path.join(root, 'autovpn-update.lock');
	const controllerLock = path.join(root, 'autovpn-controller.lock');
	const helper = path.join(bin, 'install-wifi');
	let source = template
		.replace('@AUTOVPN_RELEASE_BASE@', 'https://github.com/example/AutoVPN/releases/download/router-v0.14.1-openwrt-25.12.5-r1')
		.replace('@AUTOVPN_SIGNING_KEY_SHA256@', keyHash)
		.replace('@AUTOVPN_MANIFEST_SHA256@', options.badManifestPin ? '0'.repeat(64) : digest(manifestBytes))
		.replaceAll('/etc/autovpn', trust)
		.replaceAll('/etc/apk/', apkRoot + '/')
		.replaceAll('/lib/apk/', libApk + '/')
		.replace('UPDATE_LOCK=/var/lock/autovpn-update.lock', `UPDATE_LOCK=${updateLock}`)
		.replace('CONTROLLER_LOCK=/var/lock/autovpn-controller.lock', `CONTROLLER_LOCK=${controllerLock}`)
		.replace('WIFI_HELPER=/usr/libexec/autovpn/install-wifi', `WIFI_HELPER=${helper}`);
	const script = path.join(root, 'repair-bootstrap.sh');
	fs.writeFileSync(script, source, { mode: 0o700 });

	const mock = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const env = process.env;
const log = item => fs.appendFileSync(env.MOCK_LOG, JSON.stringify(item) + '\\n');
const output = value => process.stdout.write(typeof value === 'string' ? value + '\\n' : JSON.stringify(value) + '\\n');
log({name,args});
if (name === 'id') output('0');
else if (name === 'uname') output(env.MOCK_KERNEL_RELEASE || '6.12.85');
else if (name === 'df') output('Filesystem 1024-blocks Used Available Capacity Mounted on\\nfixture 100000 1 ' + (env.MOCK_FREE || '90000') + ' 1% /fixture');
else if (name === 'lock') {
  const target = args.at(-1), held = target + '.held';
  if (args[0] === '-n') {
    if (env.MOCK_BUSY_LOCK === path.basename(target) || fs.existsSync(held)) process.exit(1);
    fs.writeFileSync(held, 'held');
  } else if (args[0] === '-u') fs.rmSync(held, {force:true});
  else process.exit(2);
}
else if (name === 'uci') {
  if (args[0] === '-q' && args[1] === 'get') {
    const key = args[2];
    const values = {
      'autovpn.main.enabled': env.MOCK_ENABLED ?? '0',
      'autovpn.main.setup_prepared': env.MOCK_SETUP_PREPARED ?? '',
      'autovpn.main.base_url': env.MOCK_BASE_URL ?? '',
      'autovpn.main.router_id': env.MOCK_ROUTER_ID ?? '',
      'autovpn.main.credential_file': env.MOCK_CREDENTIAL_FILE ?? env.MOCK_DEFAULT_CREDENTIAL,
      'autovpn.wifi.bootstrap_completed': env.MOCK_WIFI_COMPLETE ?? '',
    };
    if (values[key]) output(values[key]); else process.exit(1);
  } else if (args[0] === '-q' && args[1] === 'changes') {
    if (env.MOCK_DIRTY_UCI === args[2]) output(args[2] + '.fixture=value');
  } else process.exit(90);
}
else if (name === 'ubus') output({release:{version:'25.12.5',distribution:'OpenWrt',target:'mediatek/filogic'}});
else if (name === 'jsonfilter') {
  try {
    let value = JSON.parse(fs.readFileSync(args[args.indexOf('-i') + 1], 'utf8'));
    const expression = args[args.indexOf('-e') + 1];
    for (const part of expression.slice(1).match(/([a-z_][a-z0-9_]*|[0-9]+)/g) || []) value = value[part];
    if (value === undefined) process.exit(1);
    output(value);
  } catch (_) { process.exit(1); }
}
else if (name === 'curl') {
  const url = args.at(-1);
  fs.copyFileSync(path.join(env.MOCK_ASSETS, path.basename(url)), args[args.indexOf('--output') + 1]);
}
else if (name === 'apk') {
  if (args.includes('query') && args.includes('kernel')) {
    output([{name:'kernel',version:'6.12.85~fixture-r1',arch:env.MOCK_KERNEL_ARCH || 'aarch64_cortex-a53'}]);
  } else if (args.includes('query') && args.includes('autovpn-controller')) {
    const version = fs.existsSync(env.MOCK_INSTALLED_FILE) ? fs.readFileSync(env.MOCK_INSTALLED_FILE, 'utf8') : (env.MOCK_CURRENT || '0.14.0-r3');
    output([{name:'autovpn-controller',version}]);
  } else if (args.includes('verify')) {
    if (env.MOCK_BAD_SIGNATURE === '1') process.exit(1);
  } else if (args[0] === 'adbdump') {
    output({info:{name:env.MOCK_APK_NAME || 'autovpn-controller',version:env.MOCK_APK_VERSION || '0.14.1-r1',arch:env.MOCK_APK_ARCH || 'noarch'}});
  } else if (args.includes('--simulate')) {
    if (env.MOCK_UNSAFE_PLAN === '1') output('(1/2) Upgrading autovpn-controller (0.14.0-r3 -> 0.14.1-r1)\\n(2/2) Installing surprise (1-r1)');
    else output('(1/1) Upgrading autovpn-controller (0.14.0-r3 -> 0.14.1-r1)');
  } else if (args.includes('add')) {
    if (env.MOCK_COMMIT_FAIL === '1') process.exit(1);
    fs.writeFileSync(env.MOCK_INSTALLED_FILE, '0.14.1-r1');
  } else process.exit(91);
}
else if (name === 'install-wifi') {
  log({name:'wifi-state',locksHeld:fs.existsSync(env.MOCK_UPDATE_LOCK + '.held') || fs.existsSync(env.MOCK_CONTROLLER_LOCK + '.held')});
  if (env.MOCK_WIFI_FAIL === '1') process.exit(1);
}
else throw new Error('unexpected mock tool ' + name);
`;
	for (const name of ['id', 'uname', 'df', 'lock', 'uci', 'ubus', 'jsonfilter', 'curl', 'apk', 'install-wifi']) {
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	}
	const logFile = path.join(root, 'calls.jsonl');
	const installedFile = path.join(root, 'installed-version');
	return {
		root, trust, script, receiptBefore, keyBefore, updateLock, controllerLock,
		run(env = {}) {
			fs.rmSync(logFile, { force: true });
			const result = spawnSync('/bin/sh', [script], {
				encoding: 'utf8', timeout: 30000,
				env: {
					...process.env,
					PATH: bin + ':' + process.env.PATH,
					MOCK_LOG: logFile,
					MOCK_ASSETS: assets,
					MOCK_INSTALLED_FILE: installedFile,
					MOCK_DEFAULT_CREDENTIAL: path.join(trust, 'credentials'),
					MOCK_UPDATE_LOCK: updateLock,
					MOCK_CONTROLLER_LOCK: controllerLock,
					...env,
				},
			});
			assert.equal(result.error, undefined);
			const calls = fs.existsSync(logFile)
				? fs.readFileSync(logFile, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse)
				: [];
			return { ...result, calls };
		},
	};
}

function committed(result) {
	return result.calls.some(call => call.name === 'apk' && call.args.includes('add') && !call.args.includes('--simulate'));
}

test('source recovery template cannot run before release pins are injected', () => {
	const result = spawnSync('/bin/sh', ['-c', template], { encoding: 'utf8' });
	assert.notEqual(result.status, 0);
	assert.match(result.stderr, /unpublished source template/);
});

test('repairs only the controller, preserves trust and UCI, then releases both locks before Wi-Fi', t => {
	const f = fixture(t);
	const result = f.run();
	assert.equal(result.status, 0, result.stderr);
	const downloads = result.calls.filter(call => call.name === 'curl').map(call => path.basename(call.args.at(-1)));
	assert.deepEqual(downloads, [
		'manifest-25.12.5-mediatek-filogic-aarch64_cortex-a53.json',
		'autovpn-controller-0.14.1-r1.apk',
	]);
	const plans = result.calls.filter(call => call.name === 'apk' && call.args.includes('--simulate'));
	const commits = result.calls.filter(call => call.name === 'apk' && call.args.includes('add') && !call.args.includes('--simulate'));
	assert.equal(plans.length, 1);
	assert.equal(commits.length, 1);
	for (const call of [...plans, ...commits]) {
		assert.ok(call.args.includes('--no-network'));
		assert.equal(call.args.filter(arg => arg.endsWith('.apk')).length, 1);
		assert.match(call.args.find(arg => arg.endsWith('.apk')), /autovpn-controller-/);
	}
	assert.equal(result.calls.some(call => call.name === 'uci' && ['set', 'commit'].some(word => call.args.includes(word))), false);
	assert.deepEqual(fs.readFileSync(path.join(f.trust, 'release.json')), f.receiptBefore);
	assert.deepEqual(fs.readFileSync(path.join(f.trust, 'release-signing.pem')), f.keyBefore);
	const wifi = result.calls.find(call => call.name === 'wifi-state');
	assert.deepEqual(wifi, { name: 'wifi-state', locksHeld: false });
});

test('configured or stateful installations are rejected before download and mutation', t => {
	const cases = [
		{ env: { MOCK_ENABLED: '1' } },
		{ env: { MOCK_BASE_URL: 'https://vpn.example' } },
		{ env: { MOCK_ROUTER_ID: 'router-1' } },
		{ env: { MOCK_SETUP_PREPARED: '1' } },
		{ env: { MOCK_WIFI_COMPLETE: '1' } },
		{ env: { MOCK_DIRTY_UCI: 'wireless' } },
		{ env: { MOCK_FREE: '1024' } },
		{ path: 'credentials' },
		{ path: 'state/journal.json' },
		{ path: 'networks/journal.json' },
		{ path: 'state/maintenance.lock' },
		{ path: 'state/.resume-maintenance.lock' },
		{ path: 'state/update.lock' },
	];
	for (const entry of cases) {
		const f = fixture(t);
		if (entry.path) {
			const target = path.join(f.trust, entry.path);
			fs.mkdirSync(path.dirname(target), { recursive: true });
			fs.writeFileSync(target, 'existing');
		}
		const result = f.run(entry.env);
		assert.notEqual(result.status, 0, entry.path || JSON.stringify(entry.env));
		assert.equal(result.calls.some(call => call.name === 'curl'), false);
		assert.equal(committed(result), false);
	}
});

test('trust, manifest, signature, metadata and APK plan failures never commit', t => {
	const cases = [
		[fixture(t, { receiptBase: 'https://github.com/other/AutoVPN/releases/download/old' }), {}],
		[fixture(t, { receiptKeyHash: '0'.repeat(64) }), {}],
		[fixture(t, { badManifestPin: true }), {}],
		[fixture(t, { manifestArchitecture: 'aarch64' }), {}],
		[fixture(t), { MOCK_BAD_SIGNATURE: '1' }],
		[fixture(t), { MOCK_APK_NAME: 'surprise' }],
		[fixture(t), { MOCK_UNSAFE_PLAN: '1' }],
	];
	for (const [f, env] of cases) {
		const result = f.run(env);
		assert.notEqual(result.status, 0, result.stderr);
		assert.equal(committed(result), false);
	}
});

test('safe rerun at the repaired version skips APK mutation and retries Wi-Fi with locks released', t => {
	const f = fixture(t, { receiptVersion: '0.14.0-r3' });
	const result = f.run({ MOCK_CURRENT: '0.14.1-r1' });
	assert.equal(result.status, 0, result.stderr);
	assert.equal(result.calls.some(call => call.name === 'apk' && call.args.includes('--simulate')), false);
	assert.equal(committed(result), false);
	assert.deepEqual(result.calls.find(call => call.name === 'wifi-state'), { name: 'wifi-state', locksHeld: false });
});
