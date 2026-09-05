'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const direct = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/direct_zapret.uc'));
const helperSource = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/direct-zapret-helper.uc'), 'utf8')
	.replace(/^#![^\n]*\n/, '').replace(/^import\s+.*?;\s*$/gm, '');
const serviceSource = fs.readFileSync(path.join(root, 'files/etc/init.d/autovpn-direct-zapret'), 'utf8');

const ROOT = '/var/run/autovpn-direct-zapret';
const ENGINE = '/usr/lib/autovpn-zapret';
const SERVICE = '/etc/init.d/autovpn-direct-zapret';
const NFT = '/usr/sbin/nft';

function harness() {
	const files = new Map([['/proc/sys/net/netfilter/nf_conntrack_acct', '0\n']]);
	const directories = new Set();
	const links = new Set();
	const engines = new Set([
		ENGINE + '/nfqws2', ENGINE + '/zapret-lib.lua', ENGINE + '/zapret-antidpi.lua',
	]);
	const settings = new Map([
		['runtime.wan_device', 'pppoe-wan'],
	]);
	const env = {
		files, directories, links, engines, settings, calls: [], writes: [], applied: [], output: null, exit: null,
		tableRaw: null, serviceRunning: false, externalQueue: false, listenerAppears: true,
		validationFails: false, nftCheckFails: false, openApplyFails: false,
		networkGateFails: false, detectedWan: 'pppoe-wan',
	};

	function queueText() {
		return env.externalQueue || (env.serviceRunning && env.listenerAppears) ?
			' 20196 123 0 2 65535 0 0 0 1\n' : '';
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
	function chmod(name) { return directories.has(name) || files.has(name) ? true : null; }
	function rename(from, to) {
		if (!files.has(from) || links.has(to)) return null;
		files.set(to, files.get(from)); files.delete(from); return true;
	}
	function unlink(name) { files.delete(name); return true; }
	function access(name) { return engines.has(name); }
	function mkdir(name) {
		if (directories.has(name) || links.has(name)) return null;
		directories.add(name); return true;
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
		if (argv[0] === '/bin/ubus') return result(JSON.stringify({ l3_device: env.detectedWan }));
		if (argv[0] === '/usr/libexec/autovpn/network-helper.uc') return result('', env.networkGateFails ? 1 : 0);
		if (argv[0] === NFT && argv[1] === 'list' && argv[2] === 'tables')
			return result(env.tableRaw == null ? 'table inet fw4\n' : 'table inet fw4\ntable inet autovpn_direct_zapret\n');
		if (argv[0] === NFT && argv[1] === '-s') return env.tableRaw == null ? result('', 1) : result(env.tableRaw);
		if (argv[0] === NFT && argv[1] === '-c') return result('', env.nftCheckFails ? 1 : 0);
		if (argv[0] === NFT && argv[1] === '-f') {
			const raw = files.get(argv[2]);
			if (raw == null || (env.openApplyFails && raw.includes('queue num 20196'))) return result('', 1);
			env.tableRaw = raw;
			env.applied.push(raw);
			return result();
		}
		if (argv[0] === SERVICE && argv[1] === 'stop') { env.serviceRunning = false; return result(); }
		if (argv[0] === SERVICE && argv[1] === 'start') { env.serviceRunning = true; return result(); }
		if (argv[0] === SERVICE && argv[1] === 'running') return result('', env.serviceRunning ? 0 : 1);
		if (argv[0] === '/sbin/sysctl') {
			files.set('/proc/sys/net/netfilter/nf_conntrack_acct', '1\n'); return result();
		}
		if (argv[0] === '/sbin/modprobe' || argv[0] === '/bin/sleep') return result();
		throw new Error('Unexpected command: ' + JSON.stringify(argv));
	}
	function cursor() {
		return {
			load() {},
			get(_config, section, option) { return settings.get(section + '.' + option); },
		};
	}
	function run(action, extra) {
		env.output = null; env.exit = null;
		const invoke = new Function(
			'readfile', 'writefile', 'chmod', 'rename', 'unlink', 'access', 'mkdir', 'lstat', 'fsError', 'cursor',
			'require', 'type', 'length', 'match', 'sprintf', 'json', 'index', 'int', 'ARGV', 'printf', 'exit', helperSource,
		);
		invoke(readfile, writefile, chmod, rename, unlink, access, mkdir, lstat, () => 'No such file or directory', cursor,
			name => {
				if (name === 'autovpn.direct_zapret') return direct;
				if (name === 'autovpn.process') return { popen };
				throw new Error('Unexpected module: ' + name);
			},
			value => value == null ? null : Array.isArray(value) ? 'array' :
				typeof value === 'number' ? (Number.isInteger(value) ? 'int' : 'double') :
					typeof value === 'boolean' ? 'bool' : typeof value,
			value => typeof value === 'string' ? Buffer.byteLength(value) : value.length,
			(value, expression) => value.match(expression),
			(format, value) => JSON.stringify(value) + (format.endsWith('\n') ? '\n' : ''),
			JSON.parse, (values, value) => values.indexOf(value), value => Number.parseInt(value, 10),
			extra === undefined ? [action] : [action, extra],
			(_format, value) => { env.output = structuredClone(value); }, value => { env.exit = value; });
		return env.output;
	}
	return { env, run };
}

function called(env, ...wanted) {
	return env.calls.some(argv => wanted.every((value, index) => argv[index] === value));
}

function mediaPlan(overrides = {}) {
	return direct.plan('pppoe-wan', true, {
		discord_media: false,
		stun: false,
		media_strategy: 'fake',
		media_repeats: 2,
		...overrides,
	});
}

test('up closes first, validates, starts listener and only then opens direct forwarding', () => {
	const { env, run } = harness();
	assert.deepEqual(run('up'), { ok: true });
	assert.equal(env.exit, 0);
	assert.equal(env.applied.length, 2);
	assert.equal(env.applied[0], direct.closedNft());
	assert.equal(env.applied[1], direct.nft(direct.plan('pppoe-wan', true)));
	const start = env.calls.findIndex(argv => argv[0] === SERVICE && argv[1] === 'start');
	const applies = env.calls.map((argv, index) => ({ argv, index }))
		.filter(({ argv }) => argv[0] === NFT && argv[1] === '-f');
	const open = applies[1].index;
	assert.ok(start >= 0 && open > start);
	assert.equal(env.files.get(ROOT + '/nfqws.conf'), direct.config(direct.plan('pppoe-wan', true)));
	assert.equal(env.files.get(ROOT + '/plan.json'), JSON.stringify(direct.plan('pppoe-wan', true)) + '\n');
	const validation = env.writes.find(([name]) => name === ROOT + '/check.conf.new')[1];
	assert.match(validation, /--intercept=0\n$/);
	assert.equal(called(env, '/sbin/modprobe', 'nft_queue'), true);
	assert.deepEqual(run('check'), { ok: true, enabled: true });
});

test('explicit media settings enable a version 2 plan with typed defaults', () => {
	const { env, run } = harness();
	env.settings.set('direct.discord_media', '1');
	assert.deepEqual(run('up'), { ok: true });
	const expected = mediaPlan({ discord_media: true });
	assert.equal(expected.version, 2);
	assert.equal(env.tableRaw, direct.nft(expected));
	assert.equal(env.files.get(ROOT + '/nfqws.conf'), direct.config(expected));
	assert.equal(env.files.get(ROOT + '/plan.json'), JSON.stringify(expected) + '\n');
	assert.match(env.tableRaw, /udp dport \{ 50000-50099,19294-19344 \}/);
	assert.match(env.files.get(ROOT + '/nfqws.conf'), /--filter-l7=discord/);
	assert.doesNotMatch(env.files.get(ROOT + '/nfqws.conf'), /--filter-l7=stun/);
	assert.deepEqual(run('check'), { ok: true, enabled: true });
});

test('an unchanged version 2 plan is idempotent', () => {
	const { env, run } = harness();
	env.settings.set('direct.discord_media', '1');
	env.settings.set('direct.stun', '1');
	env.settings.set('direct.media_strategy', 'fake_badsum');
	env.settings.set('direct.media_repeats', '4');
	assert.deepEqual(run('up'), { ok: true });
	const applied = env.applied.length;
	const writes = env.writes.length;
	const starts = env.calls.filter(argv => argv[0] === SERVICE && argv[1] === 'start').length;
	assert.deepEqual(run('up'), { ok: true });
	assert.equal(env.applied.length, applied);
	assert.equal(env.writes.length, writes);
	assert.equal(env.calls.filter(argv => argv[0] === SERVICE && argv[1] === 'start').length, starts);
});

test('media toggle and strategy changes reconfigure closed-first and removals return to legacy', () => {
	const { env, run } = harness();
	env.settings.set('direct.discord_media', '1');
	env.settings.set('direct.stun', '0');
	env.settings.set('direct.media_strategy', 'fake');
	env.settings.set('direct.media_repeats', '2');
	assert.deepEqual(run('up'), { ok: true });

	env.settings.set('direct.discord_media', '0');
	env.settings.set('direct.stun', '1');
	env.settings.set('direct.media_strategy', 'fake_badsum');
	env.settings.set('direct.media_repeats', '6');
	const before = env.applied.length;
	assert.deepEqual(run('up'), { ok: true });
	const expected = mediaPlan({ stun: true, media_strategy: 'fake_badsum', media_repeats: 6 });
	assert.deepEqual(env.applied.slice(before), [direct.closedNft(), direct.nft(expected)]);
	assert.equal(env.files.get(ROOT + '/nfqws.conf'), direct.config(expected));
	assert.match(env.files.get(ROOT + '/nfqws.conf'), /--filter-l7=stun/);
	assert.match(env.files.get(ROOT + '/nfqws.conf'), /:badsum:repeats=6/);
	assert.doesNotMatch(env.files.get(ROOT + '/nfqws.conf'), /--filter-l7=discord/);

	for (const option of ['discord_media', 'stun', 'media_strategy', 'media_repeats'])
		env.settings.delete('direct.' + option);
	const beforeLegacy = env.applied.length;
	assert.deepEqual(run('up'), { ok: true });
	const legacy = direct.plan('pppoe-wan', true);
	assert.equal(legacy.version, 1);
	assert.deepEqual(env.applied.slice(beforeLegacy), [direct.closedNft(), direct.nft(legacy)]);
	assert.equal(env.files.get(ROOT + '/plan.json'), JSON.stringify(legacy) + '\n');
	assert.equal(env.files.get(ROOT + '/nfqws.conf'), direct.config(legacy));
});

test('malformed media settings close an already open lane before failing', () => {
	for (const [option, value] of [
		['discord_media', 'yes'],
		['stun', '2'],
		['media_strategy', 'ttl'],
		['media_repeats', '0'],
		['media_repeats', '02'],
		['media_repeats', '7'],
	]) {
		const { env, run } = harness();
		env.settings.set('direct.discord_media', '1');
		assert.deepEqual(run('up'), { ok: true });
		env.settings.set('direct.' + option, value);
		const before = env.applied.length;
		assert.equal(run('up').code, 'direct_zapret_unavailable', `${option}=${value}`);
		assert.deepEqual(env.applied.slice(before), [direct.closedNft()]);
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.serviceRunning, false);
		assert.equal(env.files.get(ROOT + '/plan.json'), 'null\n');
		assert.equal(env.files.has(ROOT + '/nfqws.conf'), false);
		assert.equal(env.files.has(ROOT + '/open.nft'), false);
	}
});

test('down and an explicitly disabled setting retain the owned closed guard', () => {
	for (const disable of [false, true]) {
		const { env, run } = harness();
		assert.deepEqual(run('up'), { ok: true });
		if (disable) env.settings.set('direct.zapret_enabled', '0');
		assert.deepEqual(run(disable ? 'up' : 'down'), { ok: true });
		assert.equal(env.serviceRunning, false);
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.files.get(ROOT + '/plan.json'), 'null\n');
		if (disable) assert.deepEqual(run('check'), { ok: true, enabled: false });
		else assert.equal(run('check').code, 'direct_zapret_unavailable');
		assert.doesNotMatch(env.tableRaw, /queue num|\bbypass\b/);
	}
});

