'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const presets = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/negative_presets.uc'));

test('negative VPN preset catalog is fixed, typed, and does not contain duplicate suffixes', () => {
	assert.deepEqual(presets.names(), [
		'negative_chatgpt', 'negative_claude', 'negative_extended_blocked_services',
		'negative_instagram', 'negative_telegram', 'negative_x', 'negative_youtube'
	]);
	const catalog = presets.catalog;
	assert.deepEqual(catalog.negative_telegram.slice(-3), [
		'web.telegram.org', 'desktop.telegram.org', 'updates.tdesktop.com'
	]);
	assert.ok(catalog.negative_youtube.includes('googlevideo.com'));
	assert.ok(catalog.negative_chatgpt.includes('oaiusercontent.com'));
	assert.ok(catalog.negative_extended_blocked_services.includes('discord.com'));
	assert.ok(catalog.negative_extended_blocked_services.includes('signal.org'));
	assert.deepEqual(presets.cidrs.negative_telegram, ['149.154.160.0/20', '91.108.4.0/22']);
	for (const domains of Object.values(catalog)) {
		assert.ok(domains.length > 0);
		assert.equal(new Set(domains).size, domains.length);
		for (const domain of domains)
			assert.match(domain, /^[a-z0-9][a-z0-9.-]*[a-z0-9]$/);
	}
});

test('negative VPN preset selection is locally bounded and de-duplicated', () => {
	assert.deepEqual(presets.enabled(null), []);
	assert.deepEqual(presets.enabled({ ignored: true }), []);
	const selected = presets.enabled({ negative_telegram: true, negative_extended_blocked_services: true });
	assert.ok(selected.includes('api.telegram.org'));
	assert.ok(selected.includes('discord.com'));
	assert.equal(new Set(selected).size, selected.length);
	assert.equal(selected.includes('youtube.com'), false);
	assert.deepEqual(presets.enabledCidrs({ negative_telegram: true }),
		['149.154.160.0/20', '91.108.4.0/22']);
});

test('LuCI displays every packaged preset domain and supplemental CIDR', () => {
	const source = fs.readFileSync(path.join(root, 'files/www/luci-static/resources/view/autovpn/settings.js'), 'utf8');
	for (const domains of Object.values(presets.catalog))
		for (const domain of domains) assert.ok(source.includes("'" + domain + "'"), domain);
	for (const cidrs of Object.values(presets.cidrs))
		for (const cidr of cidrs) assert.ok(source.includes("'" + cidr + "'"), cidr);
	assert.match(source, /not a complete or current register of all websites blocked in Russia/);
});
