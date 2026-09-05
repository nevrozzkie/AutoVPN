'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'files/usr/share/ucode/autovpn/probes.uc'), 'utf8');

const descriptors = {
	vpn: {
		id: 'vpn',
		probe_ports: { 'vless-reality': 1089, hysteria2: 1090, amneziawg: 1091 },
		secret: 'must-not-leak'
	},
	vpn_zapret: {
		id: 'vpn_zapret',
		probe_ports: { 'vless-reality': 1109, hysteria2: 1110, amneziawg: 1111 },
		secret: 'must-not-leak'
	}
};

function loadWithLanes(requested = []) {
	const factory = new Function('type', 'match', 'split', 'int', 'json', 'index', 'require', source);
	return factory(
		value => value == null ? null : Array.isArray(value) ? 'array' :
			Number.isInteger(value) ? 'int' : typeof value === 'boolean' ? 'bool' : typeof value,
		(value, expression) => value.match(expression),
		(value, separator) => value.split(separator),
		value => Number.parseInt(value, 10),
		value => JSON.parse(value),
		(value, needle) => value.indexOf(needle),
		name => {
			assert.equal(name, 'autovpn.lanes');
			return { get(id) {
				requested.push(id);
				return descriptors[id] == null ? null : structuredClone(descriptors[id]);
			} };
		}
	);
}

test('omitting lane preserves the exact legacy probe arguments', () => {
	const probes = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/probes.uc'));
	assert.deepEqual(probes.argumentsFor('vless-reality', 'youtube'), [
		'/usr/bin/curl', '--disable', '--silent', '--head', '--output', '/dev/null',
		'--write-out', '%{http_code} %{time_starttransfer}', '--proxy',
		'socks5h://127.0.0.1:1089', '--noproxy', '', '--proto', '=https',
		'--connect-timeout', '3', '--max-time', '5', '--max-redirs', '0',
		'https://www.youtube.com/generate_204'
	]);
});

test('lane probes use only fixed descriptor ports and fixed HTTPS targets', () => {
	const requested = [];
	const probes = loadWithLanes(requested);
	for (const [lane, base] of [['vpn', 1089], ['vpn_zapret', 1109]]) {
		for (const [offset, profile] of probes.profiles.entries()) {
			const args = probes.argumentsFor(profile, 'instagram', lane);
			assert.equal(args[args.indexOf('--proxy') + 1], `socks5h://127.0.0.1:${base + offset}`);
			assert.equal(args.at(-1), 'https://www.instagram.com/');
			assert.equal(JSON.stringify(args).includes('must-not-leak'), false);
		}
	}
	assert.deepEqual(requested, ['vpn', 'vpn', 'vpn', 'vpn_zapret', 'vpn_zapret', 'vpn_zapret']);
});

test('invalid lanes, profiles, targets and malformed ports fail closed', () => {
	const probes = loadWithLanes();
	assert.equal(probes.argumentsFor('vless-reality', 'youtube', '../vpn'), null);
	assert.equal(probes.argumentsFor('direct', 'youtube', 'vpn'), null);
	assert.equal(probes.argumentsFor('vless-reality', 'https://private/', 'vpn'), null);
	descriptors.bad = { probe_ports: { 'vless-reality': '1089' } };
	assert.equal(probes.argumentsFor('vless-reality', 'youtube', 'bad'), null);
	delete descriptors.bad;
});

test('lane-aware arguments do not alter probe parsing or health observations', () => {
	const legacy = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/probes.uc'));
	const lanes = loadWithLanes();
	assert.deepEqual(lanes.parseResult('hysteria2', 'youtube', '204 0.123456', 0),
		legacy.parseResult('hysteria2', 'youtube', '204 0.123456', 0));
	for (const success of [true, false]) {
		assert.deepEqual(lanes.observe({ identity: 'same', failures: 2 }, 'same', success),
			legacy.observe({ identity: 'same', failures: 2 }, 'same', success));
	}
});
