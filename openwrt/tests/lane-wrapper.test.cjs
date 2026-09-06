'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const wrapperSource = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/runtime-adapter'), 'utf8');

function fixture(t, { legacy = 'owned', direct = 'missing' } = {}) {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-lane-wrapper-test-'));
	t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
	const bin = path.join(directory, 'bin');
	const tables = path.join(directory, 'tables');
	const events = path.join(directory, 'events');
	const share = path.join(directory, 'share');
	fs.mkdirSync(bin);
	fs.mkdirSync(tables);
	fs.mkdirSync(share);
	for (const name of ['guard-vpn-closed.nft', 'guard-vpn_zapret-closed.nft'])
		fs.writeFileSync(path.join(share, name), name);
	if (legacy !== 'missing')
		fs.writeFileSync(path.join(tables, 'autovpn'), legacy === 'owned' ? 'chain ownership_autovpn_v1 {}' : 'chain foreign {}');
	if (direct !== 'missing')
		fs.writeFileSync(path.join(tables, 'autovpn_direct_zapret'),
			direct === 'owned' ? 'chain ownership_autovpn_direct_zapret_v1 {}' : 'chain foreign {}');

	const tool = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const log = value => fs.appendFileSync(process.env.EVENTS, JSON.stringify(value) + '\\n');
log([name, ...args]);
if (name === 'jsonfilter') {
  try {
    let value = JSON.parse(fs.readFileSync(args[args.indexOf('-i') + 1], 'utf8'));
    for (const part of args[args.indexOf('-e') + 1].slice(1).split('.').filter(Boolean)) value = value[part];
    if (value === undefined || value === null) process.exit(1);
    process.stdout.write(typeof value === 'string' ? value : JSON.stringify(value));
  } catch (_) { process.exit(1); }
} else if (name === 'nft') {
  if (args[0] === 'list' && args[1] === 'tables') {
    for (const table of fs.readdirSync(process.env.TABLES)) process.stdout.write('table inet ' + table + '\\n');
  } else if (args[0] === 'list' && args[1] === 'table') {
    const file = path.join(process.env.TABLES, args[3]);
    if (!fs.existsSync(file)) process.exit(1);
    process.stdout.write(fs.readFileSync(file, 'utf8'));
  } else if (args[0] === '-f') {
    const table = path.basename(args[1]).includes('vpn_zapret') ? 'autovpn_vpn_zapret' : 'autovpn_vpn';
    fs.writeFileSync(path.join(process.env.TABLES, table), 'chain ownership_autovpn_v1 {}');
  } else if (args[0] === 'delete' && args[1] === 'table') {
    fs.rmSync(path.join(process.env.TABLES, args[3]), { force: true });
  } else process.exit(1);
} else process.exit(64);
`;
	for (const name of ['jsonfilter', 'nft']) fs.writeFileSync(path.join(bin, name), tool, { mode: 0o755 });
	const lane = `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.EVENTS, JSON.stringify(['runtime-lane', ...args]) + '\\n');
const action = args[0], id = args[3];
const failed = process.env.FAIL_LANE === id ||
  (process.env.FAIL_RESTORE_LANE === id && action === 'restore') ||
  (process.env.FAIL_CLOSE_LANE === id && action === 'fail-closed');
const capabilities = id === 'vpn'
  ? {vless:true,hysteria2:false,amneziawg:false,zapret:false,policy_routing:true}
  : {vless:false,hysteria2:true,amneziawg:false,zapret:true,policy_routing:true};
