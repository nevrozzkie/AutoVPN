#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const directory = process.env.AUTOVPN_TEST_DIR;
const scenario = process.env.AUTOVPN_TEST_SCENARIO;
const name = path.basename(process.argv[1]);
const args = process.argv.slice(2);
fs.appendFileSync(path.join(directory, 'events'), JSON.stringify([name, ...args]) + '\n');
const command = args.join(' ');
if (name === 'ip' && command === '-4 route replace default dev avpn0 table 20191' && scenario === 'route-failed') process.exit(1);
if (name === 'ucode') {
	if (args[0].endsWith('/zapret-helper.uc')) {
		process.exit(scenario === 'zapret-failed' && args[1] !== 'down' ? 1 : 0);
	}
	if (args[1] === 'network-gate') process.exit(scenario === 'networks-pending' ? 1 : 0);
	if (args[0].endsWith('/awg-helper.uc') && ['up', 'check'].includes(args[1]) && ['optional-awg-failed', 'hard-awg-failed'].includes(scenario)) process.exit(1);
	if (args[1] === 'disable-awg') fs.writeFileSync(path.join(directory, 'runtime/awg.json'), 'null');
	if (args[1] === 'fallback-awg') {
		fs.writeFileSync(path.join(directory, 'runtime/awg.json'), 'null');
		fs.writeFileSync(path.join(directory, 'selected-profile'), args[3] || 'vless-reality');
	}
	if (args[0].endsWith('/probe-helper.uc')) {
		if (args[1] === 'bootstrap') { process.stdout.write('{"ok":true,"next_profile":"hysteria2"}'); process.exit(0); }
		if (args[1] === 'ping-all') {
			process.stdout.write(JSON.stringify({ ok: true, active_profile: 'vless-reality', results: [] }));
			process.exit(0);
		}
		const switched = fs.existsSync(path.join(directory, 'selected-profile'));
		const bad = ['health-dead', 'probe-failed'].includes(scenario) ||
			(scenario === 'health-switch-failed' && switched);
		const pending = ['health-switch', 'health-switch-failed'].includes(scenario) && !switched;
		process.stdout.write(JSON.stringify({ ok: true, healthy: !bad && !pending && scenario !== 'health-transient',
			failed: bad || pending, next_profile: pending ? 'hysteria2' : null }));
		process.exit(0);
	}
	if (args[1] === 'select-profile') fs.writeFileSync(path.join(directory, 'selected-profile'), args[3]);
	if (args[1] === 'commit-profile') fs.writeFileSync(path.join(directory, 'committed-profile'), args[3]);
	fs.writeFileSync(path.join(directory, 'runtime/candidate.json'), '{"test":"generated"}');
	const selected = fs.existsSync(path.join(directory, 'selected-profile')) ? fs.readFileSync(path.join(directory, 'selected-profile'), 'utf8') :
		(scenario === 'hard-awg-failed' ? 'amneziawg' : 'vless-reality');
	process.stdout.write(JSON.stringify({ok:true,active_profile:selected,capabilities:{vless:true,hysteria2:false,amneziawg:false,zapret:false,policy_routing:true}}));
} else if (name === 'jsonfilter') {
	const value = JSON.parse(fs.readFileSync(args[args.indexOf('-i') + 1], 'utf8'));
	process.stdout.write(value[args[args.indexOf('-e') + 1].slice(2)] || '');
} else if (name === 'nft' && command === 'list table inet autovpn') {
	process.stdout.write(scenario === 'foreign-table' ? 'table inet autovpn { chain unrelated {} }' : 'table inet autovpn { chain ownership_autovpn_v1 {} }');
} else if (name === 'nft' && command === 'list chain inet fw4 forward') {
	process.stdout.write('iifname "br-avpn" oifname "avpn0" accept');
} else if (name === 'ip' && command === '-4 rule show') {
	process.stdout.write(scenario === 'foreign-route' ? '20191: from all lookup 500\n' : '0: from all lookup local\n');
} else if (name === 'uci' && scenario === 'offload') {
	process.stdout.write('1');
} else if (name === 'curl') {
	process.stdout.write(scenario === 'probe-failed' ? '503' : '204');
} else if (name === 'service' && command === 'start') {
	fs.writeFileSync(path.join(directory, 'service-running'), '1');
} else if (name === 'service' && command === 'stop') {
	fs.rmSync(path.join(directory, 'service-running'), { force: true });
} else if (name === 'service' && command === 'running') {
	process.exit(fs.existsSync(path.join(directory, 'service-running')) ? 0 : 1);
}
