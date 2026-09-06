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
	params: ['target', 'lane'],
	timeout: 30000,
	expect: { '': {} }
});

var callRuDbStatus = rpc.declare({
	object: 'luci.autovpn',
	method: 'ru_db_status',
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

function countOrDash(value) {
	return typeof value === 'number' && isFinite(value) && value >= 0 ? String(value) : '—';
}

function ruDatabaseText(status) {
	if (!status || typeof status !== 'object') return _('Status unavailable');
	if (status.code === 'update_in_progress')
		return _('Database update is in progress. Last success: %s.').format(checkedAt(status.last_success));
	if (status.ok === false && status.code)
		return _('Database status unavailable: %s.').format(valueOrDash(status.code));
	if (typeof status.last_attempt !== 'number' || !isFinite(status.last_attempt) || status.last_attempt <= 0)
		return _('Database has not been checked yet.');
	var lastSuccess = checkedAt(status.last_success);
	var details = _('Last attempt: %s; last success: %s; domains: %s; IPv4 CIDRs: %s').format(
		checkedAt(status.last_attempt), lastSuccess, countOrDash(status.domains), countOrDash(status.ipv4_cidrs));
	return status.ok === true ? _('Last database update succeeded. %s').format(details) :
		_('Last database update failed: %s. %s').format(valueOrDash(status.code), details);
}

return view.extend({
	load: function() {
		return Promise.all([
			callStatus(),
			callRuDbStatus().catch(function() { return null; })
		]);
	},

	render: function(data) {
		var status = Array.isArray(data) ? data[0] : data;
		var ruDatabase = Array.isArray(data) ? data[1] : null;
		status = status || {};
		var vpn = (status.runtime_lanes && status.runtime_lanes.vpn) || { ok: status.runtime_running, active_profile: status.runtime_profile, code: status.runtime_error };
		var table = E('table', { 'class': 'table' }, [
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('VPN')), E('td', { 'class': 'td left' }, vpn.ok ? valueOrDash(vpn.active_profile) : valueOrDash(vpn.code))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Router ID')), E('td', { 'class': 'td left' }, valueOrDash(status.router_id))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Phase')), E('td', { 'class': 'td left' }, valueOrDash(status.phase))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Credential')), E('td', { 'class': 'td left' }, status.credential_configured ? _('configured') : _('not configured'))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Desired revision')), E('td', { 'class': 'td left' }, revision(status.desired))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Applied revision')), E('td', { 'class': 'td left' }, revision(status.applied))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Last-good revision')), E('td', { 'class': 'td left' }, revision(status.last_good))]),
			E('tr', { 'class': 'tr' }, [E('td', { 'class': 'td left' }, _('Last error')), E('td', { 'class': 'td left' }, valueOrDash(status.last_error))])
		]);
		var ruDatabaseRow = E('div', { 'class': 'cbi-section' }, [
			E('h3', {}, _('Russian bypass database')),
			E('p', {}, ruDatabaseText(ruDatabase)),
			E('p', {}, _('This is update status only, not a VPN readiness or policy-application result. Automatic bypass is applied to the active VPN; manual Russian rules remain active when the database option is disabled.'))
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
		function pingSection(lane, heading, label) {
			var pingOutput = E('div', { 'id': 'autovpn-ping-results-' + lane });
			var ping = E('button', {
			'class': 'btn cbi-button cbi-button-action',
			'click': ui.createHandlerFn(this, function() {
				ping.disabled = true;
				return callPingAll(target.value, lane).then(function(result) {
					pingOutput.replaceChildren(
						E('p', {}, result.ok ? _('Checked at: %s; active profile: %s').format(checkedAt(result.checked_at), valueOrDash(result.active_profile)) : _('Ping all failed: %s').format(valueOrDash(result.code))),
						result.ok ? pingTable(result) : E('p', {}, _('No candidate results were returned.'))
					);
				}).catch(function() {
					pingOutput.replaceChildren(E('p', {}, _('Ping all is unavailable or timed out.')));
				}).finally(function() {
					ping.disabled = false;
				});
			})
			}, label);
			return E('section', { 'class': 'autovpn-ping-lane' }, [
				E('h3', {}, heading),
				ping,
				pingOutput
			]);
		}

		return E('div', { 'class': 'cbi-map', 'id': 'autovpn-status' }, [
			E('h2', {}, _('AutoVPN controller')),
			E('p', {}, _('The active VPN network supports VLESS, Hysteria2 and optional kernel AmneziaWG. Configure the subscription and VPN selection in Settings.')),
			table,
			ruDatabaseRow,
			E('p', {}, _('Ping all checks each candidate with HTTPS without changing the active VPN. It measures response latency, not throughput or ICMP reachability.')),
			E('div', { 'class': 'cbi-page-actions' }, [target]),
			E('div', { 'class': 'autovpn-ping-lanes' }, [
				pingSection('vpn', _('VPN'), _('Ping all: VPN'))
			]),
			E('div', { 'class': 'cbi-page-actions' }, [button, applyButton])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
