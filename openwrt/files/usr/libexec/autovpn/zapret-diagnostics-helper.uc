#!/usr/bin/ucode
'use strict';

import { readfile, writefile, chmod, rename } from 'fs';

const zapret = require('autovpn.zapret');
const diagnostics = require('autovpn.zapret_diagnostics');
const SOURCE = '/etc/autovpn/runtime-zapret/zapret.json';
const TARGET = '/tmp/autovpn-zapret-diagnostics/transport-plan.json';
const STATUS = '/tmp/autovpn-zapret-diagnostics/status.json';

function readJson(path, limit) {
	let raw = readfile(path, limit + 1);
	if (raw == null || length(raw) > limit) return null;
	try { return json(raw); } catch (e) { return null; }
}

function privateWrite(path, value) {
	let raw = sprintf('%J\n', value);
	return writefile(path + '.new', raw) == length(raw) && chmod(path + '.new', 0o600) != null &&
		rename(path + '.new', path) != null;
}

function makePlan(profile, strategy) {
	let value = zapret.diagnosticPlan(readJson(SOURCE, 65536), profile, strategy);
	if (value == null) return { ok: false, code: 'diagnostic_strategy_unavailable' };
	if (!privateWrite(TARGET, value)) return { ok: false, code: 'diagnostic_plan_write_failed' };
	return { ok: true, profile: profile, strategy: strategy };
}

function validateResult(scope, candidate) {
	if (!diagnostics.validCandidate(scope, candidate)) return { ok: false, code: 'invalid_diagnostic_candidate' };
	let value = readJson(STATUS, 8192);
	if (type(value) != 'object' || value.ok !== true || value.phase != 'ready' || value.scope != scope ||
		type(value.candidates) != 'array' || length(value.candidates) > 8)
		return { ok: false, code: 'diagnostic_not_ready' };
	for (let i = 0; i < length(value.candidates); i++) {
		let item = value.candidates[i];
		if (type(item) == 'object' && item.candidate_id == candidate) return { ok: true };
	}
	return { ok: false, code: 'candidate_not_found' };
}

let action = ARGV[0];
let result = action == 'make-plan' && ARGV[3] == null
	? makePlan(ARGV[1], ARGV[2])
	: action == 'validate-result' && ARGV[3] == null
		? validateResult(ARGV[1], ARGV[2])
	: { ok: false, code: 'invalid_diagnostic_action' };
printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
