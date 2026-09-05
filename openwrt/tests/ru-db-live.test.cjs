'use strict';

const assert = require('node:assert/strict');
const { once } = require('node:events');
const fs = require('node:fs');
const net = require('node:net');
const os = require('node:os');
const path = require('node:path');
const { spawn, spawnSync } = require('node:child_process');
const test = require('node:test');

const singBox = process.env.AUTOVPN_SING_BOX;

function delay(ms) { return new Promise(resolve => setTimeout(resolve, ms)); }

async function eventually(predicate, timeout = 5000) {
	const deadline = Date.now() + timeout;
	let last;
	while (Date.now() < deadline) {
		try {
			last = await predicate();
			if (last) return last;
		} catch (error) { last = error; }
		await delay(25);
	}
	throw last instanceof Error ? last : new Error('condition did not become true');
}

function localPort() {
	return new Promise((resolve, reject) => {
		const server = net.createServer();
		server.once('error', reject);
		server.listen(0, '127.0.0.1', () => {
			const port = server.address().port;
			server.close(error => error ? reject(error) : resolve(port));
		});
	});
}

function atomicReplace(target, value) {
	const temporary = path.join(path.dirname(target), '.' + path.basename(target) + '.' + process.pid + '.' + Date.now());
	fs.writeFileSync(temporary, value, { mode: 0o600 });
	fs.renameSync(temporary, target);
}

function ruleSet(cidr) {
	return JSON.stringify({ version: 1, rules: [{ ip_cidr: [cidr] }] });
}

function config(socksPort, httpPort, rules) {
	return {
		log: { level: 'error', timestamp: false },
		inbounds: [{ type: 'socks', tag: 'socks', listen: '127.0.0.1', listen_port: socksPort }],
		outbounds: [
			{ type: 'direct', tag: 'direct' },
			{ type: 'block', tag: 'reject' },
		],
		route: {
			rule_set: [{ type: 'local', tag: 'autovpn-ru', format: 'source', path: rules }],
			rules: [
				{ rule_set: ['autovpn-ru'], action: 'route', outbound: 'reject' },
				{ action: 'route-options', override_address: '127.0.0.1', override_port: httpPort },
			],
			final: 'direct',
		},
	};
}

async function socksHttp(port) {
	const socket = net.createConnection({ host: '127.0.0.1', port });
	const state = { data: Buffer.alloc(0), error: null, ended: false };
	socket.setTimeout(750, () => socket.destroy(new Error('SOCKS request timed out')));
	socket.on('data', chunk => { state.data = Buffer.concat([state.data, chunk]); });
	socket.on('error', error => { state.error = error; });
	socket.on('end', () => { state.ended = true; });
	try {
		await Promise.race([once(socket, 'connect'), delay(800).then(() => { throw new Error('SOCKS connect timed out'); })]);
		async function bytes(count) {
			await eventually(() => state.data.length >= count || state.error || state.ended, 800);
			if (state.error) throw state.error;
			if (state.data.length < count) throw new Error('SOCKS connection closed early');
			const value = state.data.subarray(0, count);
			state.data = state.data.subarray(count);
			return value;
		}
		socket.write(Buffer.from([0x05, 0x01, 0x00]));
		assert.deepEqual([...await bytes(2)], [0x05, 0x00]);
		socket.write(Buffer.from([0x05, 0x01, 0x00, 0x01, 198, 51, 100, 9, 0x00, 0x50]));
		const reply = await bytes(10);
		if (reply[0] !== 0x05) throw new Error('invalid SOCKS version');
		if (reply[1] !== 0x00) return { ok: false, reply: reply[1] };
		socket.write('GET / HTTP/1.1\r\nHost: local.test\r\nConnection: close\r\n\r\n');
		await eventually(() => state.data.includes(Buffer.from('\r\n\r\n')) || state.error || state.ended, 800);
		if (state.error) throw state.error;
		return { ok: state.data.includes(Buffer.from('200 OK')), reply: 0 };
	} finally {
		socket.destroy();
	}
}

async function waitForSocks(port) {
	await eventually(async () => {
		const socket = net.createConnection({ host: '127.0.0.1', port });
		try {
			await Promise.race([
				once(socket, 'connect'),
				once(socket, 'error').then(([error]) => { throw error; }),
				delay(150).then(() => { throw new Error('not ready'); })
			]);
			return true;
		} finally { socket.destroy(); }
	}, 5000);
}

test('native sing-box atomically hot-reloads one local RU rule-set in two SOCKS runtimes', { skip: !singBox }, async t => {
	const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autovpn-ru-live-'));
	const rules = path.join(directory, 'ru.json');
	const http = net.createServer(socket => {
		socket.end('HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK');
	});
	const children = [];
	t.after(async () => {
		for (const child of children) if (child.exitCode == null) child.kill('SIGTERM');
		await Promise.all(children.map(child => child.exitCode == null ? once(child, 'exit').catch(() => {}) : undefined));
		await new Promise(resolve => http.close(() => resolve()));
		fs.rmSync(directory, { recursive: true, force: true });
	});

	fs.writeFileSync(rules, ruleSet('127.0.0.1/32'), { mode: 0o600 });
	await new Promise((resolve, reject) => http.listen(0, '127.0.0.1', error => error ? reject(error) : resolve()));
	const httpPort = http.address().port;
	const ports = [await localPort(), await localPort()];
	for (const port of ports) {
		const file = path.join(directory, 'sing-box-' + port + '.json');
		fs.writeFileSync(file, JSON.stringify(config(port, httpPort, rules)), { mode: 0o600 });
		const checked = spawnSync(singBox, ['check', '-c', file], { encoding: 'utf8', timeout: 5000 });
		assert.equal(checked.status, 0, checked.stderr || String(checked.error));
		const child = spawn(singBox, ['run', '-c', file], { stdio: ['ignore', 'ignore', 'pipe'] });
		children.push(child);
	}
	for (const port of ports) await waitForSocks(port);
	const pids = children.map(child => child.pid);

	for (const port of ports) assert.deepEqual(await socksHttp(port), { ok: true, reply: 0 });

	atomicReplace(rules, ruleSet('198.51.100.0/24'));
	await eventually(async () => (await Promise.all(ports.map(socksHttp))).every(result => !result.ok), 5000);

	atomicReplace(rules, '{invalid-json');
	await delay(200);
	for (const port of ports) assert.equal((await socksHttp(port)).ok, false, 'malformed replacement must retain old loaded rules');

	atomicReplace(rules, ruleSet('127.0.0.1/32'));
	await eventually(async () => (await Promise.all(ports.map(socksHttp))).every(result => result.ok), 5000);
	assert.deepEqual(children.map(child => child.pid), pids);
	assert.ok(children.every(child => child.exitCode == null), 'hot reload must not restart a runtime');
});
