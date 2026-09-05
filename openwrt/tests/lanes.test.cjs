'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const root = path.join(__dirname, '..');
const lanes = loadUcodeModule(path.join(root, 'files/usr/share/ucode/autovpn/lanes.uc'));
const profiles = ['vless-reality', 'hysteria2', 'amneziawg'];
const expected = {
	vpn: {
		id: 'vpn', root: '/etc/autovpn/runtime', health_dir: '/var/run/autovpn-health', tun: 'avpn0', bridge: 'br-avpn',
		address: '172.30.255.1/30', dns: '172.30.255.2', health_port: 1088,
		probe_ports: { 'vless-reality': 1089, hysteria2: 1090, amneziawg: 1091 },
		route_table: 20191, priorities: { route: 20191, unreachable: 20192 },
		awg: { device: 'avpnwg0', inner_mark: 20193, outer_mark: 20194 }, profiles,
	},
	vpn_zapret: {
		id: 'vpn_zapret', root: '/etc/autovpn/runtime-zapret', health_dir: '/var/run/autovpn-health-zapret', tun: 'avpn1', bridge: 'br-avpnz',
		address: '172.30.255.5/30', dns: '172.30.255.6', health_port: 1108,
		probe_ports: { 'vless-reality': 1109, hysteria2: 1110, amneziawg: 1111 },
		route_table: 20211, priorities: { route: 20211, unreachable: 20212 },
		awg: { device: 'avpnwg1', inner_mark: 20213, outer_mark: 20214 }, profiles,
	},
};

function awg(privateKey, address) {
	return { interface: { private_key: privateKey, address } };
}

test('fixed lane descriptors have exact distinct resources and the same bounded profiles', () => {
	assert.deepEqual(lanes.ids, ['vpn', 'vpn_zapret']);
	assert.deepEqual(lanes.get('vpn'), expected.vpn);
	assert.deepEqual(lanes.get('vpn_zapret'), expected.vpn_zapret);
	const primary = lanes.get('vpn');
	const secondary = lanes.get('vpn_zapret');
	for (const select of [
		lane => lane.root, lane => lane.health_dir, lane => lane.tun, lane => lane.bridge, lane => lane.address, lane => lane.dns,
		lane => lane.health_port, lane => lane.route_table, lane => lane.priorities.route,
		lane => lane.priorities.unreachable, lane => lane.awg.device, lane => lane.awg.inner_mark,
		lane => lane.awg.outer_mark,
	]) assert.notEqual(select(primary), select(secondary));
	assert.equal(new Set(Object.values(primary.probe_ports).concat(Object.values(secondary.probe_ports))).size, 6);
	assert.deepEqual(primary.profiles, profiles);
	assert.deepEqual(secondary.profiles, profiles);
});

test('unknown and path-like lane identifiers are denied', () => {
	for (const id of [null, '', 'VPN', 'vpn-zapret', '../vpn', 'vpn/..', '/etc/autovpn/runtime', 'constructor'])
		assert.equal(lanes.get(id), null);
});

test('mutating a returned descriptor cannot poison the fixed registry', () => {
	const descriptor = lanes.get('vpn');
	descriptor.root = '/tmp/poison';
	descriptor.probe_ports.hysteria2 = 1;
	descriptor.priorities.route = 1;
	descriptor.awg.outer_mark = 1;
	descriptor.profiles.push('direct');
	assert.deepEqual(lanes.get('vpn'), expected.vpn);
});

test('AWG lane identity must be minimally valid and distinct without echoing secrets', () => {
	const firstKey = 'A'.repeat(43) + '=';
	const secondKey = 'B'.repeat(43) + '=';
	assert.deepEqual(lanes.validatePair(null, null), { ok: true });
	assert.deepEqual(lanes.validatePair(awg(firstKey, '10.66.66.2/32'), null), { ok: true });
	assert.deepEqual(lanes.validatePair(null, awg(secondKey, '10.66.66.3/32')), { ok: true });
	assert.deepEqual(lanes.validatePair(awg(firstKey, '10.66.66.2/32'), awg(secondKey, '10.66.66.3/32')), { ok: true });
	for (const invalid of [{}, { interface: {} }, awg('secret', '10.66.66.2/32'), awg(firstKey, '10.066.66.2/32')]) {
		const result = lanes.validatePair(invalid, null);
		assert.deepEqual(result, { ok: false, code: 'invalid_awg_lane_profile' });
		assert.equal(JSON.stringify(result).includes(firstKey), false);
	}
	const duplicateKey = lanes.validatePair(awg(firstKey, '10.66.66.2/32'), awg(firstKey, '10.66.66.3/32'));
	assert.deepEqual(duplicateKey, { ok: false, code: 'shared_awg_private_key' });
	assert.equal(JSON.stringify(duplicateKey).includes(firstKey), false);
	const duplicateAddress = lanes.validatePair(awg(firstKey, '10.66.66.2/32'), awg(secondKey, '10.66.66.2/32'));
	assert.deepEqual(duplicateAddress, { ok: false, code: 'shared_awg_address' });
	assert.equal(JSON.stringify(duplicateAddress).includes('10.66.66.2'), false);
});
