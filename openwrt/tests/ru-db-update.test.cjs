'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const test = require('node:test');

const helperPath = path.join(__dirname, '../files/usr/libexec/autovpn/ru-db-helper.uc');
const updaterPath = path.join(__dirname, '../files/usr/libexec/autovpn/ru-db-update');
const helperSource = fs.readFileSync(helperPath, 'utf8');
const updaterSource = fs.readFileSync(updaterPath, 'utf8');

function runHelper(t, geosite, geoip, prepare) {
	const work = fs.mkdtempSync('/tmp/autovpn-ru-db.');
	t.after(() => fs.rmSync(work, { recursive: true, force: true }));
	fs.writeFileSync(path.join(work, 'geosite.json'), typeof geosite === 'string' ? geosite : JSON.stringify(geosite));
	fs.writeFileSync(path.join(work, 'geoip.json'), typeof geoip === 'string' ? geoip : JSON.stringify(geoip));
	if (prepare) prepare(work);
	const source = helperSource.replace(/^#![^\n]*\n/, '').replace(/^import[^\n]*\n/m, '');
	let stdout = '';
	let status;
	const exit = code => { status = code; throw new Error('__exit__'); };
	const invoke = new Function(
		'readfile', 'writefile', 'chmod', 'lstat', 'type', 'length', 'keys', 'join', 'sort', 'match',
		'index', 'split', 'int', 'push', 'substr', 'sprintf', 'json', 'ARGV', 'printf', 'exit', source
	);
	try {
		invoke(
			(file, limit) => {
				const raw = fs.readFileSync(file, 'utf8');
				return Buffer.byteLength(raw) > limit ? raw : raw;
			},
			(file, raw) => { fs.writeFileSync(file, raw); return Buffer.byteLength(raw); },
			(file, mode) => { fs.chmodSync(file, mode); return true; },
			file => {
				try {
					const info = fs.lstatSync(file);
					return { type: info.isDirectory() ? 'directory' : info.isFile() ? 'file' : 'other', uid: 0 };
				} catch { return null; }
			},
			value => value == null ? null : Array.isArray(value) ? 'array' : typeof value === 'boolean' ? 'bool'
				: Number.isInteger(value) ? 'int' : typeof value,
			value => typeof value === 'string' ? Buffer.byteLength(value, 'utf8') : value.length,
			Object.keys, (separator, value) => value.join(separator), value => value.sort(),
			(value, expression) => value.match(expression), (value, needle) => value.indexOf(needle),
			(value, separator) => value.split(separator), value => Number.parseInt(value, 10),
			(array, value) => array.push(value),
			(value, start, count) => count == null ? value.substring(start) : value.substring(start, start + count),
			(format, value) => format === '%J\n' ? JSON.stringify(value) + '\n' : JSON.stringify(value),
			JSON.parse, ['merge', work], (format, value) => { stdout += format === '%J\n' ? JSON.stringify(value) + '\n' : format; }, exit
		);
	} catch (error) {
		if (error.message !== '__exit__') throw error;
	}
	return {
		status,
		json: JSON.parse(stdout),
		output: status === 0 && fs.existsSync(path.join(work, 'ru.json')) ? JSON.parse(fs.readFileSync(path.join(work, 'ru.json'))) : null,
	};
}

const geosite = (override = {}) => ({
	version: 1,
	rules: [{ domain: ['example.ru'], domain_suffix: ['.ru', '.xn--p1ai', 'su'], ...override }],
});
const geoip = values => ({ version: 1, rules: [{ ip_cidr: values }] });

test('helper preserves ASCII seeds, normalizes suffixes and discards IPv6 and non-public IPv4', t => {
	const result = runHelper(t, geosite(), geoip([
		'5.136.0.0/13', '2001:db8::/32', '10.0.0.0/8', '127.0.0.0/8', '224.0.0.0/4',
	]));
	assert.equal(result.status, 0);
	assert.deepEqual(result.json, { ok: true, domains: 4, ipv4_cidrs: 1 });
	assert.deepEqual(result.output, {
		version: 1,
		rules: [
			{ domain: ['example.ru'], domain_suffix: ['ru', 'su', 'xn--p1ai'] },
			{ ip_cidr: ['5.136.0.0/13'] },
		],
	});
	assert.equal(JSON.stringify(result.output).includes('рф'), false);
});

test('helper rejects malformed JSON, arbitrary operators and unsafe domain syntax', t => {
	assert.equal(runHelper(t, '{', geoip(['5.136.0.0/13'])).json.code, 'invalid_geosite');
	assert.equal(runHelper(t, geosite({ domain_regex: ['.*'] }), geoip(['5.136.0.0/13'])).json.code, 'invalid_geosite');
	for (const value of ['*.ru', 'https://example.ru', 'a..ru', '-bad.ru', 'пример.рф'])
		assert.equal(runHelper(t, geosite({ domain: [value] }), geoip(['5.136.0.0/13'])).json.code, 'invalid_geosite', value);
	assert.equal(runHelper(t, geosite(), geoip(['0.0.0.0/0'])).json.code, 'invalid_geoip');
	assert.equal(runHelper(t, { ...geosite(), version: '1' }, geoip(['5.136.0.0/13'])).json.code, 'invalid_geosite');
	assert.equal(runHelper(t, geosite(), geoip(['::::::::/999', '5.136.0.0/13'])).json.code, 'invalid_geoip');
});

test('helper rejects oversized input and never follows a preexisting output symlink', t => {
	assert.equal(runHelper(t, ' '.repeat(2097153), geoip(['5.136.0.0/13'])).json.code, 'invalid_geosite');
	const victim = path.join(os.tmpdir(), `autovpn-ru-victim-${process.pid}`);
	t.after(() => fs.rmSync(victim, { force: true }));
	fs.writeFileSync(victim, 'untouched\n');
	const result = runHelper(t, geosite(), geoip(['5.136.0.0/13']), work => fs.symlinkSync(victim, path.join(work, 'ru.json')));
	assert.equal(result.json.code, 'ru_db_write_failed');
	assert.equal(fs.readFileSync(victim, 'utf8'), 'untouched\n');
});

if (process.env.AUTOVPN_RU_DB_AUDIT_DIR) test('helper accepts the locally audited current upstream decode', t => {
	const directory = process.env.AUTOVPN_RU_DB_AUDIT_DIR;
	const result = runHelper(t,
		JSON.parse(fs.readFileSync(path.join(directory, 'geosite-category-ru.json'))),
		JSON.parse(fs.readFileSync(path.join(directory, 'geoip-ru.json'))));
	assert.equal(result.status, 0);
	assert.deepEqual(result.json, { ok: true, domains: 1092, ipv4_cidrs: 8681 });
	assert.ok(Buffer.byteLength(JSON.stringify(result.output)) <= 2097152);
});

function fixture(t) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-ru-update-test-'));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	const bin = path.join(root, 'bin');
	const assets = path.join(root, 'assets');
	const routingParent = path.join(root, 'etc/autovpn');
	const routing = path.join(routingParent, 'routing');
	const state = path.join(routingParent, 'state');
	const run = path.join(root, 'run');
	const statusFile = path.join(run, 'autovpn-ru-db-status.json');
	const lock = path.join(run, 'autovpn-ru-db.lock');
	const sourceFile = path.join(routing, 'ru.json');
	const log = path.join(root, 'calls.jsonl');
	fs.mkdirSync(bin);
	fs.mkdirSync(assets);
	fs.mkdirSync(routing, { recursive: true });
	fs.mkdirSync(state);
	fs.mkdirSync(run);
	fs.writeFileSync(sourceFile, 'seeded-old-dataset\n');
	fs.writeFileSync(path.join(assets, 'geosite.json'), JSON.stringify(geosite()));
	fs.writeFileSync(path.join(assets, 'geoip.json'), JSON.stringify(geoip(['5.136.0.0/13', '2001:db8::/32', '10.0.0.0/8'])));

	const mock = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const env = process.env;
const record = value => fs.appendFileSync(env.MOCK_LOG, JSON.stringify(value) + '\\n');
if (name === 'stat') {
  const target = args.at(-1);
  process.stdout.write(env.MOCK_NONROOT && target.includes(env.MOCK_NONROOT) ? '501\\n' : '0\\n');
} else if (name === 'date') process.stdout.write((env.MOCK_NOW || '10000') + '\\n');
else if (name === 'uci') {
  if (env.MOCK_UCI_MISSING === '1') process.exit(1);
  process.stdout.write((env.MOCK_UCI === undefined ? '1' : env.MOCK_UCI) + '\\n');
} else if (name === 'curl') {
  record({ name, args });
  if (env.MOCK_CURL_FAIL === '1') process.exit(22);
  if (env.MOCK_CURL_DELAY_MS) Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(env.MOCK_CURL_DELAY_MS));
  const output = args[args.indexOf('--output') + 1];
  if (env.MOCK_OVERSIZE === '1') fs.writeFileSync(output, Buffer.alloc(262145, 65));
  else fs.writeFileSync(output, 'fixture-srs\\n');
} else if (name === 'sing-box') {
  const output = args[args.indexOf('-o') + 1];
  if (args.includes('decompile')) {
    const input = args.at(-1);
    if (env.MOCK_BAD_JSON === '1' && path.basename(input) === 'geosite.srs') fs.writeFileSync(output, '{');
    else fs.copyFileSync(path.join(env.MOCK_ASSETS, path.basename(input).replace('.srs', '.json')), output);
  } else {
    JSON.parse(fs.readFileSync(args.at(-1), 'utf8'));
    fs.writeFileSync(output, 'compiled\\n');
    if (env.MOCK_GATE_ON_COMPILE === '1') fs.writeFileSync(path.join(env.MOCK_STATE, 'maintenance.lock'), '1');
  }
} else if (name === 'ucode') {
  try {
    const work = args.at(-1);
    const site = JSON.parse(fs.readFileSync(path.join(work, 'geosite.json')));
    const ip = JSON.parse(fs.readFileSync(path.join(work, 'geoip.json')));
    if (Object.keys(site).sort().join(',') !== 'rules,version' || Object.keys(ip).sort().join(',') !== 'rules,version') process.exit(1);
    const domains = [...new Set(site.rules[0].domain)];
    const suffixes = [...new Set(['ru', 'xn--p1ai', 'su', ...site.rules[0].domain_suffix.map(v => v.replace(/^\\./, ''))])].sort();
    const cidrs = ip.rules[0].ip_cidr.filter(v => !v.includes(':') && !v.startsWith('10.'));
    fs.writeFileSync(path.join(work, 'ru.json'), JSON.stringify({ version: 1, rules: [{ domain: domains, domain_suffix: suffixes }, { ip_cidr: cidrs }] }) + '\\n');
    process.stdout.write(JSON.stringify({ ok: true, domains: domains.length + suffixes.length, ipv4_cidrs: cidrs.length }) + '\\n');
  } catch { process.exit(1); }
} else if (name === 'jsonfilter') {
  try {
    const value = JSON.parse(fs.readFileSync(args[args.indexOf('-i') + 1], 'utf8'));
    const field = args[args.indexOf('-e') + 1].slice(2);
    if (value[field] !== null && value[field] !== undefined) process.stdout.write(String(value[field]) + '\\n');
  } catch { process.exit(1); }
} else if (name === 'timeout') {
  const result = spawnSync(args[1], args.slice(2), { stdio: 'inherit', env });
  process.exit(result.status === null ? 1 : result.status);
} else throw new Error('unexpected mock ' + name);
`;
	for (const name of ['stat', 'date', 'uci', 'curl', 'sing-box', 'ucode', 'jsonfilter', 'timeout'])
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	const python = spawnSync('/bin/sh', ['-c', 'command -v python3'], { encoding: 'utf8' }).stdout.trim();
	fs.writeFileSync(path.join(bin, 'flock'), `#!${python}\nimport fcntl, os, sys\nfd = int(sys.argv[-1])\ntry:\n    op = fcntl.LOCK_UN if '-u' in sys.argv else fcntl.LOCK_EX | fcntl.LOCK_NB\n    fcntl.flock(fd, op)\nexcept BlockingIOError:\n    sys.exit(1)\n`, { mode: 0o755 });

	const script = path.join(root, 'ru-db-update');
	const transformed = updaterSource
		.replace('HELPER=/usr/libexec/autovpn/ru-db-helper.uc', `HELPER=${path.join(root, 'ru-db-helper.uc')}`)
		.replace('ROUTING_PARENT=/etc/autovpn', `ROUTING_PARENT=${routingParent}`)
		.replace('ROUTING_DIR=/etc/autovpn/routing', `ROUTING_DIR=${routing}`)
		.replace('SOURCE=/etc/autovpn/routing/ru.json', `SOURCE=${sourceFile}`)
		.replace('STATUS=/var/run/autovpn-ru-db-status.json', `STATUS=${statusFile}`)
		.replace('LOCK=/var/run/autovpn-ru-db.flock', `LOCK=${lock}`)
		.replace('STATUS_PARENT=/var/run', `STATUS_PARENT=${run}`)
		.replaceAll('/etc/autovpn/state/', state + '/');
	fs.writeFileSync(script, transformed, { mode: 0o755 });
	const environment = extra => ({
		...process.env, PATH: bin + ':' + process.env.PATH, MOCK_LOG: log, MOCK_ASSETS: assets,
		MOCK_STATE: state, ...extra,
	});
	return {
		root, routing, state, run, statusFile, lock, sourceFile, log,
		start(extra = {}) {
			return spawn('/bin/sh', [script, 'tick'], { encoding: 'utf8', env: environment(extra) });
		},
		run(args = ['tick'], extra = {}) {
			const result = spawnSync('/bin/sh', [script, ...args], { encoding: 'utf8', timeout: 30000, env: environment(extra) });
			assert.equal(result.error, undefined);
			const lines = result.stdout.trim().split('\n').filter(Boolean);
			assert.equal(lines.length, 1, `stdout must be one JSON line: ${result.stdout}\nstderr: ${result.stderr}`);
			return { ...result, json: JSON.parse(lines[0]), calls: fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [] };
		},
	};
}

