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
if (name === 'ucode') {
	if (args[1] === 'network-gate') process.exit(scenario === 'networks-pending' ? 1 : 0);
	fs.writeFileSync(path.join(directory, 'runtime/candidate.json'), '{"test":"generated"}');
	process.stdout.write('{"ok":true,"active_profile":"auto","capabilities":{"vless":true,"hysteria2":false,"amneziawg":false,"zapret":false,"policy_routing":true}}\n');
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
