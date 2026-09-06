'use strict';

// Contract tests for the bounded zapret diagnostics runner. Diagnostics may
// temporarily change only the owned zapret runtime and apply an explicitly
// selected result; LuCI must never expose a shell-shaped API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const runner = path.join(root, 'files/usr/libexec/autovpn/zapret-diagnostics');
const strategy = path.join(root, 'files/usr/share/ucode/autovpn/zapret_diagnostics.uc');
const rpc = path.join(root, 'files/usr/share/rpcd/ucode/luci.autovpn');
const makefile = path.join(root, 'Makefile');

function source(file) {
	assert.ok(fs.existsSync(file), `missing diagnostics contract file: ${file}`);
	return fs.readFileSync(file, 'utf8');
}

test('diagnostics runner is a root-only RAM-state executable', () => {
	const text = source(runner);
	assert.match(text, /^#!\/bin\/sh\b|^#!\/usr\/bin\/ucode\b/m);
	assert.match(text, /\/tmp\/autovpn-zapret-diagnostics/);
	assert.match(text, /SOURCE_PLAN=\/etc\/autovpn\/runtime-zapret\/zapret\.json/);
	assert.match(text, /restore_direct|restore_transport/);
	assert.doesNotMatch(text, /eval\s|sh\s+-c|curl[^\n]*\$\{?[A-Za-z_]*(URL|DOMAIN)/);
	assert.match(text, /\bflock\b|maintenance\.lock/);
	assert.match(text, /chmod\s+700|install\s+-m\s+700|umask\s+077/);
	assert.match(text, /DIAGNOSTIC_LOCK=.*autovpn-zapret-diagnostics\.lock/);
	assert.match(text, /\/usr\/bin\/timeout -k 15/);
	assert.match(text, /TEST=custom/);
	assert.match(text, /IPVS=4/);
	assert.match(text, /\^\\\* COMMON/);
	assert.doesNotMatch(text, /\^\\\* SUMMARY/);
	assert.match(text, /validate-result/);
});

test('strategy module exposes typed bounded candidates, not command strings', () => {
	const text = source(strategy);
	assert.match(text, /module\.exports|return\s+\{/);
	assert.match(text, /valid|validate/);
	assert.match(text, /CANDIDATES/);
	assert.match(text, /direct|vless|hysteria2/);
	assert.doesNotMatch(text, /shell|system\s*\(|popen\s*\(.*input|eval\s*\(/);
});

test('diagnostics RPC has no arbitrary URL, domain, command, or Lua arguments', () => {
	const text = source(rpc);
	assert.match(text, /zapret[_-]diagn|diagnostics/);
	assert.doesNotMatch(text, /diagnostics[^\n]*args\.(url|domain|command|cmd|lua)/i);
	assert.doesNotMatch(text, /diagnosticsApply\([^)]*(url|domain|command|cmd|lua)/i);
	assert.doesNotMatch(text, /popen\([^\n]*request\.args/);
});

test('diagnostics runner, typed module, RPC and UI are packaged', () => {
	const text = source(makefile);
	assert.match(text, /zapret-diagnostics/);
	assert.match(text, /zapret_diagnostics\.uc/);
	assert.match(text, /luci\.autovpn/);
});
