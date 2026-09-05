#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, lstat } from 'fs';

const MAX_INPUT = 2097152;
const MAX_OUTPUT = 2097152;
const MAX_DOMAINS = 20000;
const MAX_CIDRS = 30000;

function exact(value, expected) {
	return type(value) == 'object' && join(',', sort(keys(value))) == join(',', sort(expected));
}

function readJson(path) {
	let info = lstat(path);
	if (info == null || info.type != 'file' || info.uid != 0) return null;
	let raw = readfile(path, MAX_INPUT + 1);
	if (raw == null || length(raw) > MAX_INPUT) return null;
	try { return json(raw); } catch (e) { return null; }
}

function domain(value, suffix) {
	if (type(value) != 'string' || length(value) < 1 || length(value) > 253 ||
		match(value, /^[.A-Za-z0-9-]+$/) == null) return false;
	if (suffix && substr(value, 0, 1) == '.') value = substr(value, 1);
	else if (substr(value, 0, 1) == '.') return false;
	if (length(value) < 1 || substr(value, length(value) - 1) == '.') return false;
	let labels = split(value, '.');
	for (let i = 0; i < length(labels); i++) {
		if (length(labels[i]) < 1 || length(labels[i]) > 63 ||
			match(labels[i], /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/) == null) return false;
	}
	return true;
}

function domainArray(value, suffix) {
	if (type(value) != 'array') return false;
	for (let i = 0; i < length(value); i++) if (!domain(value[i], suffix)) return false;
	return true;
}

function octets(value) {
	if (type(value) != 'string' || match(value, /^(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}$/) == null)
		return null;
	let parts = split(value, '.');
	for (let i = 0; i < 4; i++) {
		parts[i] = int(parts[i]);
		if (parts[i] > 255) return null;
	}
	return parts;
}

function ipNumber(parts) {
	return ((parts[0] * 256 + parts[1]) * 256 + parts[2]) * 256 + parts[3];
}

/* 1 means keep, 0 means a deliberately discarded non-public/IPv6 prefix. */
function cidr(value) {
	if (type(value) != 'string' || length(value) < 3 || length(value) > 64) return -1;
	if (index(value, ':') >= 0) {
		let ipv6 = split(value, '/');
		if (length(ipv6) != 2 || match(ipv6[1], /^(0|[1-9][0-9]{0,2})$/) == null || int(ipv6[1]) > 128 ||
			match(ipv6[0], /^[0-9A-Fa-f:]+$/) == null) return -1;
		let compression = index(ipv6[0], '::');
		if (compression >= 0 && index(substr(ipv6[0], compression + 2), '::') >= 0) return -1;
		let halves = compression >= 0 ? split(ipv6[0], '::') : [ipv6[0]];
		if (length(halves) > 2) return -1;
		let groups = 0;
		for (let half = 0; half < length(halves); half++) {
			if (length(halves[half]) == 0) continue;
			let values = split(halves[half], ':');
			for (let group = 0; group < length(values); group++) {
				if (match(values[group], /^[0-9A-Fa-f]{1,4}$/) == null) return -1;
				groups++;
			}
		}
		if ((compression < 0 && groups != 8) || (compression >= 0 && groups >= 8)) return -1;
		return 0;
	}
	let pieces = split(value, '/');
	if (length(pieces) != 2 || match(pieces[1], /^([1-9]|[12][0-9]|3[0-2])$/) == null) return -1;
	let ip = octets(pieces[0]);
	if (ip == null) return -1;
	let prefix = int(pieces[1]);
	let size = 1;
	for (let bit = prefix; bit < 32; bit++) size *= 2;
	let start = ipNumber(ip);
	if (start % size != 0) return -1;
	let end = start + size - 1;
	/* Never route private, loopback, link-local, CGNAT or non-unicast ranges as RU. */
	let forbidden = [
		[0, 16777215], [167772160, 184549375], [1681915904, 1686110207],
		[2130706432, 2147483647], [2851995648, 2852061183], [2886729728, 2887778303],
		[3221225472, 3221225727], [3221225984, 3221226239], [3232235520, 3232301055],
		[3323068416, 3323199487], [3325256704, 3325256959], [3405803776, 3405804031],
		[3758096384, 4294967295],
	];
	for (let i = 0; i < length(forbidden); i++)
		if (start <= forbidden[i][1] && forbidden[i][0] <= end) return 0;
	return 1;
}

