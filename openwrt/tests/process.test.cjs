'use strict';

const assert = require('node:assert/strict');
const childProcess = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '..', 'files/usr/share/ucode/autovpn/process.uc'), 'utf8')
	.replace(/^import\s+.*?;\s*$/gm, '');

function type(value) {
	if (value == null) return null;
	if (Array.isArray(value)) return 'array';
	return typeof value;
}

function moduleFor(nativePopen) {
	return new Function('nativePopen', 'type', 'length', 'match', 'index', 'replace', source)(
		nativePopen,
		type,
		value => typeof value === 'string' ? Buffer.byteLength(value) : value.length,
		(value, expression) => value.match(expression),
		(value, needle) => value.indexOf(needle),
		(value, expression, replacement) => value.replace(expression, replacement),
	);
}

test('quotes every argv element before stable 25.12 fs.popen string API', () => {
	let captured;
	const process = moduleFor((command, mode) => { captured = { command, mode }; return {}; });
	process.popen(['/usr/bin/printf', '%s', "one'; touch /definitely-not-created; echo '", '$(not-expanded)', 'two words'], 'r');
	assert.equal(captured.mode, 'r');
	assert.equal(captured.command, "'/usr/bin/printf' '%s' 'one'\\''; touch /definitely-not-created; echo '\\''' '$(not-expanded)' 'two words'");
	const local = childProcess.spawnSync('/bin/sh', ['-c', captured.command], { encoding: 'utf8' });
	assert.equal(local.status, 0, local.stderr);
	assert.equal(local.stdout, "one'; touch /definitely-not-created; echo '$(not-expanded)two words");
});

test('rejects non-absolute executables, invalid values and unsupported modes', () => {
	let calls = 0;
	const process = moduleFor(() => { calls++; return {}; });
	for (const [argv, mode] of [
		[['printf', 'x'], 'r'],
		[['/bin/printf', 'x\0y'], 'r'],
		[['/bin/printf', 1], 'r'],
		[[], 'r'],
		[['/bin/printf'], 'a'],
	]) assert.equal(process.popen(argv, mode), null);
	assert.equal(calls, 0);
});