test('invalid UCI, missing engine and listener failure all leave forwarding closed', () => {
	for (const prepare of [
		env => env.settings.set('direct.zapret_enabled', 'yes'),
		env => env.settings.set('runtime.wan_device', 'br-lan'),
		env => env.engines.delete(ENGINE + '/nfqws2'),
		env => { env.validationFails = true; },
		env => { env.listenerAppears = false; },
	]) {
		const { env, run } = harness();
		prepare(env);
		assert.equal(run('up').code, 'direct_zapret_unavailable');
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.serviceRunning, false);
		assert.doesNotMatch(env.tableRaw, /queue num|\bbypass\b/);
	}
});

test('occupied queue and open-table apply failure cannot open the guard', () => {
	for (const prepare of [env => { env.externalQueue = true; }, env => { env.openApplyFails = true; }]) {
		const { env, run } = harness();
		prepare(env);
		assert.equal(run('up').code, 'direct_zapret_unavailable');
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.serviceRunning, false);
		assert.equal(env.applied.some(raw => raw.includes('queue num 20196')), false);
	}
});

test('missing configured WAN uses validated ubus l3_device without persisting UCI', () => {
	const { env, run } = harness();
	env.settings.delete('runtime.wan_device');
	assert.deepEqual(run('up'), { ok: true });
	assert.equal(called(env, '/bin/ubus', 'call', 'network.interface.wan', 'status'), true);
	assert.equal(env.tableRaw, direct.nft(direct.plan('pppoe-wan', true)));
});

