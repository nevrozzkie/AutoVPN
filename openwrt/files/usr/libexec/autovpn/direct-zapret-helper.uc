#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename, unlink, access, mkdir, lstat, error as fsError } from 'fs';
import { cursor } from 'uci';

const direct = require('autovpn.direct-zapret');
const runner = require('autovpn.process');
const ROOT = '/var/run/autovpn-direct-zapret';
const ENGINE = '/usr/lib/autovpn-zapret';
const SERVICE = '/etc/init.d/autovpn-direct-zapret';
const NFT = '/usr/sbin/nft';
const TABLE = 'autovpn_direct_zapret';
const NETWORK_HELPER = '/usr/libexec/autovpn/network-helper.uc';

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
	if (match(listing, /(^|\n)table inet autovpn_direct_zapret(\n|$)/) == null)
		return { ok: true, present: false };
	let raw = call([NFT, '-s', 'list', 'table', 'inet', TABLE], 32768);
	return { ok: raw != null && match(raw, /chain ownership_autovpn_direct_zapret_v1\s*\{/) != null,
		present: true, raw: raw };
}
function queued() {
	let raw = readfile('/proc/net/netfilter/nfnetlink_queue', 65537);
	return raw != null && length(raw) <= 65536 && match(raw, /(^|\n)\s*20196\s/) != null;
}
function apply(raw) {
	if (!directory() || !write('rules.nft', raw) ||
		!command([NFT, '-c', '-f', ROOT + '/rules.nft']) || !command([NFT, '-f', ROOT + '/rules.nft'])) return false;
	let owned = table();
	return owned.ok && owned.present && write('canonical.nft', owned.raw);
}
function cleanEnabledFiles() {
	for (let i = 0; i < 3; i++) unlink(ROOT + '/' + ['nfqws.conf', 'check.conf', 'open.nft'][i]);
}
function blocked() {
	let paths = ['/etc/autovpn/state/maintenance.lock', '/etc/autovpn/state/update.lock'];
	for (let i = 0; i < length(paths); i++) {
		let path = paths[i];
		if (lstat(path) != null || fsError() != 'No such file or directory') return true;
	}
	return false;
}
function networkReady() {
	return command([NETWORK_HELPER, 'network-gate']);
}
function closeGuard() {
	let owned = table();
	if (!owned.ok || (owned.present && match(owned.raw, /chain ownership_autovpn_direct_zapret_v1\s*\{/) == null)) {
		command([SERVICE, 'stop']);
		return false;
	}
	/* Close forwarding before stopping the listener; down can never become a bypass. */
	let closed = apply(direct.closedNft());
	let stopped = command([SERVICE, 'stop']);
	if (!closed) return false;
	cleanEnabledFiles();
	return stopped && write('plan.json', 'null\n');
}
function setting() {
	let uci = cursor();
	uci.load('autovpn');
	let enabled = uci.get('autovpn', 'direct', 'zapret_enabled');
	if (enabled != null && enabled != '0' && enabled != '1') return { ok: false };
	if (enabled == '0') return { ok: true, value: null, enabled: false };
	let discordMedia = uci.get('autovpn', 'direct', 'discord_media');
	let stun = uci.get('autovpn', 'direct', 'stun');
	let mediaStrategy = uci.get('autovpn', 'direct', 'media_strategy');
	let mediaRepeats = uci.get('autovpn', 'direct', 'media_repeats');
	let hasOptions = discordMedia != null || stun != null || mediaStrategy != null || mediaRepeats != null;
	if ((discordMedia != null && (type(discordMedia) != 'string' || index(['0', '1'], discordMedia) < 0)) ||
		(stun != null && (type(stun) != 'string' || index(['0', '1'], stun) < 0)) ||
		(mediaStrategy != null && (type(mediaStrategy) != 'string' ||
			index(['fake', 'fake_badsum'], mediaStrategy) < 0)) ||
		(mediaRepeats != null && (type(mediaRepeats) != 'string' || match(mediaRepeats, /^[1-6]$/) == null)))
		return { ok: false };
	let wan = uci.get('autovpn', 'runtime', 'wan_device') || '';
	if (wan == '') {
		let raw = call(['/bin/ubus', 'call', 'network.interface.wan', 'status'], 4096);
		let status;
		try { status = raw == null ? null : json(raw); } catch (e) { status = null; }
		wan = type(status) == 'object' ? status.l3_device : '';
	}
	let value = hasOptions ? direct.plan(wan, true, {
		discord_media: discordMedia == '1',
		stun: stun == '1',
		media_strategy: mediaStrategy == null ? 'fake' : mediaStrategy,
		media_repeats: mediaRepeats == null ? 2 : int(mediaRepeats),
	}) : direct.plan(wan, true);
	return { ok: value !== false, value: value, enabled: true };
}
function validate(value) {
	if (value == null) return true;
	if (!direct.validPlan(value) || access(ENGINE + '/nfqws2', 'x') !== true ||
		access(ENGINE + '/zapret-lib.lua', 'r') !== true ||
		access(ENGINE + '/zapret-antidpi.lua', 'r') !== true || !directory()) return false;
	if (!write('check.conf', direct.config(value) + '--intercept=0\n')) return false;
	let ok = command([ENGINE + '/nfqws2', '@' + ROOT + '/check.conf']);
	unlink(ROOT + '/check.conf');
	return ok;
}
function check(value) {
	let owned = table();
	if (!owned.ok || !owned.present || readfile(ROOT + '/canonical.nft', 32769) != owned.raw ||
		readfile(ROOT + '/plan.json', 65537) != sprintf('%J\n', value)) return false;
	if (value == null) return !command([SERVICE, 'running']);
	return direct.validPlan(value) && readfile(ROOT + '/nfqws.conf', 32769) == direct.config(value) &&
		readfile('/proc/sys/net/netfilter/nf_conntrack_acct', 8) == '1\n' &&
		command([SERVICE, 'running']) && queued();
}
function up(value) {
	if (value == null) return closeGuard();
	if (blocked() || !networkReady()) {
		closeGuard();
		return false;
	}
	if (check(value)) return true;
	/* Establish the deny guard before any validation or queue ownership transition. */
	if (!closeGuard()) return false;
	/* Recheck after closing so a concurrent maintenance transition cannot be reopened. */
	if (blocked() || !networkReady() || !validate(value) || queued() || !command(['/sbin/modprobe', 'nft_queue']) ||
		!command(['/sbin/sysctl', '-w', 'net.netfilter.nf_conntrack_acct=1']) ||
		!write('nfqws.conf', direct.config(value)) ||
		!write('open.nft', direct.nft(value)) ||
		!command([NFT, '-c', '-f', ROOT + '/open.nft']) || !command([SERVICE, 'start'])) {
		closeGuard();
		return false;
	}
	for (let i = 0; i < 5; i++) {
		if (queued() && command([SERVICE, 'running'])) {
			if (apply(direct.nft(value)) && write('plan.json', sprintf('%J\n', value))) {
				unlink(ROOT + '/open.nft');
				return true;
			}
			break;
		}
		command(['/bin/sleep', '1']);
	}
	closeGuard();
	return false;
}
function run() {
	let action = ARGV[0];
	if (index(['validate', 'up', 'check', 'down'], action) < 0 || ARGV[1] != null) return false;
	if (action == 'down') return closeGuard();
	let desired = setting();
	if (desired.enabled != null) REPORT_ENABLED = desired.enabled;
	if (!desired.ok) {
		if (action == 'up') closeGuard();
		return false;
	}
	if (action == 'validate') return validate(desired.value);
	if (action == 'check') return check(desired.value);
	return up(desired.value);
}

let ok = false;
let REPORT_ENABLED = null;
try { ok = run(); } catch (e) {
	if (ARGV[0] == 'up') try { closeGuard(); } catch (ignored) {}
	ok = false;
}
let response = ok ? { ok: true } : { ok: false, code: 'direct_zapret_unavailable' };
if (ARGV[0] == 'check' && type(REPORT_ENABLED) == 'bool') response.enabled = REPORT_ENABLED;
printf('%J\n', response);
exit(ok ? 0 : 1);
