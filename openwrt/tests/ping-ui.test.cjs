'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');

test('ping-all RPC has a fixed target allowlist and bounded controller output', () => {
	const source = read('files/usr/share/rpcd/ucode/luci.autovpn');
	assert.match(source, /ping_all:\s*\{/);
	assert.match(source, /target != 'youtube' && target != 'instagram'/);
	assert.match(source, /callController\('ping-all', target\)/);
	assert.match(source, /process\.read\(4097\)/);
	assert.doesNotMatch(source, /ping_all[\s\S]*credential/);
});

test('ping-all is ACL protected and UI keeps inspection manual', () => {
	const acl = JSON.parse(read('files/usr/share/rpcd/acl.d/luci-app-autovpn.json'));
	assert.ok(acl['luci-app-autovpn'].write.ubus['luci.autovpn'].includes('ping_all'));
	const overview = read('files/www/luci-static/resources/view/autovpn/overview.js');
	assert.match(overview, /timeout: 30000/);
	assert.match(overview, /Ping all checks each candidate with HTTPS without changing the active VPN/);
	assert.match(overview, /result\.latency_ms/);
	assert.match(overview, /result\.http_status/);
	assert.doesNotMatch(overview, /innerHTML/);
	const settings = read('files/www/luci-static/resources/view/autovpn/settings.js');
	assert.match(settings, /three health-check rounds fail/);
	assert.match(settings, /Never switches for a lower latency/);
});
