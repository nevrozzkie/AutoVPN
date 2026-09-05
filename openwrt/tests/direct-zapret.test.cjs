'use strict';

const assert = require('node:assert/strict');
const path = require('node:path');
const test = require('node:test');
const { loadUcodeModule } = require('./ucode-loader.cjs');

const direct = loadUcodeModule(path.join(__dirname,
	'../files/usr/share/ucode/autovpn/direct-zapret.uc'));

test('direct zapret plan has one fixed typed WAN input and safe defaults', () => {
	assert.deepEqual(direct.plan('pppoe-wan', true), { version: 1, wan_device: 'pppoe-wan' });
	assert.deepEqual(direct.plan('eth1', null), { version: 1, wan_device: 'eth1' });
	assert.equal(direct.plan('eth1', false), null);
	for (const wan of ['', 'lo', 'br-lan', 'br-avpndz', 'avpn0', 'eth1";drop', '../wan', 'a'.repeat(16)])
		assert.equal(direct.plan(wan, true), false);
	for (const value of [null, {}, { version: 1, wan_device: 'eth1', ports: [22] },
		{ version: 2, wan_device: 'eth1' }]) {
		assert.equal(direct.validPlan(value), false);
		assert.equal(direct.config(value), null);
		assert.equal(direct.nft(value), null);
	}
});

test('nfqws2 configuration is bounded to outbound IPv4 HTTP TLS and QUIC', () => {
	const value = direct.plan('pppoe-wan', true);
	const config = direct.config(value);
	assert.match(config, /^--qnum=20196$/m);
	assert.match(config, /^--fwmark=0x40000000$/m);
	assert.match(config, /^--bind-fix4$/m);
	assert.match(config, /^--filter-tcp=80,443$/m);
	assert.match(config, /^--filter-l7=http,tls$/m);
	assert.match(config, /^--payload=tls_client_hello,http_req$/m);
	assert.match(config, /^--lua-desync=multisplit:pos=1,midsld$/m);
	assert.match(config, /^--filter-udp=443$/m);
	assert.match(config, /^--filter-l7=quic$/m);
	assert.match(config, /^--payload=quic_initial$/m);
	assert.match(config, /^--lua-desync=fake:blob=fake_default_quic:badsum:repeats=2$/m);
	assert.equal(config.match(/^--new$/gm).length, 1);
	assert.equal(config.match(/^--in-range=x$/gm).length, 2);
	assert.equal(config.match(/^--out-range=-n12$/gm).length, 2);
	assert.doesNotMatch(config, /--filter-ip|--hostlist|--daemon|--writable|payload=all|bypass/);
});