if (failed) {
  process.stdout.write(process.env.MALICIOUS === '1'
    ? JSON.stringify({ok:false,code:'bad\\"code',secret:'must-not-leak'})
    : JSON.stringify({ok:false,code:'runtime_not_running'}));
  process.exit(1);
}
process.stdout.write(JSON.stringify({ok:true,active_profile:id === 'vpn' ? 'vless-reality' : 'hysteria2',capabilities}));
`;
	const laneBin = path.join(bin, 'runtime-lane');
	fs.writeFileSync(laneBin, lane, { mode: 0o755 });
	const directHelper = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
fs.appendFileSync(process.env.EVENTS, JSON.stringify(['direct-zapret-helper', ...args]) + '\\n');
if (args.length !== 1) process.exit(64);
if (args[0] === 'down')
  fs.writeFileSync(path.join(process.env.TABLES, 'autovpn_direct_zapret'), 'chain ownership_autovpn_direct_zapret_v1 {}');
const failedUp = path.join(process.env.TABLES, 'direct-up-failed');
if (args[0] === 'up' && process.env.FAIL_DIRECT === 'up') fs.writeFileSync(failedUp, '1');
const failed = process.env.FAIL_DIRECT === args[0] || (args[0] === 'check' && fs.existsSync(failedUp));
if (failed) {
  const enabled = args[0] === 'check' ? {enabled:true} : {};
  process.stdout.write(process.env.MALICIOUS_DIRECT === '1'
    ? JSON.stringify({ok:false,...enabled,code:'bad\\"code',secret:'direct-secret'})
    : JSON.stringify({ok:false,...enabled,code:'direct_zapret_unavailable'}));
  process.exit(1);
}
process.stdout.write(JSON.stringify(args[0] === 'check'
  ? {ok:true,enabled:process.env.DIRECT_ENABLED !== '0',secret:'direct-secret'}
  : {ok:true,secret:'direct-secret'}));
`;
	const directBin = path.join(bin, 'direct-zapret-helper');
	fs.writeFileSync(directBin, directHelper, { mode: 0o755 });
	const wrapper = path.join(directory, 'runtime-adapter');
	fs.writeFileSync(wrapper, wrapperSource
		.replace('LANE_BIN=/usr/libexec/autovpn/runtime-lane', `LANE_BIN=${laneBin}`)
		.replace('DIRECT_BIN=/usr/libexec/autovpn/direct-zapret-helper.uc', `DIRECT_BIN=${directBin}`)
		.replace('SHARE=/usr/share/autovpn', `SHARE=${share}`), { mode: 0o755 });
	return {
		run(args, env = {}) {
			fs.writeFileSync(events, '');
			const result = spawnSync('/bin/sh', [wrapper, ...args], {
				encoding: 'utf8', timeout: 10000,
				env: { ...process.env, PATH: bin + ':' + process.env.PATH,
					EVENTS: events, TABLES: tables, ...env }
			});
			assert.equal(result.error, undefined);
			return { ...result, events: fs.readFileSync(events, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) };
		},
		tables
	};
}


test('migration retires removed lanes before activating the remaining VPN lane', t => {
	const env = fixture(t);
	const result = env.run(['prepare']);
	assert.equal(result.status, 0, result.stdout + result.stderr);
	const calls = result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]);
	assert.deepEqual(calls, [['fail-closed', 'vpn_zapret'], ['prepare', 'vpn']]);
	assert.equal(result.events.some(event => event.join(' ') === 'direct-zapret-helper down'), true);
	const events = result.events.map(event => event.join(' '));
	assert.ok(events.indexOf('nft delete table inet autovpn') > events.indexOf('direct-zapret-helper down'));
	assert.equal(fs.existsSync(path.join(env.tables, 'autovpn')), false);
});
test('migration closes removed paths even when their owned guards already exist', t => {
	const result = fixture(t, { legacy: 'missing', direct: 'owned' }).run(['activate']);
	assert.equal(result.status, 0, result.stdout + result.stderr);
	assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]),
		[['fail-closed', 'vpn_zapret'], ['activate', 'vpn']]);
	assert.equal(result.events.some(event => event.join(' ') === 'direct-zapret-helper down'), true);
});

test('foreign legacy guards refuse migration before any runtime mutation', t => {
	for (const options of [{ legacy: 'foreign' }, { legacy: 'missing', direct: 'foreign' }]) {
		const result = fixture(t, options).run(['activate']);
		assert.equal(result.status, 1);
		assert.deepEqual(JSON.parse(result.stdout), { ok: false, code: 'guard_migration_failed' });
		assert.equal(result.events.some(event => event[0] === 'runtime-lane' || event[0] === 'direct-zapret-helper' ||
			event[1] === '-f' || event[1] === 'delete'), false);
	}
});

test('failure to close a removed lane blocks activation of the remaining VPN lane', t => {
	let result = fixture(t, { legacy: 'missing' }).run(['activate'], { FAIL_CLOSE_LANE: 'vpn_zapret' });
	assert.equal(result.status, 1);
	assert.deepEqual(JSON.parse(result.stdout), { ok: false, code: 'guard_migration_failed' });
	assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]),
		[['fail-closed', 'vpn_zapret']]);

	result = fixture(t, { legacy: 'missing' }).run(['activate'], { FAIL_DIRECT: 'down' });
	assert.equal(result.status, 1);
	assert.deepEqual(JSON.parse(result.stdout), { ok: false, code: 'guard_migration_failed' });
	assert.equal(result.events.some(event => event[1] === 'activate' && event[4] === 'vpn'), false);
});

