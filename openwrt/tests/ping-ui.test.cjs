'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.join(__dirname, '..');

function translate(value) {
	return {
		text: value,
		format: function() {
			let index = 0;
			const args = arguments;
			return value.replace(/%s/g, function() { return String(args[index++]); });
		},
		toString: function() { return value; }
	};
}

function evaluate(relative, context) {
	const source = fs.readFileSync(path.join(root, relative), 'utf8');
	return vm.runInNewContext('(function() {\n' + source + '\n})()', context, { filename: relative });
}

function settingsFixture() {
	const maps = [];
	function Map() { this.sections = []; maps.push(this); }
	Map.prototype.section = function(_kind, id, type, title) {
		const section = {
			id, type, title, options: [], addremove: true,
			option: function(_optionKind, name, label) {
				const option = {
					name, label, values: [], depends: function() {}, value: function(value) { this.values.push(value); }
				};
				this.options.push(option);
				return option;
			}
		};
		this.sections.push(section);
		return section;
	};
	Map.prototype.render = function() { return this; };
	const form = { Map, NamedSection: {}, Flag: {}, Value: {}, ListValue: {}, DynamicList: {}, Button: {} };
	const context = {
		_: translate, form, uci: { load: function() { return Promise.resolve(); } },
		rpc: { declare: function() { return function() { return Promise.resolve({ ok: true, phase: 'ready' }); }; } },
		ui: { addNotification: function() {} }, E: function() {}, window: { setTimeout: function() {} },
		view: { extend: function(value) { return value; } }, Promise
	};
	return { view: evaluate('files/www/luci-static/resources/view/autovpn/settings.js', context), maps };
}

function overviewFixture(ping) {
	function E(tag, attrs, children) {
		return {
			tag, attrs: attrs || {}, children: Array.isArray(children) ? children : [children],
			replaceChildren: function() { this.children = Array.from(arguments); }
		};
	}
	const context = {
		_: translate, E, Promise, isFinite,
		window: { setTimeout: function() {}, location: { reload: function() {} } },
		ui: { createHandlerFn: function(_context, handler) { return handler; }, addNotification: function() {} },
		view: { extend: function(value) { return value; } },
		rpc: { declare: function(spec) {
			if (spec.method === 'ping_all') return ping;
			return function() { return Promise.resolve({ ok: true }); };
		} }
	};
	return evaluate('files/www/luci-static/resources/view/autovpn/overview.js', context);
}

function findButtons(node, found = []) {
	if (node == null || typeof node !== 'object') return found;
	if (node.tag === 'button') found.push(node);
	for (const child of node.children || []) findButtons(child, found);
	return found;
}

function button(buttons, label) {
	const found = buttons.find(item => String(item.children[0]) === label);
	assert.ok(found, 'button ' + label + ' must exist');
	return found;
}

function nodeById(node, id) {
	if (node == null || typeof node !== 'object') return null;
	if (node.attrs && node.attrs.id === id) return node;
	for (const child of node.children || []) {
		const found = nodeById(child, id);
		if (found) return found;
	}
	return null;
}

function nodeText(node) {
	if (node == null) return '';
	if (typeof node !== 'object') return String(node);
	if (typeof node.text === 'string') return String(node);
	return (node.children || []).map(nodeText).join(' ');
}

test('settings assigns common options to primary runtime and only selection to VPN zapret', () => {
	const fixture = settingsFixture();
	fixture.view.render();
	const sections = Object.fromEntries(fixture.maps[0].sections.map(section => [section.id, section]));
	assert.deepEqual(sections.runtime.options.map(option => option.name), [
		'selection', 'hysteria_tls_mode', 'wan_device', 'dns_server', 'direct_domains', 'direct_cidrs', 'ru_bypass',
		'_install_zapret', 'zapret_enabled', 'zapret_vless', 'zapret_hysteria2', 'zapret_amneziawg', 'zapret_repeats'
	]);
	assert.deepEqual(sections.runtime_zapret.options.map(option => option.name), ['selection']);
	assert.deepEqual(sections.direct.options.map(option => option.name), ['zapret_enabled', 'discord_media', 'stun', 'media_strategy', 'media_repeats']);
});

