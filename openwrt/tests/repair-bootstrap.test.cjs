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
	const controllerName = 'autovpn-controller-0.14.5-r1.apk';
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
			{ name: 'autovpn-controller', filename: controllerName, sha256: digest(controller), version: '0.14.5-r1' },
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
	const networkHelper = path.join(bin, 'network-helper.uc');
	const timeout = path.join(bin, 'timeout');
	let source = template
		.replace('@AUTOVPN_RELEASE_BASE@', 'https://github.com/example/AutoVPN/releases/download/router-v0.14.5-openwrt-25.12.5-r1')
		.replace('@AUTOVPN_SIGNING_KEY_SHA256@', keyHash)
		.replace('@AUTOVPN_MANIFEST_SHA256@', options.badManifestPin ? '0'.repeat(64) : digest(manifestBytes))
		.replaceAll('/etc/autovpn', trust)
		.replaceAll('/etc/apk/', apkRoot + '/')
		.replaceAll('/lib/apk/', libApk + '/')
		.replace('UPDATE_LOCK=/var/lock/autovpn-update.lock', `UPDATE_LOCK=${updateLock}`)
		.replace('CONTROLLER_LOCK=/var/lock/autovpn-controller.lock', `CONTROLLER_LOCK=${controllerLock}`)
		.replace('WIFI_HELPER=/usr/libexec/autovpn/install-wifi', `WIFI_HELPER=${helper}`)
		.replace('NETWORK_HELPER=/usr/libexec/autovpn/network-helper.uc', `NETWORK_HELPER=${networkHelper}`)
		.replace('TIMEOUT=/usr/bin/timeout', `TIMEOUT=${timeout}`);
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
else if (name === 'timeout') {
  if (args[0] !== '20' || path.basename(args[1]) !== 'network-helper.uc' || args[2] !== 'network-gate') process.exit(2);
  if (env.MOCK_NETWORK_GATE_FAIL === '1') process.exit(1);
}
else if (name === 'network-helper.uc') process.exit(99);
else if (name === 'flock') {
  if (args[0] !== '-n' || !/^\\d+$/.test(args[1])) process.exit(2);
  if ((env.MOCK_BUSY_LOCK === 'autovpn-update.lock' && args[1] === '8') ||
      (env.MOCK_BUSY_LOCK === 'autovpn-controller.lock' && args[1] === '9')) process.exit(1);
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
      'autovpn.wifi.base_ssid': env.MOCK_WIFI_SSID ?? '',
      'autovpn.wifi.password': env.MOCK_WIFI_PASSWORD ?? '',
      'autovpn.wifi.primary_lan': env.MOCK_WIFI_PRIMARY_LAN ?? '',
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
		output({info:{name:env.MOCK_APK_NAME || 'autovpn-controller',version:env.MOCK_APK_VERSION || '0.14.5-r1',arch:env.MOCK_APK_ARCH || 'noarch'}});
  } else if (args.includes('--simulate')) {
    const current = fs.existsSync(env.MOCK_INSTALLED_FILE) ? fs.readFileSync(env.MOCK_INSTALLED_FILE, 'utf8') : (env.MOCK_CURRENT || '0.14.0-r3');
		if (env.MOCK_UNSAFE_PLAN === '1') output('(1/2) Upgrading autovpn-controller (' + current + ' -> 0.14.5-r1)\\n(2/2) Installing surprise (1-r1)');
		else output('(1/1) Upgrading autovpn-controller (' + current + ' -> 0.14.5-r1)');
  } else if (args.includes('add')) {
    if (env.MOCK_COMMIT_FAIL === '1') process.exit(1);
		fs.writeFileSync(env.MOCK_INSTALLED_FILE, '0.14.5-r1');
  } else process.exit(91);
}
else if (name === 'install-wifi') {
  log({name:'wifi-state'});
  if (env.MOCK_WIFI_FAIL === '1') process.exit(1);
}
else throw new Error('unexpected mock tool ' + name);
`;
	for (const name of ['id', 'uname', 'df', 'timeout', 'network-helper.uc', 'flock', 'uci', 'ubus', 'jsonfilter', 'curl', 'apk', 'install-wifi']) {
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	}
	const logFile = path.join(root, 'calls.jsonl');
	const installedFile = path.join(root, 'installed-version');
	return {
		root, trust, script, receiptBefore, keyBefore, updateLock, controllerLock,
		networkJournal: path.join(trust, 'networks/journal.json'),
		maintenanceGate: path.join(trust, 'state/maintenance.lock'),
		controllerJournal: path.join(trust, 'state/journal.json'),
		credential: path.join(trust, 'credentials'),
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
		'autovpn-controller-0.14.5-r1.apk',
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
	assert.deepEqual(wifi, { name: 'wifi-state' });
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

test('0.14.0 receipt with installed 0.14.1 upgrades to the pinned repair without rewriting trust', t => {
	const f = fixture(t, { receiptVersion: '0.14.0-r1' });
	const result = f.run({
		MOCK_CURRENT: '0.14.1-r1',
		MOCK_WIFI_SSID: 'wifi',
		MOCK_WIFI_PASSWORD: 'failed-attempt-password',
		MOCK_WIFI_PRIMARY_LAN: '1',
	});
	assert.equal(result.status, 0, result.stderr);
	assert.equal(result.calls.some(call => call.name === 'apk' && call.args.includes('--simulate')), true);
	assert.equal(committed(result), true);
	assert.deepEqual(fs.readFileSync(path.join(f.trust, 'release.json')), f.receiptBefore);
	assert.deepEqual(fs.readFileSync(path.join(f.trust, 'release-signing.pem')), f.keyBefore);
	assert.deepEqual(result.calls.find(call => call.name === 'wifi-state'), { name: 'wifi-state' });
});

test('safe rerun at the repaired version skips APK mutation and retries Wi-Fi with locks released', t => {
	const f = fixture(t, { receiptVersion: '0.14.0-r1' });
	const result = f.run({ MOCK_CURRENT: '0.14.5-r1' });
	assert.equal(result.status, 0, result.stderr);
	assert.equal(result.calls.some(call => call.name === 'apk' && call.args.includes('--simulate')), false);
	assert.equal(committed(result), false);
	assert.deepEqual(result.calls.find(call => call.name === 'wifi-state'), { name: 'wifi-state' });
});

test('confirmed primary Wi-Fi upgrades 0.14.2 unchanged, passes the gate before mutation and continues in LuCI', t => {
	const f = fixture(t, { receiptVersion: '0.14.2-r1' });
	fs.mkdirSync(path.dirname(f.networkJournal), { recursive: true });
	const journal = JSON.stringify({ phase: 'confirmed', retained: 'fixture' }) + '\n';
	fs.writeFileSync(f.networkJournal, journal, { mode: 0o600 });
	const result = f.run({
		MOCK_CURRENT: '0.14.2-r1',
		MOCK_WIFI_COMPLETE: '1',
		MOCK_WIFI_PRIMARY_LAN: '1',
	});
	assert.equal(result.status, 0, result.stderr);
	assert.equal(committed(result), true);
	assert.equal(fs.readFileSync(f.networkJournal, 'utf8'), journal);
	assert.equal(result.calls.some(call => call.name === 'install-wifi'), false);
	assert.match(result.stdout, /continue setup in LuCI/);
	const gateIndex = result.calls.findIndex(call => call.name === 'timeout');
	const commitIndex = result.calls.findIndex(call => call.name === 'apk' && call.args.includes('add') && !call.args.includes('--simulate'));
	assert.ok(gateIndex >= 0 && gateIndex < commitIndex);
	assert.deepEqual(result.calls[gateIndex].args, ['20', f.script.replace('repair-bootstrap.sh', 'bin/network-helper.uc'), 'network-gate']);
});

test('confirmed bootstrap recovery rejects unsafe journal states, mismatched flags and a failed network gate before mutation', t => {
	const cases = [
		{ journal: JSON.stringify({ phase: 'pending' }), env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '1' } },
		{ journal: JSON.stringify({ phase: 'rolled_back' }), env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '1' } },
		{ journal: '{tampered', env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '1' } },
		{ journal: JSON.stringify({ phase: 'confirmed' }), symlink: true, env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '1' } },
		{ journal: JSON.stringify({ phase: 'confirmed' }), env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '0' } },
		{ journal: JSON.stringify({ phase: 'confirmed' }), env: { MOCK_WIFI_COMPLETE: '0', MOCK_WIFI_PRIMARY_LAN: '1' } },
		{ journal: JSON.stringify({ phase: 'confirmed' }), env: { MOCK_WIFI_COMPLETE: '1', MOCK_WIFI_PRIMARY_LAN: '1', MOCK_NETWORK_GATE_FAIL: '1' } },
	];
	for (const entry of cases) {
		const f = fixture(t, { receiptVersion: '0.14.2-r1' });
		fs.mkdirSync(path.dirname(f.networkJournal), { recursive: true });
		if (entry.symlink) {
			const target = f.networkJournal + '.target';
			fs.writeFileSync(target, entry.journal);
			fs.symlinkSync(target, f.networkJournal);
		} else {
			fs.writeFileSync(f.networkJournal, entry.journal);
		}
		const result = f.run({ MOCK_CURRENT: '0.14.2-r1', ...entry.env });
		assert.notEqual(result.status, 0, JSON.stringify(entry));
		assert.equal(result.calls.some(call => call.name === 'curl'), false);
		assert.equal(committed(result), false);
		assert.equal(result.calls.some(call => call.name === 'install-wifi'), false);
	}
});

test('stranded running Rebind upgrades 0.14.3 while preserving identity, credential, confirmed Wi-Fi and its retry gate', t => {
	const f = fixture(t, { receiptVersion: '0.14.3-r1' });
	fs.mkdirSync(path.dirname(f.networkJournal), { recursive: true });
	fs.mkdirSync(path.dirname(f.maintenanceGate), { recursive: true });
	const journal = JSON.stringify({ phase: 'confirmed', retained: 'network fixture' }) + '\n';
	const gate = JSON.stringify({ schema_version: 1, action: 'rebind', phase: 'running' }) + '\n';
	const credential = 'avrt_oldrouter.' + 'o'.repeat(43) + '\n';
	fs.writeFileSync(f.networkJournal, journal, { mode: 0o600 });
	fs.writeFileSync(f.maintenanceGate, gate, { mode: 0o600 });
	fs.writeFileSync(f.credential, credential, { mode: 0o600 });
	const result = f.run({
		MOCK_CURRENT: '0.14.3-r1',
		MOCK_SETUP_PREPARED: '1',
		MOCK_BASE_URL: 'https://vpn.example',
		MOCK_ROUTER_ID: 'old_router',
		MOCK_WIFI_COMPLETE: '1',
		MOCK_WIFI_PRIMARY_LAN: '1',
	});
	assert.equal(result.status, 0, result.stderr);
	assert.equal(committed(result), true);
	assert.equal(fs.readFileSync(f.networkJournal, 'utf8'), journal);
	assert.equal(fs.readFileSync(f.maintenanceGate, 'utf8'), gate);
	assert.equal(fs.readFileSync(f.credential, 'utf8'), credential);
	assert.equal(result.calls.some(call => call.name === 'install-wifi'), false);
	assert.equal(result.calls.some(call => call.name === 'uci' && ['set', 'commit'].some(word => call.args.includes(word))), false);
	assert.match(result.stdout, /Retry Rebind explicitly in LuCI/);
});

test('stranded Rebind recovery rejects every other maintenance or unsafe runtime state before mutation', t => {
	const validGate = { schema_version: 1, action: 'rebind', phase: 'running' };
	const cases = [
		{ gate: { schema_version: 1, action: 'reset', phase: 'running' } },
		{ gate: { schema_version: 1, action: 'rebind', phase: 'ready' } },
		{ gate: { schema_version: 1, action: 'rebind', phase: 'failed' } },
		{ gate: '{invalid' },
		{ gate: validGate, gateSymlink: true },
		{ gate: validGate, controllerJournal: true },
		{ gate: validGate, networkPhase: 'pending' },
		{ gate: validGate, env: { MOCK_ENABLED: '1' } },
		{ gate: validGate, env: { MOCK_DIRTY_UCI: 'autovpn' } },
		{ gate: validGate, receiptVersion: '0.14.2-r1' },
		{ gate: validGate, env: { MOCK_CURRENT: '0.14.2-r1' } },
	];
	for (const entry of cases) {
		const f = fixture(t, { receiptVersion: entry.receiptVersion || '0.14.3-r1' });
		fs.mkdirSync(path.dirname(f.networkJournal), { recursive: true });
		fs.mkdirSync(path.dirname(f.maintenanceGate), { recursive: true });
		fs.writeFileSync(f.networkJournal, JSON.stringify({ phase: entry.networkPhase || 'confirmed' }));
		const gateBytes = typeof entry.gate === 'string' ? entry.gate : JSON.stringify(entry.gate);
		if (entry.gateSymlink) {
			const target = f.maintenanceGate + '.target';
			fs.writeFileSync(target, gateBytes);
			fs.symlinkSync(target, f.maintenanceGate);
		} else {
			fs.writeFileSync(f.maintenanceGate, gateBytes);
		}
		if (entry.controllerJournal) fs.writeFileSync(f.controllerJournal, '{}');
		const result = f.run({
			MOCK_CURRENT: '0.14.3-r1',
			MOCK_SETUP_PREPARED: '1',
			MOCK_BASE_URL: 'https://vpn.example',
			MOCK_ROUTER_ID: 'old_router',
			MOCK_WIFI_COMPLETE: '1',
			MOCK_WIFI_PRIMARY_LAN: '1',
			...(entry.env || {}),
		});
		assert.notEqual(result.status, 0, JSON.stringify(entry));
		assert.equal(result.calls.some(call => call.name === 'curl'), false);
		assert.equal(committed(result), false);
		assert.equal(result.calls.some(call => call.name === 'install-wifi'), false);
	}
});

test('descriptor locks keep their inodes, replace legacy PID contents and are never explicitly unlocked', t => {
	const f = fixture(t);
	fs.writeFileSync(f.updateLock, '1234\n');
	fs.writeFileSync(f.controllerLock, '5678\n');
	const updateInode = fs.statSync(f.updateLock).ino;
	const controllerInode = fs.statSync(f.controllerLock).ino;
	const result = f.run();
	assert.equal(result.status, 0, result.stderr);
	assert.equal(fs.statSync(f.updateLock).ino, updateInode);
	assert.equal(fs.statSync(f.controllerLock).ino, controllerInode);
	assert.equal(fs.readFileSync(f.updateLock, 'utf8'), '0\n');
	assert.equal(fs.readFileSync(f.controllerLock, 'utf8'), '0\n');
	assert.equal(result.calls.some(call => call.name === 'flock' && call.args.includes('-u')), false);
});

test('busy descriptor locks fail closed without clearing legacy PID contents', t => {
	for (const lockName of ['autovpn-update.lock', 'autovpn-controller.lock']) {
		const f = fixture(t);
		const lockPath = lockName === 'autovpn-update.lock' ? f.updateLock : f.controllerLock;
		fs.writeFileSync(lockPath, '1234\n');
		const result = f.run({MOCK_BUSY_LOCK: lockName});
		assert.notEqual(result.status, 0);
		assert.equal(fs.readFileSync(lockPath, 'utf8'), '1234\n');
		assert.equal(committed(result), false);
	}
});
