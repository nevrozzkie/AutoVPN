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