test('open nft guard permits only exact-subnet IPv4 to exact WAN and queues only web ports', () => {
	const rules = direct.nft(direct.plan('pppoe-wan', true));
	assert.match(rules, /chain ownership_autovpn_direct_zapret_v1/);
	assert.match(rules, /hook forward priority -10/);
	assert.match(rules, /iifname "br-avpndz" ip saddr 192\.168\.31\.0\/24 oifname "pppoe-wan" accept/);
	assert.match(rules, /iifname "pppoe-wan" oifname "br-avpndz" ip daddr 192\.168\.31\.0\/24 ct state established,related accept/);
	assert.match(rules, /iifname "br-avpndz" drop/);
	assert.match(rules, /oifname "br-avpndz" drop/);
	assert.match(rules, /hook postrouting priority 101/);
	assert.match(rules, /ct original ip saddr 192\.168\.31\.0\/24 meta mark & 1073741824 == 0 udp dport 443 ct original packets 1-12 meta mark set 20221 queue num 20196/);
	assert.match(rules, /tcp dport \{ 80, 443 \} ct original packets 1-12 meta mark set 20221 queue num 20196/);
	assert.match(rules, /tcp dport \{ 80, 443 \} tcp flags & \(fin \| rst\) != 0 meta mark set 20221 queue num 20196/);
	assert.match(rules, /hook output priority -401/);
	assert.match(rules, /oifname "pppoe-wan" meta mark 1073762045 udp dport 443 notrack/);
	assert.match(rules, /tcp dport \{ 80, 443 \} notrack/);
	assert.doesNotMatch(rules, /\bbypass\b|flush ruleset|br-lan|ip6|udp dport \{/);
});

test('closed nft state is an owned standalone drop guard without queue rules', () => {
	const rules = direct.closedNft();
	assert.match(rules, /chain ownership_autovpn_direct_zapret_v1/);
	assert.match(rules, /hook forward priority -10/);
	assert.match(rules, /iifname "br-avpndz" drop/);
	assert.doesNotMatch(rules, /queue num|hook postrouting|hook output|\bbypass\b|pppoe-wan/);
});

const media = (overrides = {}) => ({ discord_media: true, stun: true,
	media_strategy: 'fake', media_repeats: 2, ...overrides });

test('media plans strictly validate the local bounded schema', () => {
	assert.deepEqual(direct.plan('eth1', true, media()), { version: 2, wan_device: 'eth1', ...media() });
	for (const bad of [media({ discord_media: 1 }), media({ stun: '1' }),
		media({ media_strategy: 'fake;sh' }), media({ media_strategy: null }),
		...[0, 7, 1.5, '2', null].map(media_repeats => media({ media_repeats })),
		media({ ports: '22' }), {}, []]) {
		assert.equal(direct.plan('eth1', true, bad), false, JSON.stringify(bad));
		const value = { version: 2, wan_device: 'eth1', ...bad };
		assert.equal(direct.validPlan(value), false);
		assert.equal(direct.config(value), null);
		assert.equal(direct.nft(value), null);
	}
});

test('Discord and STUN toggles add independent outbound payload-only profiles', () => {
	for (const discord_media of [false, true]) for (const stun of [false, true]) {
		const value = direct.plan('eth1', true, media({ discord_media, stun }));
		const cfg = direct.config(value);
		const profiles = cfg.split('--new\n');
		assert.equal(profiles.length, 2 + Number(discord_media) + Number(stun));
		assert.equal(cfg.includes('--payload=discord_ip_discovery'), discord_media);
		assert.equal(cfg.includes('--payload=stun'), stun);
		for (const profile of profiles.slice(2)) {
			assert.match(profile, /--filter-l3=ipv4\n/);
			assert.match(profile, /--in-range=x\n--out-range=a\n/);
			assert.match(profile, /--lua-desync=fake:blob=0x0{32}:repeats=2/);
			assert.doesNotMatch(profile, /badsum|-n12|payload=all/);
		}
		if (discord_media) assert.match(cfg, /--filter-udp=50000-50099,19294-19344\n--filter-l7=discord/);
		if (stun) assert.match(cfg, /--filter-udp=1-65535\n--filter-l7=stun/);
		if (!discord_media && !stun) {
			assert.equal(cfg, direct.config(direct.plan('eth1', true)));
			assert.equal(direct.nft(value), direct.nft(direct.plan('eth1', true)));
		}
	}
	for (const repeats of [1, 6]) assert.match(direct.config(direct.plan('eth1', true,
		media({ media_strategy: 'fake_badsum', media_repeats: repeats }))),
		new RegExp('blob=0x0{32}:badsum:repeats=' + repeats));
});

// Evaluate emitted payload-offset predicates against bytes, independently of renderer constants.
function signatureMatches(rule, payload) {
	const length = rule.match(/udp length (==|>=) (\d+)/);
	if (!length || (length[1] === '==' ? payload.length + 8 !== +length[2] : payload.length + 8 < +length[2])) return false;
	for (const [, offset, width, expected] of rule.matchAll(/@ih,(\d+),(\d+) (0x[0-9A-Fa-f]+|0)\b/g)) {
		if (+offset + +width > payload.length * 8) return false;
		let value = 0n;
		for (let bit = +offset; bit < +offset + +width; bit++)
			value = (value << 1n) | BigInt((payload[bit >> 3] >> (7 - (bit % 8))) & 1);
		if (value !== BigInt(expected)) return false;
	}
	return true;
}

test('emitted upstream discovery and STUN signatures exclude ordinary media and malformed packets', () => {
	const rules = direct.nft(direct.plan('eth1', true, media())).split('\n');
	const discordRule = rules.find(line => line.includes('meta mark set 20222'));
	const stunRule = rules.find(line => line.includes('meta mark set 20223'));
	const discovery = Buffer.alloc(74);
	discovery.writeUInt32BE(0x00010046, 0);
	discovery.writeUInt32BE(0x12345678, 4); // arbitrary SSRC
	assert.equal(signatureMatches(discordRule, discovery), true);
	for (const position of [0, 8, 23, 24, 40, 56, 71]) {
		const invalid = Buffer.from(discovery); invalid[position] ^= 1;
		assert.equal(signatureMatches(discordRule, invalid), false, 'discovery byte ' + position);
	}
	assert.equal(signatureMatches(discordRule, discovery.subarray(0, 73)), false);
	const stun = Buffer.alloc(20);
	stun.writeUInt16BE(1, 0); stun.writeUInt32BE(0x2112a442, 4);
	assert.equal(signatureMatches(stunRule, stun), true);
	for (const [position, mask] of [[0, 0x80], [3, 1], [4, 1]]) {
		const invalid = Buffer.from(stun); invalid[position] ^= mask;
		assert.equal(signatureMatches(stunRule, invalid), false);
	}
	assert.equal(signatureMatches(stunRule, stun.subarray(0, 19)), false);
	assert.equal(signatureMatches(discordRule, stun), false);
	assert.equal(signatureMatches(stunRule, discovery), false);
	assert.equal(signatureMatches(stunRule, Buffer.alloc(1200, 0x80)), false);
});

test('media queues remain direct-only with no late-STUN cutoff or repeated UDP443 queue', () => {
	for (const discord_media of [false, true]) for (const stun of [false, true]) {
		const rules = direct.nft(direct.plan('pppoe-wan', true, media({ discord_media, stun })));
		const lines = rules.split('\n');
		for (const mark of [20222, 20223]) {
			const enabled = mark === 20222 ? discord_media : stun;
			const queue = lines.find(line => line.includes('meta mark set ' + mark));
			assert.equal(Boolean(queue), enabled);
			if (!enabled) continue;
			assert.match(queue, /iifname "br-avpndz" oifname "pppoe-wan" ct original ip saddr 192\.168\.31\.0\/24 meta mark & 1073741824 == 0/);
			assert.match(queue, /queue num 20196$/);
			assert.doesNotMatch(queue, /ct original packets|bypass|return/);
			assert.ok(lines.some(line => line.includes('oifname "pppoe-wan" meta mark ' + (1073741824 + mark)) && line.endsWith('notrack')));
		}
		if (stun) {
			const web = lines.find(line => line.includes('udp dport 443 ct original packets'));
			assert.match(web, /meta mark != \{ 20222, 20223 \}/);
			assert.ok(lines.indexOf(web) > lines.findIndex(line => line.includes('meta mark set 20223')));
		}
		assert.doesNotMatch(rules, /br-avpnz"|br-avpn"|queue num 20195|flush ruleset|\bbypass\b|ip6/);
	}
});
