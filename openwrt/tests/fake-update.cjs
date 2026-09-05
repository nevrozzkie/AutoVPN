#!/usr/bin/env node
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
const name = path.basename(process.argv[1]);
const env = process.env;
const write = value => process.stdout.write((typeof value === 'string' ? value : JSON.stringify(value)) + '\n');
const current = () => fs.existsSync(env.FAKE_COMMIT) ? fs.readFileSync(env.FAKE_COMMIT, 'utf8') : (env.FAKE_CURRENT || '0.7.0-r1');
if (name !== 'jsonfilter') fs.appendFileSync(env.FAKE_LOG, JSON.stringify({name, args}) + '\n');
if (name === 'ubus') write({release: {version: '25.12.5', target: 'mediatek/filogic'}});
else if (name === 'uname') write('6.12.85');
else if (name === 'df') write('Filesystem 1024-blocks Used Available Capacity Mounted on\nfixture 100000 1 99999 1% /');
else if (name === 'curl') {
	if (env.FAKE_DOWNLOAD_FAIL === '1') process.exit(22);
	fs.copyFileSync(path.join(env.FAKE_ASSETS, path.basename(args.at(-1))), args[args.indexOf('--output') + 1]);
} else if (name === 'jsonfilter') {
	try {
		const file = args.includes('-i') ? args[args.indexOf('-i') + 1] : 0;
		let value = JSON.parse(fs.readFileSync(file, 'utf8'));
		for (const part of args[args.indexOf('-e') + 1].slice(1).match(/[a-z0-9_]+/g) || []) value = value[part];
		if (value === undefined || value === null) process.exit(1);
		write(value);
	} catch (_) { process.exit(1); }
} else if (name === 'apk') {
	if (args.includes('--print-arch')) write('aarch64_cortex-a53');
	else if (args.includes('query')) write([{name: args.includes('kernel') ? 'kernel' : 'autovpn-controller', version: args.includes('kernel') ? '6.12.85~fixture' : current()}]);
	else if (args.includes('version')) write(args.at(-2) === args.at(-1) ? '=' : args.at(-2) < args.at(-1) ? '<' : '>');
	else if (args.includes('adbdump')) write({info: {name: 'autovpn-controller', version: '0.8.0-r1', arch: 'all'}});
	else if (args.includes('verify')) { if (env.FAKE_BAD_SIGNATURE === '1') process.exit(1); }
	else if (args.includes('--simulate')) {
		write(env.FAKE_BAD_PLAN === '1' ? '(1/2) Upgrading autovpn-controller (0.7 -> 0.8)\n(2/2) Purging other (1)' :
			current() === '0.8.0-r1' ? 'OK: 0 changes' : '(1/1) Upgrading autovpn-controller (0.7.0-r1 -> 0.8.0-r1)');
	} else if (args.includes('add')) {
		if (env.FAKE_ADD_FAIL === '1') process.exit(1);
		fs.writeFileSync(env.FAKE_COMMIT, '0.8.0-r1');
	} else throw new Error('Unexpected apk command: ' + args.join(' '));
} else if (name === 'lock') {
	const file = args.at(-1);
	if (args.includes('-u')) { fs.rmSync(file, {force: true}); }
	else {
		if (env.FAKE_LOCK_BUSY === '1') process.exit(1);
		fs.mkdirSync(path.dirname(file), {recursive: true});
		try { fs.closeSync(fs.openSync(file, 'wx')); } catch (_) { process.exit(1); }
	}
} else if (name === 'uci') {
	if (args.includes('changes') && env.FAKE_DIRTY_UCI === '1') write('autovpn.main.enabled=1');
} else if (name === 'ucode') { if (env.FAKE_NETWORK_UNSAFE === '1') process.exit(1); }
else if (name === 'runtime' || name === 'sync') { /* Logged successful device operation. */ }
else throw new Error('Unexpected mock command: ' + name);
