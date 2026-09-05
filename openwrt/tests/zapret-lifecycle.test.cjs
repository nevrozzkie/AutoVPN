'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const moduleRoot = path.join(root, 'files/usr/share/ucode/autovpn');
const zapret = loadUcodeModule(path.join(moduleRoot, 'zapret.uc'));
const helperSource = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/zapret-helper.uc'), 'utf8')
	.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
const serviceSource = fs.readFileSync(path.join(root, 'files/etc/init.d/autovpn-zapret'), 'utf8');

const ROOT = '/var/run/autovpn-zapret';
const INPUT = '/etc/autovpn/runtime/zapret.json';
const ENGINE = '/usr/lib/autovpn-zapret';
const SERVICE = '/etc/init.d/autovpn-zapret';
const NFT = '/usr/sbin/nft';

function plan() {
	return {
		version: 1,
		wan_device: 'eth1',
		flows: [
			{ profile: 'vless-reality', ip: '203.0.113.7', port: 443, transport: 'tcp', mark: 20201, strategy: 'split' },
			{ profile: 'hysteria2', ip: '203.0.113.7', port: 8443, transport: 'udp', mark: 20202, strategy: 'fake' },
			{ profile: 'amneziawg', ip: '203.0.113.7', port: 51820, transport: 'udp', mark: 20194, strategy: 'fake' },
		],
		repeats: 2,
	};
}

function harness() {
	const files = new Map([
		[INPUT, JSON.stringify(plan())],
		['/proc/sys/net/netfilter/nf_conntrack_acct', '0\n'],
	]);
	const directories = new Set();
	const links = new Set();
	const engines = new Set([
		ENGINE + '/nfqws2', ENGINE + '/zapret-lib.lua', ENGINE + '/zapret-antidpi.lua',
	]);
	const env = {
		files, directories, links, engines, calls: [], writes: [], modes: [], output: null, exit: null,
		tableRaw: null, serviceRunning: false, externalQueue: false, validationFails: false,
		listenerAppears: true, nftCheckFails: false, nftApplyFails: false, tableDuringStart: false,
	};

	function queueText() {
		return env.externalQueue || (env.serviceRunning && env.listenerAppears) ? ' 20195 123 0 2 65535 0 0 0 1\n' : '';
	}
	function readfile(name, limit) {
		if (name === '/proc/net/netfilter/nfnetlink_queue') return queueText().slice(0, limit);
		const value = files.get(name);
		return value == null ? null : value.slice(0, limit);
	}
	function writefile(name, value) {
		if (!directories.has(ROOT) || links.has(name)) return null;
		files.set(name, value);
		env.writes.push([name, value]);
		return Buffer.byteLength(value);
	}
	function chmod(name, mode) {
		if (!directories.has(name) && !files.has(name)) return null;
		env.modes.push([name, mode]);
		return true;
	}
	function rename(from, to) {
		if (!files.has(from) || links.has(to)) return null;
		files.set(to, files.get(from));
		files.delete(from);
		return true;
	}
	function unlink(name) {
		files.delete(name);
		return true;
	}
	function access(name, mode) {
		return engines.has(name) && (mode === 'x' || mode === 'r');
	}
	function mkdir(name) {
		if (directories.has(name) || links.has(name)) return null;
		directories.add(name);
		return true;
	}
	function lstat(name) {
		if (links.has(name)) return { type: 'link', uid: 0 };
		if (directories.has(name)) return { type: 'directory', uid: 0 };
		if (files.has(name)) return { type: 'file', uid: 0 };
		return null;
	}
	function result(output = '', status = 0) {
		return { read: limit => output.slice(0, limit), close: () => status };
	}
	function popen(argv) {
		env.calls.push([...argv]);
		if (argv[0] === ENGINE + '/nfqws2') return result('', env.validationFails ? 1 : 0);
		if (argv[0] === NFT && argv[1] === 'list' && argv[2] === 'tables')
			return result(env.tableRaw == null ? 'table inet fw4\n' : 'table inet fw4\ntable inet autovpn_zapret\n');
		if (argv[0] === NFT && argv[1] === '-s') return env.tableRaw == null ? result('', 1) : result(env.tableRaw);
		if (argv[0] === NFT && argv[1] === 'delete') {
			env.tableRaw = null;
			return result();
		}
		if (argv[0] === NFT && argv[1] === '-c') return result('', env.nftCheckFails ? 1 : 0);
		if (argv[0] === NFT && argv[1] === '-f') {
			if (env.nftApplyFails) return result('', 1);
			env.tableRaw = 'table inet autovpn_zapret {\n\tchain ownership_autovpn_zapret_v1 {\n\t}\n}\n';
			return result();
		}
		if (argv[0] === SERVICE && argv[1] === 'stop') {
			env.serviceRunning = false;
			return result();
		}
		if (argv[0] === SERVICE && argv[1] === 'start') {
			env.tableDuringStart = env.tableRaw != null;
			env.serviceRunning = true;
			return result();
		}
		if (argv[0] === SERVICE && argv[1] === 'running') return result('', env.serviceRunning ? 0 : 1);
		if (argv[0] === '/sbin/sysctl') {
			files.set('/proc/sys/net/netfilter/nf_conntrack_acct', '1\n');
			return result('net.netfilter.nf_conntrack_acct = 1\n');
		}
		if (argv[0] === '/sbin/modprobe' || argv[0] === '/bin/sleep') return result();
		throw new Error('Unexpected command: ' + JSON.stringify(argv));
	}

	function run(action, input = INPUT) {
		env.output = null;
		env.exit = null;
		const invoke = new Function(
			'readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'mkdir', 'lstat',
			'require', 'length', 'match', 'sprintf', 'json', 'index', 'ARGV', 'printf', 'exit',
			helperSource,
		);
		invoke(
			readfile, writefile, chmod, rename, unlink, access, mkdir, lstat,
			name => {
				if (name === 'autovpn.zapret') return zapret;
				if (name === 'autovpn.process') return { popen };
				throw new Error('Unexpected module: ' + name);
			},
			value => typeof value === 'string' ? Buffer.byteLength(value) : value.length,
			(value, expression) => value.match(expression),
			(format, value) => JSON.stringify(value) + (format.endsWith('\n') ? '\n' : ''),
			JSON.parse, (values, value) => values.indexOf(value), [action, input],
			(_format, value) => { env.output = structuredClone(value); }, value => { env.exit = value; },
		);
		return env.output;
	}

	return { env, run };
}

