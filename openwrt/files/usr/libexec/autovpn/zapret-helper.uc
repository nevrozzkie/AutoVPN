#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename, unlink, access, mkdir, lstat } from 'fs';

const zapret = require('autovpn.zapret');
const runner = require('autovpn.process');
const ROOT = '/var/run/autovpn-zapret';
const INPUT = '/etc/autovpn/runtime/zapret.json';
const DUAL_INPUT = '/etc/autovpn/runtime-zapret/zapret.json';
const ENGINE = '/usr/lib/autovpn-zapret';
const SERVICE = '/etc/init.d/autovpn-zapret';
const NFT = '/usr/sbin/nft';
const TABLE = 'autovpn_zapret';

function call(argv, limit) {
	let child = runner.popen(argv, 'r');
	if (child == null) return null;
	let raw = child.read(limit + 1) || '';
	return child.close() == 0 && length(raw) <= limit ? raw : null;
}
function command(argv) { return call(argv, 4096) != null; }
function directory() {
	let info = lstat(ROOT);
	if (info == null) return mkdir(ROOT, 0o700) != null;
	return info.type == 'directory' && info.uid == 0 && chmod(ROOT, 0o700) != null;
}
function write(name, raw) {
	let path = ROOT + '/' + name;
	let info = lstat(path + '.new');
	if (info != null && (info.type != 'file' || info.uid != 0)) return false;
	return writefile(path + '.new', raw) == length(raw) &&
		chmod(path + '.new', 0o600) != null && rename(path + '.new', path) != null;
}
function table() {
	let listing = call([NFT, 'list', 'tables'], 65536);
	if (listing == null) return { ok: false };
	if (match(listing, /(^|\n)table inet autovpn_zapret(\n|$)/) == null)
		return { ok: true, present: false };
	let raw = call([NFT, '-s', 'list', 'table', 'inet', TABLE], 32768);
	return { ok: raw != null && match(raw, /chain ownership_autovpn_zapret_v1\s*\{/) != null,
		present: true, raw: raw };
}
function queued() {
	let raw = readfile('/proc/net/netfilter/nfnetlink_queue', 65537);
	return raw != null && length(raw) <= 65536 && match(raw, /(^|\n)\s*20195\s/) != null;
}
function stopped() {
	let owned = table();
	if (!owned.ok) return false;
	if (!command([SERVICE, 'stop'])) return false;
	if (owned.present && !command([NFT, 'delete', 'table', 'inet', TABLE])) return false;
	if (!directory()) return false;
	for (let i = 0; i < 4; i++) unlink(ROOT + '/' + ['plan.json', 'rules.nft', 'nfqws.conf', 'canonical.nft'][i]);
	return true;
}
function validate(value) {
	if (value == null) return true;
	if (!zapret.validPlan(value)) return false;
	if (access(ENGINE + '/nfqws2', 'x') !== true || access(ENGINE + '/zapret-lib.lua', 'r') !== true ||
		access(ENGINE + '/zapret-antidpi.lua', 'r') !== true || !directory()) return false;
	if (!write('check.conf', zapret.config(value) + '--intercept=0\n')) return false;
	/* Parse config/Lua without binding a queue or touching the live service. */
	let ok = command([ENGINE + '/nfqws2', '@' + ROOT + '/check.conf']);
	unlink(ROOT + '/check.conf');
	return ok;
}
function check(value) {
	let owned = table();
	if (!owned.ok) return false;
	if (value == null) return !owned.present && !command([SERVICE, 'running']);
	return zapret.validPlan(value) && owned.present &&
		readfile(ROOT + '/plan.json', 65537) == sprintf('%J\n', value) &&
		readfile(ROOT + '/nfqws.conf', 32769) == zapret.config(value) &&
		readfile(ROOT + '/canonical.nft', 32769) == owned.raw &&
		readfile('/proc/sys/net/netfilter/nf_conntrack_acct', 8) == '1\n' &&
		command([SERVICE, 'running']) && queued();
}
function up(value) {
	if (value == null) return stopped();
	if (!validate(value) || !table().ok) return false;
	if (check(value)) return true;
	if (!stopped() || queued()) return false;
	if (!command(['/sbin/modprobe', 'nft_queue']) ||
		!command(['/sbin/sysctl', '-w', 'net.netfilter.nf_conntrack_acct=1'])) return false;
	if (!write('nfqws.conf', zapret.config(value)) || !write('rules.nft', zapret.nft(value)) ||
		!command([NFT, '-c', '-f', ROOT + '/rules.nft'])) return false;
	/* Queue before launching a tunnel. Deliberately no queue bypass. */
	if (!command([NFT, '-f', ROOT + '/rules.nft'])) return false;
	if (!command([SERVICE, 'start'])) { stopped(); return false; }
	for (let i = 0; i < 5; i++) {
		if (queued() && command([SERVICE, 'running'])) {
			let owned = table();
			if (owned.ok && owned.present && write('canonical.nft', owned.raw) &&
				write('plan.json', sprintf('%J\n', value))) return true;
			break;
		}
		command(['/bin/sleep', '1']);
	}
	stopped();
	return false;
}
function run() {
	let action = ARGV[0];
	if (index(['validate', 'up', 'check', 'down'], action) < 0) return false;
	if (action == 'down') return stopped();
	if (ARGV[1] != INPUT && ARGV[1] != DUAL_INPUT) return false;
	let raw = readfile(ARGV[1], 65537);
	if (raw == null || length(raw) > 65536) return false;
	let value;
	try { value = json(raw); } catch (e) { return false; }
	if (!zapret.validPlan(value)) return false;
	if (value != null && ((ARGV[1] == INPUT && value.version != 1) ||
		(ARGV[1] == DUAL_INPUT && (value.version != 2 || value.lane != 'vpn_zapret')))) return false;
	if (action == 'validate') return validate(value);
	if (action == 'check') return check(value);
	return up(value);
}
let ok = false;
try { ok = run(); } catch (e) { ok = false; }
printf('%J\n', ok ? { ok: true } : { ok: false, code: 'zapret_unavailable' });
exit(ok ? 0 : 1);