test('Discord direct-zapret options are restricted to Wi-Fi -з and preserve legacy defaults', () => {
	const fixture = settingsFixture();
	fixture.view.render();
	const section = fixture.maps[0].sections.find(item => item.id === 'direct');
	const options = Object.fromEntries(section.options.map(option => [option.name, option]));
	const fresh = fs.readFileSync(path.join(root, 'files/etc/config/autovpn'), 'utf8')
		.split("config policy 'direct'\n")[1].split('\nconfig ')[0];
	for (const name of ['discord_media', 'stun']) {
		assert.ok(fresh.includes("option " + name + " '1'"), name + ' fresh installation default');
		assert.equal(options[name].default, '0', name + ' legacy UI default');
		assert.equal(options[name].rmempty, false, name + ' must persist explicit changes');
		assert.match(String(options[name].description), /only to Wi-Fi -з/);
	}
	assert.deepEqual(options.media_strategy.values, ['fake', 'fake_badsum']);
	assert.equal(options.media_strategy.default, 'fake');
	assert.equal(options.media_repeats.default, '2');
	assert.equal(options.media_repeats.datatype, 'and(uinteger,range(1,6))');
	assert.match(String(options.media_strategy.description), /No custom command or arbitrary zapret profile/);
	assert.match(String(options.media_repeats.description), /does not change existing VPN zapret settings/);
});

test('RU bypass is shared, persisted by default, and does not replace manual rules', () => {
	const fixture = settingsFixture();
	fixture.view.render();
	const section = fixture.maps[0].sections.find(item => item.id === 'runtime');
	const bypass = section.options.find(item => item.name === 'ru_bypass');
	assert.ok(bypass);
	assert.equal(bypass.default, '1');
	assert.equal(bypass.rmempty, false);
	assert.match(String(bypass.description), /both VPN networks/);
	assert.match(String(bypass.description), /manual domain and IPv4 rules are kept/);
});

test('each Ping all button disables only itself until its pending request settles', async () => {
	const pending = [];
	const view = overviewFixture(function(target, lane) {
		let resolve;
		let reject;
		const promise = new Promise(function(done, fail) { resolve = done; reject = fail; });
		pending.push({ target, lane, resolve, reject });
		return promise;
	});
	const page = view.render({ runtime_lanes: { vpn: { ok: true, active_profile: 'vless-reality' }, vpn_zapret: { ok: true, active_profile: 'hysteria2' } } });
	const buttons = findButtons(page);
	const vpn = button(buttons, 'Ping all: VPN');
	const vpnZapret = button(buttons, 'Ping all: VPN + zapret');

	const first = vpn.attrs.click();
	assert.equal(pending[0].lane, 'vpn');
	assert.equal(vpn.disabled, true);
	assert.notEqual(vpnZapret.disabled, true);
	pending[0].resolve({ ok: true, checked_at: 1, active_profile: 'vless-reality', results: [] });
	await first;
	assert.notEqual(vpn.disabled, true);

	const second = vpnZapret.attrs.click();
	assert.equal(pending[1].lane, 'vpn_zapret');
	assert.equal(vpnZapret.disabled, true);
	assert.notEqual(vpn.disabled, true);
	pending[1].reject(new Error('timeout'));
	await second;
	assert.notEqual(vpnZapret.disabled, true);
});

