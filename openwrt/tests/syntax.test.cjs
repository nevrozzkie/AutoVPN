'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const sources = [
	'files/usr/libexec/autovpn/controller.uc',
	'files/usr/share/rpcd/ucode/luci.autovpn',
	'files/usr/share/ucode/autovpn/state.uc',
	'files/usr/share/ucode/autovpn/journal.uc',
	'files/usr/share/ucode/autovpn/health.uc',
	'files/usr/share/ucode/autovpn/probes.uc',
	'files/usr/libexec/autovpn/probe-helper.uc',
	'files/usr/libexec/autovpn/zapret-helper.uc',
	'files/usr/share/ucode/autovpn/zapret.uc',
	'files/usr/share/ucode/autovpn/orchestration.uc',
	'files/usr/share/ucode/autovpn/http.uc',
	'files/usr/share/ucode/autovpn/runtime.uc',
	'files/usr/share/ucode/autovpn/process.uc',
	'files/usr/libexec/autovpn/runtime-helper.uc',
	'files/usr/libexec/autovpn/awg-helper.uc',
	'files/usr/libexec/autovpn/network-helper.uc',
	'files/usr/libexec/autovpn/setup-helper.uc',
	'files/usr/libexec/autovpn/wifi-bootstrap.uc',
	'files/usr/libexec/autovpn/maintenance-helper.uc',
	'files/usr/share/ucode/autovpn/networks.uc',
	'files/usr/share/ucode/autovpn/network-transaction.uc',
	'files/usr/share/ucode/autovpn/setup-policy.uc',
	'files/usr/libexec/autovpn/http-helper.uc'
];

test('ucode sources stay inside the host-parseable ECMAScript subset', () => {
	for (const relative of sources) {
		const source = fs.readFileSync(path.join(root, relative), 'utf8')
			.replace(/^#![^\n]*\n/, '')
			.replace(/^import\s+.*?;\s*$/gm, '');
		assert.doesNotThrow(() => new Function(source), relative);
	}
});

test('controller uses bounded timeout argv helpers without shell interpolation', () => {
	const source = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/controller.uc'), 'utf8');
	assert.match(source, /orchestration\.boundedInteger\(uci\.get\('autovpn', 'main', 'connect_timeout'\), 10, 1, 60\)/);
	assert.match(source, /orchestration\.boundedInteger\(uci\.get\('autovpn', 'main', 'request_timeout'\), 30, 2, 120\)/);
	assert.match(source, /adapterCall\(orchestration\.fetchArguments\(config, etag\)\)/);
	assert.match(source, /adapterCall\(orchestration\.putResultArguments\(/);
	assert.match(source, /process\.read\(MAX_ADAPTER_OUTPUT \+ 1\)/);
	assert.match(source, /readfile\(response\.response_file, MAX_SNAPSHOT_BYTES \+ 1\)/);
	assert.match(source, /orchestration\.guardJournal\(state, operationsFor\(config\)\)/);
	assert.match(source, /snapshot\.body\.router_id != config\.router_id/);
	assert.match(source, /code: 'router_identity_mismatch'/);
	assert.match(source, /if \(action != 'fail-closed'\)\s+push\(argv, config\.state_dir/);
	assert.doesNotMatch(source, /popen\s*\(\s*`/);
});

test('package has an exact curl dependency and adapter never puts payload in curl argv', () => {
	const makefile = fs.readFileSync(path.join(root, 'Makefile'), 'utf8');
	const adapter = fs.readFileSync(path.join(root, 'files/usr/libexec/autovpn/http-adapter'), 'utf8');
	assert.match(makefile, /DEPENDS:=.*\+busybox(?:\s|$)/);
	assert.match(makefile, /DEPENDS:=.*\+curl(?:\s|$)/);
	assert.match(adapter, /"\$CURL_BIN" --disable --config "\$config"/);
	assert.match(adapter, /head 2>\/dev\/null/);
	assert.match(adapter, /mkfifo 2>\/dev\/null/);
	assert.match(adapter, /STREAM_CAPTURE_BYTES=16385/);
	assert.doesNotMatch(adapter, /curl[^\n]*(Authorization|Bearer|data-binary)/);
});

test('menu-bound ACL grants the authenticated first-run action but no file access', () => {
	const acl = JSON.parse(fs.readFileSync(path.join(root, 'files/usr/share/rpcd/acl.d/luci-app-autovpn.json'), 'utf8'));
	const menu = JSON.parse(fs.readFileSync(path.join(root, 'files/usr/share/luci/menu.d/luci-app-autovpn.json'), 'utf8'));
	assert.deepEqual(Object.keys(acl), ['luci-app-autovpn']);
	assert.deepEqual(acl['luci-app-autovpn'].read.ubus['luci.autovpn'], ['status', 'network_status', 'update_status', 'zapret_install_status']);
	assert.deepEqual(acl['luci-app-autovpn'].write.ubus['luci.autovpn'], ['refresh', 'apply_policy', 'ping_all', 'zapret_install', 'network_setup', 'network_confirm', 'setup_configure', 'setup_activate', 'maintenance_action', 'maintenance_resume', 'update_check', 'update_apply']);
	assert.deepEqual(acl['luci-app-autovpn'].write.uci, ['autovpn']);
	assert.deepEqual(menu['admin/services/autovpn'].depends.acl, ['luci-app-autovpn']);
	assert.equal(JSON.stringify(acl).includes('/etc/autovpn'), false);
	assert.equal(JSON.stringify(acl).includes('snapshot'), false);
});
