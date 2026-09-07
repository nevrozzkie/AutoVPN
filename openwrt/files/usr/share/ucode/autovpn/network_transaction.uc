'use strict';

const CONFIGS = ['network', 'wireless', 'dhcp', 'firewall'];
function fail(code) { return { ok: false, code: code }; }
function valid(state) {
	if (state == null) return true;
	if (type(state) != 'object' || state.version != 1 || type(state.sequence) != 'int' || state.sequence < 1 ||
		type(state.id) != 'string' || match(state.id, /^[0-9]+-[0-9]+$/) == null ||
		type(state.deadline) != 'int' || type(state.expires_uptime) != 'int' || type(state.ready) != 'bool' ||
		index(['pending', 'confirmed', 'rolled_back', 'rollback_conflict'], state.phase) < 0 ||
		type(state.before) != 'object' || type(state.after) != 'object' ||
		type(state.ssids) != 'array' || type(state.radios) != 'array' ||
		(state.wired_ports != null && type(state.wired_ports) != 'array')) return false;
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		if (type(state.before[name]) != 'string' || type(state.after[name]) != 'string' ||
			length(state.before[name]) > 65536 || length(state.after[name]) > 65536) return false;
	}
	return true;
}
function status(io) {
	let state = io.load();
	if (!valid(state)) return fail('network_journal_invalid');
	if (state == null) return { ok: true, phase: 'not_configured' };
	return { ok: true, phase: state.phase, transaction_id: state.id, deadline: state.deadline,
		ready: state.ready, ssids: state.ssids, radios: state.radios };
}
function gate(io) {
	let state = io.load();
	if (!valid(state)) return fail('network_journal_invalid');
	if (state != null && index(['pending', 'rollback_conflict'], state.phase) >= 0)
		return fail('network_confirmation_required');
	return { ok: true };
}
function rollback(io, state, boot) {
	/* Close forwarding first; keep backups when an unrelated edit prevents restoration. */
	let safe = boot || io.close();
	let conflict = false;
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		let current = io.read(name);
		if (current == state.before[name]) continue;
		if (current != state.after[name] || !io.write(name, state.before[name])) conflict = true;
	}
	if (!boot && !io.reload()) safe = false;
	state.phase = conflict || !safe ? 'rollback_conflict' : 'rolled_back';
	state.ready = false;
	if (!io.save(state)) return fail('network_journal_write_failed');
	return conflict || !safe ? fail('network_rollback_requires_attention') : { ok: true, phase: 'rolled_back' };
}
function recover(io, boot) {
	let state = io.load();
	if (!valid(state)) return fail('network_journal_invalid');
	if (state == null || index(['pending', 'rollback_conflict'], state.phase) < 0) return { ok: true, idle: true };
	if (!boot && state.phase == 'pending' && io.uptime() < state.expires_uptime) return { ok: true, pending: true };
	return rollback(io, state, boot);
}
function begin(settings, io, planner) {
	let old = io.load();
	if (!valid(old)) return fail('network_journal_invalid');
	if (old != null && index(['pending', 'rollback_conflict'], old.phase) >= 0) return fail('network_transaction_pending');
	if (!io.clean()) return fail('uncommitted_network_changes');
	let before = {};
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		before[name] = io.read(name);
		if (type(before[name]) != 'string' || length(before[name]) > 65536) return fail('network_config_unavailable');
	}
	let staged = io.stage(before, settings, planner);
	if (!staged.ok) return staged;
	let sequence = old == null ? 1 : old.sequence + 1;
	let state = { version: 1, sequence: sequence, id: io.now() + '-' + sequence,
		phase: 'pending', ready: false, deadline: io.now() + 180, expires_uptime: io.uptime() + 180,
		before: before, after: staged.after, ssids: staged.ssids, radios: staged.radios,
		wired_ports: staged.wired_ports || [] };
	if (!valid(state)) return fail('network_stage_invalid');
	/* Refuse to merge staged edits from another session or overwrite a concurrent commit. */
	if (!io.clean()) return fail('uncommitted_network_changes');
	for (let i = 0; i < length(CONFIGS); i++)
		if (io.read(CONFIGS[i]) != before[CONFIGS[i]]) return fail('network_config_changed');
	if (!io.close()) return fail('network_guard_failed');
	if (!io.save(state)) return fail('network_journal_write_failed');
	/* The independent procd watchdog survives a disconnected LuCI request. */
	if (!io.watch()) {
		rollback(io, state, false);
		return fail('network_watchdog_failed');
	}
	for (let i = 0; i < length(CONFIGS); i++) {
		let name = CONFIGS[i];
		if (io.read(name) != before[name] || !io.write(name, staged.after[name])) {
			let restored = rollback(io, state, false);
			return restored.ok ? fail('network_write_failed') : restored;
		}
	}
	if (!io.reload()) {
		let restored = rollback(io, state, false);
		return restored.ok ? fail('network_reload_failed') : restored;
	}
	state.ready = true;
	if (!io.save(state)) {
		rollback(io, state, false);
		return fail('network_journal_write_failed');
	}
	return status(io);
}
function confirm(id, io) {
	let state = io.load();
	if (!valid(state)) return fail('network_journal_invalid');
	if (state == null || state.phase != 'pending' || !state.ready || id != state.id)
		return fail('network_confirmation_mismatch');
	if (io.uptime() >= state.expires_uptime) return rollback(io, state, false);
	for (let i = 0; i < length(CONFIGS); i++)
		if (io.read(CONFIGS[i]) != state.after[CONFIGS[i]]) return fail('network_config_changed');
	if (!io.ready(state)) return fail('wifi_not_ready');
	state.phase = 'confirmed';
	if (!io.save(state)) return fail('network_journal_write_failed');
	return status(io);
}

return { valid: valid, status: status, gate: gate, begin: begin, recover: recover, confirm: confirm };