test('Ping all lanes retain distinct successful results', async () => {
	const pending = [];
	const view = overviewFixture(function(target, lane) {
		let resolve;
		const promise = new Promise(function(done) { resolve = done; });
		pending.push({ target, lane, resolve });
		return promise;
	});
	const page = view.render({ runtime_lanes: { vpn: { ok: true }, vpn_zapret: { ok: true } } });
	const buttons = findButtons(page);
	const vpn = button(buttons, 'Ping all: VPN');
	const vpnZapret = button(buttons, 'Ping all: VPN + zapret');
	const vpnOutput = nodeById(page, 'autovpn-ping-results-vpn');
	const zapretOutput = nodeById(page, 'autovpn-ping-results-vpn_zapret');
	assert.ok(vpnOutput);
	assert.ok(zapretOutput);

	const first = vpn.attrs.click();
	const second = vpnZapret.attrs.click();
	pending[0].resolve({ ok: true, checked_at: 1, active_profile: 'vless-primary', results: [{ profile: 'vless-primary', status: 'ok', latency_ms: 10, http_status: 200 }] });
	pending[1].resolve({ ok: true, checked_at: 2, active_profile: 'hy2-zapret', results: [{ profile: 'hy2-zapret', status: 'ok', latency_ms: 20, http_status: 204 }] });
	await Promise.all([first, second]);

	assert.match(nodeText(vpnOutput), /vless-primary/);
	assert.doesNotMatch(nodeText(vpnOutput), /hy2-zapret/);
	assert.match(nodeText(zapretOutput), /hy2-zapret/);
	assert.doesNotMatch(nodeText(zapretOutput), /vless-primary/);
});

test('optional RU database status failure does not prevent overview rendering', async () => {
	function E(tag, attrs, children) {
		return { tag, attrs: attrs || {}, children: Array.isArray(children) ? children : [children], replaceChildren: function() {} };
	}
	const context = {
		_: translate, E, Promise, isFinite,
		window: { setTimeout: function() {}, location: { reload: function() {} } },
		ui: { createHandlerFn: function(_context, handler) { return handler; }, addNotification: function() {} },
		view: { extend: function(value) { return value; } },
		rpc: { declare: function(spec) {
			if (spec.method === 'ru_db_status') return function() { return Promise.reject(new Error('offline')); };
			return function() { return Promise.resolve({ ok: true, runtime_lanes: {} }); };
		} }
	};
	const view = evaluate('files/www/luci-static/resources/view/autovpn/overview.js', context);
	const page = view.render(await view.load());
	assert.match(nodeText(page), /Russian bypass database/);
	assert.match(nodeText(page), /Status unavailable/);
});

test('RU database error text stays DOM text rather than HTML', () => {
	const view = overviewFixture(function() { return Promise.resolve({ ok: true }); });
	const injection = '<img src=x onerror=alert(1)>';
	const page = view.render([{ runtime_lanes: {} }, { ok: false, code: injection, last_attempt: 2, last_success: 1, domains: 1, ipv4_cidrs: 2 }]);
	assert.match(nodeText(page), /<img src=x onerror=alert\(1\)>/);
	assert.equal(JSON.stringify(page).includes('innerHTML'), false);
	assert.equal(JSON.stringify(page.attrs || {}).includes(injection), false);
});

test('unattempted RU database status is not presented as up to date', () => {
	const view = overviewFixture(function() { return Promise.resolve({ ok: true }); });
	const page = view.render([{ runtime_lanes: {} }, { ok: true, code: null, last_attempt: 0, last_success: 0, domains: 0, ipv4_cidrs: 0 }]);
	assert.match(nodeText(page), /Database has not been checked yet/);
	assert.doesNotMatch(nodeText(page), /Last database update succeeded/);
});

test('RU database status error is not hidden by an unattempted timestamp', () => {
	const view = overviewFixture(function() { return Promise.resolve({ ok: true }); });
	const page = view.render([{ runtime_lanes: {} }, { ok: false, code: 'invalid_ru_db_status', last_attempt: 0, last_success: 0, domains: 0, ipv4_cidrs: 0 }]);
	assert.match(nodeText(page), /Database status unavailable: invalid_ru_db_status/);
	assert.doesNotMatch(nodeText(page), /Database has not been checked yet/);
});

test('RU database update in progress is distinct from a failed update', () => {
	const view = overviewFixture(function() { return Promise.resolve({ ok: true }); });
	const page = view.render([{ runtime_lanes: {} }, { ok: false, code: 'update_in_progress', last_attempt: 2, last_success: 1, domains: 1, ipv4_cidrs: 2 }]);
	assert.match(nodeText(page), /Database update is in progress/);
	assert.doesNotMatch(nodeText(page), /Last database update failed/);
});