test('verify and health operate only the remaining VPN lane after retirement', t => {
	for (const action of ['verify', 'health-tick']) {
		const result = fixture(t, { legacy: 'missing', direct: 'owned' }).run([action]);
		assert.equal(result.status, 0, result.stdout + result.stderr);
		const value = JSON.parse(result.stdout);
		assert.equal(value.ok, true);
		assert.equal(value.runtime_lanes.vpn.ok, true);
		assert.equal(value.runtime_lanes.vpn_zapret.code, 'lane_removed');
		assert.equal(value.runtime_direct_zapret.code, 'lane_removed');
		assert.equal(value.capabilities.zapret, false);
		assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]),
			[['fail-closed', 'vpn_zapret'], [action, 'vpn']]);
	}
});

test('status is read-only and reports retired lanes through the compatibility shape', t => {
	const result = fixture(t, { legacy: 'owned', direct: 'owned' }).run(['status'], {
		FAIL_LANE: 'vpn_zapret', MALICIOUS: '1', FAIL_DIRECT: 'check', MALICIOUS_DIRECT: '1'
	});
	assert.equal(result.status, 0, result.stdout + result.stderr);
	const value = JSON.parse(result.stdout);
	assert.equal(value.runtime_lanes.vpn.ok, true);
	assert.equal(value.runtime_lanes.vpn_zapret.code, 'lane_removed');
	assert.deepEqual(value.runtime_direct_zapret, { ok: false, enabled: false, running: false, code: 'lane_removed' });
	assert.equal(result.stdout.includes('must-not-leak'), false);
	assert.equal(result.stdout.includes('direct-secret'), false);
	assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]),
		[['status', 'vpn']]);
	assert.equal(result.events.some(event => event[0] === 'direct-zapret-helper'), false);
});

test('restore failure closes only the active VPN lane after retired paths are closed', t => {
	const result = fixture(t, { legacy: 'missing', direct: 'owned' }).run(['restore'], { FAIL_RESTORE_LANE: 'vpn' });
	assert.equal(result.status, 0, result.stdout + result.stderr);
	const value = JSON.parse(result.stdout);
	assert.equal(value.ok, true);
	assert.equal(value.restored, false);
	assert.equal(value.runtime_lanes.vpn.code, 'runtime_not_running');
	assert.equal(value.runtime_lanes.vpn_zapret.code, 'lane_removed');
	assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => [event[1], event[4]]), [
		['fail-closed', 'vpn_zapret'], ['restore', 'vpn'], ['fail-closed', 'vpn']
	]);
});

test('ping-all and capabilities expose only the active VPN lane', t => {
	const env = fixture(t, { legacy: 'missing' });
	let result = env.run(['ping-all', '/custom/ignored.json', 'instagram', 'vpn']);
	assert.equal(result.status, 0, result.stdout + result.stderr);
	assert.deepEqual(result.events.filter(event => event[0] === 'runtime-lane').map(event => event.slice(1)),
		[['ping-all', '/custom/ignored.json', 'instagram', 'vpn']]);

	result = env.run(['ping-all', '', 'youtube', 'vpn_zapret']);
	assert.equal(result.status, 1);
	assert.equal(JSON.parse(result.stdout).code, 'lane_removed');
	result = env.run(['ping-all', '', 'youtube', '../foreign']);
	assert.equal(result.status, 1);
	assert.equal(JSON.parse(result.stdout).code, 'invalid_runtime_lane');

	result = env.run(['capabilities']);
	assert.equal(result.status, 0, result.stdout + result.stderr);
	assert.deepEqual(JSON.parse(result.stdout).capabilities,
		{ vless: true, hysteria2: false, amneziawg: false, zapret: false, policy_routing: true });
});

test('active and retired closed guards remain scoped to their original networks', () => {
	const read = name => fs.readFileSync(path.join(root, 'files/usr/share/autovpn', name), 'utf8');
	const primary = read('guard-vpn-open.nft');
	assert.match(primary, /autovpn_vpn[\s\S]*br-avpn[\s\S]*192\.168\.30\.0\/24/);
	assert.doesNotMatch(primary, /br-avpnz|192\.168\.32/);
	for (const name of ['guard-vpn-closed.nft', 'guard-vpn_zapret-closed.nft']) {
		const closed = read(name);
		assert.match(closed, /chain ownership_autovpn_v1/);
		assert.doesNotMatch(closed, /chain dns| dnat | oifname "avpn[01]" accept/);
	}
});
