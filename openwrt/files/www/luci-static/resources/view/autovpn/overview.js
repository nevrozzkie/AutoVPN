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

function valueOrDash(value) {
	return value === null || value === undefined || value === '' ? '—' : String(value);
}

function revision(entry) {
	return entry ? valueOrDash(entry.revision) : '—';
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

		return E('div', { 'class': 'cbi-map', 'id': 'autovpn-status' }, [
			E('h2', {}, _('AutoVPN controller')),
			E('p', {}, _('The VPN network supports VLESS, Hysteria2 and optional kernel AmneziaWG. Create Wi-Fi on the Networks page. Zapret integration is not yet active.')),
			table,
			E('div', { 'class': 'cbi-page-actions' }, [button, applyButton])
		]);
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
