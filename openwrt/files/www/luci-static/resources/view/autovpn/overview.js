'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({
	object: 'luci.autovpn',
	method: 'status',
	expect: { '': {} }
});

var callRefresh = rpc.declare({
	object: 'luci.autovpn',
	method: 'refresh',
	expect: { '': {} }
});

var callApplyPolicy = rpc.declare({
	object: 'luci.autovpn',
	method: 'apply_policy',
	expect: { '': {} }
});

var callPingAll = rpc.declare({
	object: 'luci.autovpn',
	method: 'ping_all',
	params: ['target'],
	timeout: 30000,
	expect: { '': {} }
});

function valueOrDash(value) {
	return value === null || value === undefined || value === '' ? '—' : String(value);
}

function revision(entry) {
	return entry ? valueOrDash(entry.revision) : '—';
}

function checkedAt(value) {
	if (typeof value !== 'number' || !isFinite(value) || value <= 0)
		return '—';
	var date = new Date(value * 1000);
	return isNaN(date.getTime()) ? '—' : date.toLocaleString();
}

function resultText(result) {
	var status = valueOrDash(result && result.status);
	if (status === 'ok') return _('OK');
	if (status === 'failed') return _('Failed');
	if (status === 'http_error') return _('HTTP error');
	if (status === 'unavailable') return _('Unavailable');
	return status;
}

function pingTable(response) {
	var rows = [E('tr', { 'class': 'tr' }, [
		E('th', { 'class': 'th' }, _('VPN')),
		E('th', { 'class': 'th' }, _('Status')),
		E('th', { 'class': 'th' }, _('HTTPS time (ms)')),
		E('th', { 'class': 'th' }, _('HTTP code'))
	])];
	(response.results || []).forEach(function(result) {
		rows.push(E('tr', { 'class': 'tr' }, [
			E('td', { 'class': 'td' }, valueOrDash(result.profile)),
			E('td', { 'class': 'td' }, resultText(result)),
			E('td', { 'class': 'td' }, valueOrDash(result.latency_ms)),
			E('td', { 'class': 'td' }, valueOrDash(result.http_status))
		]));
	});
	return E('table', { 'class': 'table' }, rows);
}

return view.extend({
	load: function() {
		return callStatus();
	},

	render: function(status) {
		var table = E('table', { 'class': 'table' }, [
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('VPN runtime')), E('td', { 'class': 'td left' }, status.runtime_running ? valueOrDash(status.runtime_profile) : valueOrDash(status.runtime_error))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Router ID')), E('td', { 'class': 'td left' }, valueOrDash(status.router_id))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Phase')), E('td', { 'class': 'td left' }, valueOrDash(status.phase))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Credential')), E('td', { 'class': 'td left' }, status.credential_configured ? _('configured') : _('not configured'))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Desired revision')), E('td', { 'class': 'td left' }, revision(status.desired))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Applied revision')), E('td', { 'class': 'td left' }, revision(status.applied))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Last-good revision')), E('td', { 'class': 'td left' }, revision(status.last_good))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Last error')), E('td', { 'class': 'td left' }, valueOrDash(status.last_error))])
		]);

		var button = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(this, function() {
				button.disabled = true;
				return callRefresh().then(function(result) {
					ui.addNotification(null, E('p', {}, result.ok ? _('Refresh completed') : _('Refresh failed: %s').format(valueOrDash(result.code))));
					window.setTimeout(function() { window.location.reload(); }, 500);
				}).finally(function() {
					button.disabled = false;
				});
			})
		}, _('Refresh snapshot'));
		var applyButton = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(this, function() {
				return callApplyPolicy().then(function(result) {
					ui.addNotification(null, E('p', {}, result.ok ? _('VPN settings applied') : _('Apply failed: %s').format(valueOrDash(result.code))));
				});
			})
		}, _('Apply saved VPN settings'));
		var target = E('select', { 'class': 'cbi-input-select' }, [
			E('option', { 'value': 'youtube' }, _('YouTube')),
			E('option', { 'value': 'instagram' }, _('Instagram'))
		]);
		var pingButton = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(this, function() {
				pingButton.disabled = true;
				return callPingAll(target.value).then(function(result) {
					pingOutput.replaceChildren(
						E('p', {}, result.ok ? _('Checked at: %s; active profile: %s').format(checkedAt(result.checked_at), valueOrDash(result.active_profile)) : _('Ping all failed: %s').format(valueOrDash(result.code))),
						result.ok ? pingTable(result) : E('p', {}, _('No candidate results were returned.'))
					);
				}).catch(function() {
					pingOutput.replaceChildren(E('p', {}, _('Ping all is unavailable or timed out.')));
				}).finally(function() {
					pingButton.disabled = false;
				});
			})
		}, _('Ping all'));
		var pingOutput = E('div', { 'id': 'autovpn-ping-results' });

		return E('div', { 'class': 'cbi-map', 'id': 'autovpn-status' }, [
			E('h2', {}, _('AutoVPN controller')),
			E('p', {}, _('The VPN network supports VLESS, Hysteria2 and optional kernel AmneziaWG. Create Wi-Fi on the Networks page. Zapret integration is not yet active.')),
			table,
			E('p', {}, _('Ping all checks each candidate with HTTPS without changing the active VPN. It measures response latency, not throughput or ICMP reachability.')),
			E('div', { 'class': 'cbi-page-actions' }, [target, pingButton]),
			pingOutput,
			E('div', { 'class': 'cbi-page-actions' }, [button, applyButton])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