test('successful tick downloads only fixed sources, validates and atomically publishes version 1 JSON', t => {
	const env = fixture(t);
	const result = env.run(['tick'], { MOCK_UCI_MISSING: '1' });
	assert.equal(result.status, 0, result.stderr);
	assert.deepEqual(result.json, { ok: true, code: null, last_attempt: 10000, last_success: 10000, domains: 4, ipv4_cidrs: 1 });
	assert.equal(fs.statSync(env.sourceFile).mode & 0o777, 0o600);
	assert.equal(JSON.parse(fs.readFileSync(env.sourceFile)).version, 1);
	const curls = result.calls.filter(call => call.name === 'curl');
	assert.deepEqual(curls.map(call => call.args.at(-1)), [
		'https://raw.githubusercontent.com/SagerNet/sing-geosite/rule-set/geosite-category-ru.srs',
		'https://raw.githubusercontent.com/SagerNet/sing-geoip/rule-set/geoip-ru.srs',
	]);
	assert.equal(curls.some(call => call.args.includes('--location') || call.args.includes('-K') || call.args.some(arg => /credential|password/i.test(arg))), false);
	assert.equal(fs.existsSync(env.lock), true);
});

test('download failure exhausts only the two fixed SOCKS fallbacks and preserves old data', t => {
	const env = fixture(t);
	const result = env.run(['tick'], { MOCK_CURL_FAIL: '1' });
	assert.equal(result.status, 1);
	assert.equal(result.json.code, 'geosite_download_failed');
	assert.equal(fs.readFileSync(env.sourceFile, 'utf8'), 'seeded-old-dataset\n');
	const calls = result.calls.filter(call => call.name === 'curl');
	assert.equal(calls.length, 3);
	assert.deepEqual(calls.map(call => call.args.includes('--proxy') ? call.args[call.args.indexOf('--proxy') + 1] : null), [
		null, 'socks5h://127.0.0.1:1088', 'socks5h://127.0.0.1:1108',
	]);
});

