'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawnSync } = require('node:child_process');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../files/usr/libexec/autovpn/install-wifi'), 'utf8');

function fixture(t) {
	const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-install-wifi-test-'));
	t.after(() => fs.rmSync(root, { recursive: true, force: true }));
	const bin = path.join(root, 'bin');
	const ttyIn = path.join(root, 'tty.in');
	const ttyOut = path.join(root, 'tty.out');
	const log = path.join(root, 'calls.jsonl');
	const expectedSsid = path.join(root, 'expected-ssid');
	const expectedPassword = path.join(root, 'expected-password');
	fs.mkdirSync(bin);
	const mock = `#!${process.execPath}
const fs = require('node:fs');
const path = require('node:path');
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
const env = process.env;
const output = value => process.stdout.write(typeof value === 'string' ? value + '\\n' : JSON.stringify(value));
const log = value => fs.appendFileSync(env.MOCK_LOG, JSON.stringify(value) + '\\n');
if (name === 'id') output('0');
else if (name === 'stty') {
  log({name,args});
  if (env.MOCK_STTY_FAIL === '1' && args.includes('-echo')) process.exit(1);
  if (args.includes('-g')) output('fixture-state');
} else if (name === 'jsonfilter') {
  try {
    let value = JSON.parse(fs.readFileSync(0, 'utf8'));
    const expr = args[args.indexOf('-e') + 1];
    for (const part of expr.slice(1).match(/[a-z_][a-z0-9_]*/g) || []) value = value[part];
    if (value === undefined) process.exit(1);
    output(value);
  } catch (error) { process.exit(1); }
} else if (name === 'autovpnctl') {
  const input = fs.readFileSync(0, 'utf8');
  const secret = fs.readFileSync(env.EXPECTED_PASSWORD_FILE, 'utf8');
  const ssid = fs.readFileSync(env.EXPECTED_SSID_FILE, 'utf8');
  const argvLeak = args.some(arg => arg.includes(secret));
  const envLeak = Object.entries(env).some(([key, value]) => key !== 'EXPECTED_PASSWORD_FILE' && String(value).includes(secret));
  if (args[0] === 'wifi-bootstrap-resume') {
    log({name,args,stdinEmpty:input === '',argvLeak,envLeak});
    if (env.MOCK_RESUME_FAIL === '1') process.exit(1);
    if (env.MOCK_RESUME_CONFIRMED === '1') output({ok:true,phase:'confirmed',transaction_id:'123-7'});
    else if (env.MOCK_RESUME_PENDING === '1') output({ok:true,phase:'pending',transaction_id:'123-7'});
    else output({ok:true,phase:'not_configured'});
  } else if (args[0] === 'wifi-bootstrap') {
    log({name,args,stdinMatches:input === ssid + '\\n' + secret + '\\n',argvLeak,envLeak});
    if (env.MOCK_BOOTSTRAP_FAIL === '1') process.exit(1);
    if (env.MOCK_BAD_RESPONSE === '1') output({ok:true,phase:'pending',transaction_id:'bad\\"id'});
    else output({ok:true,phase:'pending',transaction_id:'123-7'});
  } else if (args[0] === 'network-confirm') {
    log({name,args,stdinEmpty:input === '',argvLeak,envLeak});
    if (env.MOCK_CONFIRM_FAIL === '1') process.exit(1);
    output({ok:true,phase:'confirmed'});
  } else process.exit(64);
} else throw new Error('unexpected mock tool ' + name);
`;
	for (const name of ['id', 'stty', 'jsonfilter', 'autovpnctl'])
		fs.writeFileSync(path.join(bin, name), mock, { mode: 0o755 });
	const helper = source
		.replace("TTY_IN='/dev/tty'", `TTY_IN='${ttyIn}'`)
		.replace("TTY_OUT='/dev/tty'", `TTY_OUT='${ttyOut}'`)
		.replace("CTL='/usr/sbin/autovpnctl'", `CTL='${path.join(bin, 'autovpnctl')}'`);
	const script = path.join(root, 'install-wifi');
	fs.writeFileSync(script, helper, { mode: 0o755 });
	return {
		root,
		run(lines, { ssid = 'wifi', password = 'fixture-pass', env = {} } = {}) {
			fs.writeFileSync(ttyIn, lines.join('\n') + (lines.length ? '\n' : ''));
			fs.writeFileSync(ttyOut, '');
			fs.writeFileSync(expectedSsid, ssid);
			fs.writeFileSync(expectedPassword, password);
			const result = spawnSync('/bin/sh', [script], {
				encoding: 'utf8', timeout: 10000,
				env: {
					...process.env, PATH: bin + ':' + process.env.PATH, MOCK_LOG: log,
					EXPECTED_SSID_FILE: expectedSsid, EXPECTED_PASSWORD_FILE: expectedPassword, ...env
				}
			});
			assert.equal(result.error, undefined);
			const calls = fs.existsSync(log) ? fs.readFileSync(log, 'utf8').trim().split('\n').filter(Boolean).map(JSON.parse) : [];
			return { ...result, calls, tty: fs.readFileSync(ttyOut, 'utf8') };
		}
	};
}

test('source reads both hidden passwords from the TTY and sends only a two-line stdin protocol', () => {
	assert.match(source, /exec 3<"\$TTY_IN"/);
	assert.match(source, /stty -echo <&3/);
	assert.match(source, /read -r secret <&3/);
	assert.match(source, /printf '%s\\n%s\\n' "\$base_ssid" "\$password" \| "\$CTL" wifi-bootstrap/);
	assert.match(source, /"\$CTL" wifi-bootstrap-resume <\/dev\/null/);
	assert.doesNotMatch(source, /(?:wifi-bootstrap|network-confirm).*\$password/);
	assert.match(source, /trap restore_tty EXIT/);
	assert.doesNotMatch(source, /bootstrap_completed|uci (?:set|commit)/);
});