function called(env, ...wanted) {
	return env.calls.some(argv => wanted.every((value, index) => argv[index] === value));
}

test('real helper validates, starts, checks and removes one owned zapret lifecycle', () => {
	const { env, run } = harness();
	assert.deepEqual(run('up'), { ok: true });
	assert.equal(env.exit, 0);
	assert.equal(env.serviceRunning, true);
	assert.match(env.tableRaw, /ownership_autovpn_zapret_v1/);
	assert.equal(env.files.get(ROOT + '/nfqws.conf'), zapret.config(plan()));
	assert.equal(env.files.get(ROOT + '/plan.json'), JSON.stringify(plan()) + '\n');
	assert.equal(env.files.has(ROOT + '/check.conf'), false);
	const validation = env.writes.find(([name]) => name === ROOT + '/check.conf.new')[1];
	assert.match(validation, /--intercept=0\n$/);
	assert.ok(env.calls.some(argv => JSON.stringify(argv) === JSON.stringify([ENGINE + '/nfqws2', '@' + ROOT + '/check.conf'])));
	assert.equal(called(env, '/sbin/modprobe', 'nft_queue'), true);
	assert.equal(called(env, '/sbin/sysctl', '-w', 'net.netfilter.nf_conntrack_acct=1'), true);
	assert.ok(env.modes.some(([name, mode]) => name === ROOT && mode === 0o700));
	assert.ok(env.modes.some(([name, mode]) => name === ROOT + '/nfqws.conf.new' && mode === 0o600));
	assert.deepEqual(run('check'), { ok: true });
	assert.deepEqual(run('down'), { ok: true });
	assert.equal(env.serviceRunning, false);
	assert.equal(env.tableRaw, null);
	for (const name of ['plan.json', 'rules.nft', 'nfqws.conf', 'canonical.nft'])
		assert.equal(env.files.has(ROOT + '/' + name), false);
});

test('null plan validates and keeps zapret stopped for the default disabled policy', () => {
	const { env, run } = harness();
	env.files.set(INPUT, 'null\n');
	assert.deepEqual(run('validate'), { ok: true });
	assert.deepEqual(run('up'), { ok: true });
	assert.deepEqual(run('check'), { ok: true });
	assert.deepEqual(run('down'), { ok: true });
	assert.equal(env.tableRaw, null);
	assert.equal(env.serviceRunning, false);
	assert.equal(env.calls.some(argv => argv[0] === ENGINE + '/nfqws2'), false);
	assert.equal(called(env, NFT, '-f'), false);
});