test('oversized downloads and bad decoded JSON never replace the seed', t => {
	for (const [extra, code] of [[{ MOCK_OVERSIZE: '1' }, 'source_oversized'], [{ MOCK_BAD_JSON: '1' }, 'merge_failed']]) {
		const env = fixture(t);
		const result = env.run(['tick'], extra);
		assert.equal(result.status, 1);
		assert.equal(result.json.code, code);
		assert.equal(fs.readFileSync(env.sourceFile, 'utf8'), 'seeded-old-dataset\n');
	}
});

test('disabled mode performs no download while a missing option defaults enabled', t => {
	const disabled = fixture(t);
	const skipped = disabled.run(['tick'], { MOCK_UCI: '0' });
	assert.equal(skipped.status, 0);
	assert.equal(skipped.json.code, 'ru_bypass_disabled');
	assert.equal(skipped.calls.length, 0);
	const defaulted = fixture(t);
	assert.equal(defaulted.run(['tick'], { MOCK_UCI_MISSING: '1' }).json.ok, true);
	assert.equal(defaulted.run(['status']).json.last_success, 10000);
});

test('failed attempts cool down for one hour and successful data refreshes daily without flash rewrite', t => {
	const failed = fixture(t);
	assert.equal(failed.run(['tick'], { MOCK_NOW: '10000', MOCK_CURL_FAIL: '1' }).json.code, 'geosite_download_failed');
	const firstCalls = fs.readFileSync(failed.log, 'utf8');
	const cooldown = failed.run(['tick'], { MOCK_NOW: '10500' });
	assert.equal(cooldown.status, 0);
	assert.equal(cooldown.json.code, 'cooldown');
	assert.equal(fs.readFileSync(failed.log, 'utf8'), firstCalls);

	const current = fixture(t);
	assert.equal(current.run(['tick'], { MOCK_NOW: '10000' }).status, 0);
	const inode = fs.statSync(current.sourceFile).ino;
	assert.equal(current.run(['tick'], { MOCK_NOW: '11000' }).json.code, 'not_due');
	assert.equal(current.run(['tick'], { MOCK_NOW: '100001' }).status, 0);
	assert.equal(fs.statSync(current.sourceFile).ino, inode, 'identical source is not rewritten');
});