test('maintenance and network transaction gates close an already open direct lane', () => {
	for (const prepare of [
		env => env.files.set('/etc/autovpn/state/maintenance.lock', '{}\n'),
		env => env.files.set('/etc/autovpn/state/update.lock', '{}\n'),
		env => { env.networkGateFails = true; },
	]) {
		const { env, run } = harness();
		assert.deepEqual(run('up'), { ok: true });
		prepare(env);
		assert.equal(run('up').code, 'direct_zapret_unavailable');
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.serviceRunning, false);
	}
});

test('foreign table and unsafe RAM directory are never adopted', () => {
	{
		const { env, run } = harness();
		env.tableRaw = 'table inet autovpn_direct_zapret { chain someone_else {} }\n';
		env.serviceRunning = true;
		assert.equal(run('up').code, 'direct_zapret_unavailable');
		assert.match(env.tableRaw, /someone_else/);
		assert.equal(env.serviceRunning, false);
		assert.equal(env.applied.length, 0);
	}
	{
		const { env, run } = harness();
		env.tableRaw = direct.closedNft();
		env.links.add(ROOT);
		assert.equal(run('up').code, 'direct_zapret_unavailable');
		assert.equal(env.tableRaw, direct.closedNft());
		assert.equal(env.writes.length, 0);
	}
});

test('canonical tamper is detected and repaired through a new closed-first transition', () => {
	const { env, run } = harness();
	assert.deepEqual(run('up'), { ok: true });
	env.tableRaw = env.tableRaw.replace('chain raw', 'chain injected {}\n chain raw');
	assert.equal(run('check').code, 'direct_zapret_unavailable');
	assert.deepEqual(run('up'), { ok: true });
	assert.doesNotMatch(env.tableRaw, /injected/);
	assert.deepEqual(run('check'), { ok: true, enabled: true });
});

test('helper API has no input path and service is foreground controller-only', () => {
	const { run } = harness();
	assert.equal(run('up', '/tmp/plan.json').code, 'direct_zapret_unavailable');
	assert.match(serviceSource, /procd_set_param command .*nfqws2 @\/var\/run\/autovpn-direct-zapret\/nfqws\.conf/);
	assert.doesNotMatch(serviceSource, /--daemon|enable\)/);
	assert.match(serviceSource, /boot\(\) \{ return 0; \}/);
});