test('metacharacters remain stdin data, passwords stay hidden, and confirmation uses only the safe id', t => {
	const f = fixture(t);
	const ssid = 'My "wifi"\\home';
	const password = 'p@ss"\\$;touch-x';
	const marker = path.join(f.root, 'injected');
	const result = f.run([ssid, password, password, 'y'], { ssid, password });
	assert.equal(result.status, 0, result.stderr + result.tty);
	const calls = result.calls.filter(call => call.name === 'autovpnctl');
	assert.deepEqual(calls.map(call => call.args), [['wifi-bootstrap-resume'], ['wifi-bootstrap'], ['network-confirm', '123-7']]);
	assert.equal(calls[1].stdinMatches, true);
	assert.equal(calls.some(call => call.argvLeak || call.envLeak), false);
	assert.equal(result.tty.includes(password), false);
	assert.equal(result.stdout.includes(password) || result.stderr.includes(password), false);
	assert.equal(fs.existsSync(marker), false);
	assert.match(result.tty, /My "wifi"\\home-вз/);
});

test('empty SSID input explicitly accepts wifi and 27 UTF-8 bytes fit every suffix', t => {
	let f = fixture(t);
	let result = f.run(['', 'fixture-pass', 'fixture-pass', 'Y']);
	assert.equal(result.status, 0, result.tty);
	assert.equal(result.calls.find(call => call.name === 'autovpnctl' && call.args[0] === 'wifi-bootstrap').stdinMatches, true);
	f = fixture(t);
	const ssid = 'я'.repeat(13) + 'a';
	const hexadecimalPsk = 'a'.repeat(64);
	result = f.run([ssid, hexadecimalPsk, hexadecimalPsk, 'yes'], { ssid, password: hexadecimalPsk });
	assert.equal(Buffer.byteLength(ssid), 27);
	assert.equal(result.status, 0, result.tty);
	assert.match(result.tty, new RegExp(ssid + '-вз'));
});

test('oversized SSID, weak password, and password mismatch fail before controller mutation', t => {
	for (const entry of [
		{ lines: ['я'.repeat(14)], ssid: 'я'.repeat(14), message: /1-27 UTF-8 bytes/ },
		{ lines: ['wifi', ''], password: '', message: /8-63 printable ASCII/ },
		{ lines: ['wifi', 'short'], password: 'short', message: /8-63 printable ASCII/ },
		{ lines: ['wifi', 'fixture-pass', 'different-pass'], message: /Passwords do not match/ }
	]) {
		const result = fixture(t).run(entry.lines, entry);
		assert.notEqual(result.status, 0);
		assert.match(result.tty, entry.message);
		assert.deepEqual(result.calls.filter(call => call.name === 'autovpnctl').map(call => call.args), [['wifi-bootstrap-resume']]);
	}
});

test('EOF or stty failure restores the saved echo state', t => {
	let result = fixture(t).run(['wifi']);
	assert.notEqual(result.status, 0);
	assert.deepEqual(result.calls.filter(call => call.name === 'stty').map(call => call.args), [['-g'], ['-echo'], ['fixture-state']]);
	result = fixture(t).run(['wifi', 'fixture-pass'], { env: { MOCK_STTY_FAIL: '1' } });
	assert.notEqual(result.status, 0);
	assert.deepEqual(result.calls.filter(call => call.name === 'stty').map(call => call.args), [['-g'], ['-echo']]);
});

test('declining readiness never confirms and leaves automatic rollback armed', t => {
	const result = fixture(t).run(['wifi', 'fixture-pass', 'fixture-pass', 'n']);
	assert.notEqual(result.status, 0);
	assert.deepEqual(result.calls.filter(call => call.name === 'autovpnctl').map(call => call.args), [['wifi-bootstrap-resume'], ['wifi-bootstrap']]);
	assert.match(result.tty, /automatic rollback remains armed/);
});

test('malformed controller output cannot become a confirmation argv', t => {
	const result = fixture(t).run(['wifi', 'fixture-pass', 'fixture-pass', 'y'], { env: { MOCK_BAD_RESPONSE: '1' } });
	assert.notEqual(result.status, 0);
	assert.deepEqual(result.calls.filter(call => call.name === 'autovpnctl').map(call => call.args), [['wifi-bootstrap-resume'], ['wifi-bootstrap']]);
	assert.match(result.tty, /Invalid Wi-Fi transaction identifier/);
});

test('confirmed bootstrap recovery completes without opening the TTY or prompting again', t => {
	const f = fixture(t);
	const result = f.run([], { env: { MOCK_RESUME_CONFIRMED: '1' } });
	assert.equal(result.status, 0, result.stderr);
	assert.deepEqual(result.calls.filter(call => call.name === 'autovpnctl').map(call => call.args), [['wifi-bootstrap-resume']]);
	assert.equal(result.calls.find(call => call.name === 'autovpnctl').stdinEmpty, true);
	assert.equal(result.tty, '');
	assert.match(result.stderr, /confirmation recovered/);
});

test('pending or failed bootstrap recovery refuses before asking for credentials', t => {
	for (const env of [{ MOCK_RESUME_PENDING: '1' }, { MOCK_RESUME_FAIL: '1' }]) {
		const result = fixture(t).run([], { env });
		assert.notEqual(result.status, 0);
		assert.deepEqual(result.calls.filter(call => call.name === 'autovpnctl').map(call => call.args), [['wifi-bootstrap-resume']]);
		assert.equal(result.tty, '');
	}
});