function appendUnique(target, seen, values) {
	for (let i = 0; i < length(values); i++) {
		let key = '$' + values[i];
		if (seen[key] === true) continue;
		seen[key] = true;
		push(target, values[i]);
	}
}

function combine(work) {
	let directory = lstat(work);
	if (type(work) != 'string' || match(work, /^\/tmp\/autovpn-ru-db\.[A-Za-z0-9]+$/) == null ||
		directory == null || directory.type != 'directory' || directory.uid != 0) return { ok: false, code: 'unsafe_work_directory' };
	let geosite = readJson(work + '/geosite.json');
	let geoip = readJson(work + '/geoip.json');
	if (!exact(geosite, ['version', 'rules']) || type(geosite.version) != 'int' || geosite.version !== 1 || type(geosite.rules) != 'array' ||
		length(geosite.rules) != 1 || !exact(geosite.rules[0], ['domain', 'domain_suffix']) ||
		!domainArray(geosite.rules[0].domain, false) || !domainArray(geosite.rules[0].domain_suffix, true))
		return { ok: false, code: 'invalid_geosite' };
	if (!exact(geoip, ['version', 'rules']) || type(geoip.version) != 'int' || geoip.version !== 1 || type(geoip.rules) != 'array' ||
		length(geoip.rules) != 1 || !exact(geoip.rules[0], ['ip_cidr']) || type(geoip.rules[0].ip_cidr) != 'array')
		return { ok: false, code: 'invalid_geoip' };
	if (length(geosite.rules[0].domain) + length(geosite.rules[0].domain_suffix) < 1 ||
		length(geosite.rules[0].domain) + length(geosite.rules[0].domain_suffix) > MAX_DOMAINS ||
		length(geoip.rules[0].ip_cidr) > MAX_CIDRS) return { ok: false, code: 'ru_db_limits_exceeded' };

	let domains = [];
	let suffixes = [];
	let cidrs = [];
	let domainSeen = {};
	let suffixSeen = {};
	let cidrSeen = {};
	appendUnique(domains, domainSeen, geosite.rules[0].domain);
	appendUnique(suffixes, suffixSeen, ['ru', 'xn--p1ai', 'su']);
	for (let i = 0; i < length(geosite.rules[0].domain_suffix); i++) {
		let suffix = geosite.rules[0].domain_suffix[i];
		appendUnique(suffixes, suffixSeen, [substr(suffix, 0, 1) == '.' ? substr(suffix, 1) : suffix]);
	}
	if (length(domains) + length(suffixes) > MAX_DOMAINS) return { ok: false, code: 'ru_db_limits_exceeded' };
	for (let i = 0; i < length(geoip.rules[0].ip_cidr); i++) {
		let value = geoip.rules[0].ip_cidr[i];
		let accepted = cidr(value);
		if (accepted < 0) return { ok: false, code: 'invalid_geoip' };
		if (accepted > 0) appendUnique(cidrs, cidrSeen, [value]);
	}
	if (length(cidrs) < 1) return { ok: false, code: 'invalid_geoip' };
	sort(domains);
	sort(suffixes);
	sort(cidrs);
	let result = { version: 1, rules: [
		{ domain: domains, domain_suffix: suffixes },
		{ ip_cidr: cidrs },
	] };
	let raw = sprintf('%J\n', result);
	if (lstat(work + '/ru.json') != null || length(raw) > MAX_OUTPUT || writefile(work + '/ru.json', raw) != length(raw) ||
		chmod(work + '/ru.json', 0o600) == null) return { ok: false, code: 'ru_db_write_failed' };
	return { ok: true, domains: length(domains) + length(suffixes), ipv4_cidrs: length(cidrs) };
}

let result;
try {
	result = ARGV[0] == 'merge' && ARGV[2] == null ? combine(ARGV[1]) : { ok: false, code: 'invalid_arguments' };
} catch (e) { result = { ok: false, code: 'ru_db_helper_failed' }; }
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