test('kernel lock excludes concurrent work, abandoned lock files recover, and foreign targets are preserved', async t => {
	const active = fixture(t);
	const first = active.start({ MOCK_CURL_DELAY_MS: '500' });
	for (let attempt = 0; attempt < 100; attempt++) {
		if (fs.existsSync(active.statusFile) && fs.readFileSync(active.statusFile, 'utf8').includes('update_in_progress')) break;
		await new Promise(resolve => setTimeout(resolve, 20));
	}
	assert.equal(active.run(['tick']).json.code, 'ru_db_busy');
	assert.equal(await new Promise(resolve => first.once('close', resolve)), 0);
	assert.equal(fs.existsSync(active.lock), true);

	const stale = fixture(t);
	fs.writeFileSync(stale.lock, 'abandoned-lock-file\n');
	assert.equal(stale.run(['tick']).status, 0);
	assert.equal(fs.existsSync(stale.lock), true);

	const foreign = fixture(t);
	fs.mkdirSync(foreign.lock);
	fs.writeFileSync(path.join(foreign.lock, 'do-not-delete'), 'foreign\n');
	assert.equal(foreign.run(['tick']).json.code, 'unsafe_ru_db_lock');
	assert.equal(fs.readFileSync(path.join(foreign.lock, 'do-not-delete'), 'utf8'), 'foreign\n');
});

