'use strict';
'require view';
'require rpc';
'require ui';
'require poll';

var start = rpc.declare({ object: 'luci.autovpn', method: 'zapret_diagnostics_start', params: ['scope'], expect: { '': {} } });
var status = rpc.declare({ object: 'luci.autovpn', method: 'zapret_diagnostics_status', expect: { '': {} } });
var cancel = rpc.declare({ object: 'luci.autovpn', method: 'zapret_diagnostics_cancel', expect: { '': {} } });
var apply = rpc.declare({ object: 'luci.autovpn', method: 'zapret_diagnostics_apply', params: ['candidate_id'], expect: { '': {} } });

function value(value) {
	return value === null || value === undefined || value === '' ? '—' : String(value);
}

function renderCandidates(result, output, candidate) {
	var candidates = Array.isArray(result && result.candidates) ? result.candidates : [];
	var previous = candidate.input.value;
	output.replaceChildren(E('p', {}, candidates.length ? _('Candidates: %s').format(candidates.map(function(item) {
		return value(item.candidate_id || item.id || item.strategy);
	}).join(', ')) : _('No candidates returned yet.')));
	candidate.input.replaceChildren(E('option', { value: '' }, _('Select a returned candidate')));
	candidates.forEach(function(item) {
		var id = item.candidate_id || item.id || item.strategy || '';
		candidate.input.appendChild(E('option', { value: id }, id));
	});
	if (candidates.some(function(item) { return (item.candidate_id || item.id || item.strategy) === previous; }))
		candidate.input.value = previous;
	else if (candidates.length)
		candidate.input.value = candidates[0].candidate_id || candidates[0].id || candidates[0].strategy || '';
}

return view.extend({
	load: function() { return status().catch(function() { return { ok: false, code: 'status_unavailable' }; }); },
	render: function(initial) {
		var scope = E('select', { 'class': 'cbi-input-select' }, [
			E('option', { value: 'direct' }, _('Direct YouTube/Discord')),
			E('option', { value: 'vless' }, _('VLESS transport')),
			E('option', { value: 'hysteria2' }, _('Hysteria2 transport'))
		]);
		var candidate = { input: E('select', { class: 'cbi-input-select' }) };
		var state = E('p', { role: 'status' });
		var candidates = E('div');
		var startButton;
		var cancelButton;
		var applyButton;
		var current = initial || {};
		function show(result) {
			result = result || {};
			current = result;
			state.textContent = _('State: %s; detail: %s').format(value(result.phase), value(result.code));
			startButton.disabled = result.phase === 'queued' || result.phase === 'running';
			cancelButton.disabled = result.phase !== 'queued' && result.phase !== 'running';
			renderCandidates(result, candidates, candidate);
			applyButton.disabled = !result.ok || !candidate.input.value || result.phase !== 'ready';
		}
		function run(button, operation) {
			button.disabled = true;
			return operation().then(show).catch(function(error) {
				show({ ok: false, phase: 'error', code: error.message || 'rpc_failed' });
			});
		}
		startButton = E('button', { class: 'btn cbi-button-action', click: function() {
			return run(startButton, function() { return start(scope.value); });
		} }, _('Start diagnostics'));
		cancelButton = E('button', { class: 'btn cbi-button-negative', click: function() {
			return run(cancelButton, cancel);
		} }, _('Cancel'));
		applyButton = E('button', { class: 'btn cbi-button-action', click: function() {
			return run(applyButton, function() { return apply(candidate.input.value.trim()); });
		} }, _('Apply candidate'));
		candidate.input.addEventListener('input', function() {
			applyButton.disabled = current.phase !== 'ready' || !current.ok || !candidate.input.value.trim();
		});
		show(initial || {});
		poll.add(function() { return status().then(show).catch(function(error) {
			show({ ok: false, phase: 'unavailable', code: error.message || 'status_unavailable' });
		}); }, 3);
		return E('div', { class: 'cbi-map', id: 'autovpn-zapret-diagnostics' }, [
			E('h2', {}, _('Zapret diagnostics')),
			E('p', {}, _('Run a bounded provider-specific check. Direct website tests and VPN transport tests are reported separately.')),
			E('label', { class: 'cbi-value' }, [E('span', { class: 'cbi-value-title' }, _('Scope')), scope]),
			E('div', { class: 'cbi-page-actions' }, [startButton, cancelButton]),
			state,
			candidates,
			E('label', { class: 'cbi-value' }, [E('span', { class: 'cbi-value-title' }, _('Candidate ID')), candidate.input]),
			E('div', { class: 'cbi-page-actions' }, [applyButton])
		]);
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