test('foreign nft state and symlinked runtime paths are never adopted or overwritten', () => {
	{
		const { env, run } = harness();
		env.tableRaw = 'table inet autovpn_zapret { chain someone_else {} }\n';
		assert.equal(run('up').code, 'zapret_unavailable');
		assert.match(env.tableRaw, /someone_else/);
		assert.equal(called(env, NFT, 'delete', 'table', 'inet', 'autovpn_zapret'), false);
		assert.equal(called(env, SERVICE, 'stop'), false);
	}
	{
		const { env, run } = harness();
		env.links.add(ROOT);
		env.files.set(ROOT + '/sentinel', 'private');
		assert.equal(run('up').code, 'zapret_unavailable');
		assert.equal(env.files.get(ROOT + '/sentinel'), 'private');
		assert.equal(env.writes.length, 0);
	}
	{
		const { env, run } = harness();
		env.directories.add(ROOT);
		env.links.add(ROOT + '/check.conf.new');
		assert.equal(run('up').code, 'zapret_unavailable');
		assert.equal(env.writes.length, 0);
	}
});

test('missing engine and absent, malformed or invalid input fail before mutation', () => {
	for (const prepare of [
		env => env.engines.delete(ENGINE + '/nfqws2'),
		env => { env.validationFails = true; },
		env => env.files.delete(INPUT),
		env => env.files.set(INPUT, '{bad-json'),
		env => env.files.set(INPUT, JSON.stringify({ ...plan(), extra: true })),
	]) {
		const { env, run } = harness();
		prepare(env);
		assert.equal(run('up').code, 'zapret_unavailable');
		assert.equal(env.tableRaw, null);
		assert.equal(env.serviceRunning, false);
		assert.equal(called(env, NFT, '-f'), false);
		assert.equal(called(env, SERVICE, 'start'), false);
	}
	const { run } = harness();
	assert.equal(run('check', '/tmp/foreign.json').code, 'zapret_unavailable');
});

test('an occupied NFQUEUE is preserved and blocks activation before nft install', () => {
	const { env, run } = harness();
	// Simulates another listener already owning the globally fixed queue number.
	env.externalQueue = true;
	assert.equal(run('up').code, 'zapret_unavailable');
	assert.equal(env.externalQueue, true);
	assert.equal(env.tableRaw, null);
	assert.equal(called(env, '/sbin/modprobe', 'nft_queue'), false);
	assert.equal(called(env, NFT, '-f'), false);
	assert.equal(called(env, SERVICE, 'start'), false);
});

test('canonical nft tampering fails check and a new up replaces only the owned table', () => {
	const { env, run } = harness();
	assert.deepEqual(run('up'), { ok: true });
	env.tableRaw = env.tableRaw.replace('\t}\n}\n', '\t}\n\tchain injected {}\n}\n');
	assert.equal(run('check').code, 'zapret_unavailable');
	assert.deepEqual(run('up'), { ok: true });
	assert.doesNotMatch(env.tableRaw, /injected/);
	assert.deepEqual(run('check'), { ok: true });
});

test('listener failure keeps scoped rules fail-closed during start and removes them on rollback', () => {
	const { env, run } = harness();
	env.listenerAppears = false;
	assert.equal(run('up').code, 'zapret_unavailable');
	assert.equal(env.tableDuringStart, true);
	const apply = env.calls.findIndex(argv => argv[0] === NFT && argv[1] === '-f');
	const start = env.calls.findIndex(argv => argv[0] === SERVICE && argv[1] === 'start');
	assert.ok(apply >= 0 && apply < start);
	assert.doesNotMatch(zapret.nft(plan()), /\bbypass\b/);
	assert.equal(env.calls.filter(argv => argv[0] === '/bin/sleep').length, 5);
	assert.equal(env.serviceRunning, false);
	assert.equal(env.tableRaw, null);
	assert.equal(env.files.has(ROOT + '/plan.json'), false);
	assert.equal(env.files.has(ROOT + '/canonical.nft'), false);
});

test('service remains a foreground, controller-only procd process', () => {
	assert.match(serviceSource, /procd_set_param command .*nfqws2 @\/var\/run\/autovpn-zapret\/nfqws\.conf/);
	assert.doesNotMatch(serviceSource, /--daemon|enable\)/);
	assert.match(serviceSource, /boot\(\) \{ return 0; \}/);
});