test('a gate appearing during validation blocks publication and status is sanitized', t => {
	const env = fixture(t);
	const result = env.run(['tick'], { MOCK_GATE_ON_COMPILE: '1' });
	assert.equal(result.status, 1);
	assert.equal(result.json.code, 'maintenance_locked');
	assert.equal(fs.readFileSync(env.sourceFile, 'utf8'), 'seeded-old-dataset\n');
	assert.deepEqual(env.run(['status']).json, {
		ok: false, code: 'maintenance_locked', last_attempt: 10000, last_success: 0, domains: 0, ipv4_cidrs: 0,
	});
});

test('status rejects non-canonical numeric JSON without touching persistent data', t => {
	const env = fixture(t);
	fs.writeFileSync(env.statusFile, '{"ok":true,"code":null,"last_attempt":"1","last_success":0,"domains":0,"ipv4_cidrs":0}\n');
	const result = env.run(['status']);
	assert.equal(result.status, 1);
	assert.equal(result.json.code, 'invalid_ru_db_status');
	assert.equal(fs.readFileSync(env.sourceFile, 'utf8'), 'seeded-old-dataset\n');
});

test('source pins HTTPS URLs, bounded transfers and bounded compiler invocations', () => {
	assert.match(updaterSource, /--proto '=https'/);
	assert.match(updaterSource, /--connect-timeout 3 --max-time 6/);
	assert.match(updaterSource, /timeout 20 sing-box rule-set decompile/);
	assert.match(updaterSource, /timeout 20 sing-box rule-set compile/);
	assert.match(updaterSource, /MAX_SRS_BYTES=262144/);
	assert.doesNotMatch(updaterSource, /--location|releases\/latest|\beval\b/);
});
