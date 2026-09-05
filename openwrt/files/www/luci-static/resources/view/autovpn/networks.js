'use strict';
'require view';
'require rpc';
'require ui';

var callStatus = rpc.declare({ object: 'luci.autovpn', method: 'network_status', expect: { '': {} } });
var callSetup = rpc.declare({ object: 'luci.autovpn', method: 'network_setup', expect: { '': {} } });
var callConfirm = rpc.declare({ object: 'luci.autovpn', method: 'network_confirm', params: ['transaction_id'], expect: { '': {} } });

return view.extend({
	load: function() { return callStatus(); },
	render: function(state) {
		var pending = state.phase === 'pending';
		var busy = pending || state.phase === 'rollback_conflict';
		var run = function(button, action) {
			button.disabled = true;
			return action().then(function(result) {
				if (!result.ok) ui.addNotification(null, E('p', {}, _('Network operation failed: %s').format(result.code || 'unknown')));
				window.setTimeout(function() { window.location.reload(); }, 500);
			}).catch(function() {
				ui.addNotification(null, E('p', {}, _('The connection was interrupted. Reopen this page via the original management network to check whether confirmation is required.')));
			}).finally(function() { button.disabled = false; });
		};
		var setup = E('button', { 'class': 'btn cbi-button cbi-button-action', 'disabled': busy || !state.ok ? '' : null,
			'click': ui.createHandlerFn(this, function() {
				ui.showModal(_('Create or update Wi-Fi'), [
					E('p', {}, _('Uses the base name and WPA2 password saved in Settings. Wi-Fi may briefly disconnect; connect by cable or keep access through the original management network. The VPN is stopped during setup. Confirm within 3 minutes or changes will be rolled back.')),
					E('div', { 'class': 'right' }, [
						E('button', { 'class': 'btn', 'click': ui.hideModal }, _('Cancel')),
						E('button', { 'class': 'btn cbi-button-action', 'click': ui.createHandlerFn(this, function() {
							ui.hideModal(); return run(setup, callSetup);
						}) }, _('Create / update'))
					])
				]);
			}) }, _('Create / update SSIDs'));
		var confirm = E('button', { 'class': 'btn cbi-button cbi-button-positive', 'disabled': pending && state.ready ? null : '',
			'click': ui.createHandlerFn(this, function() {
				return run(confirm, function() { return callConfirm(state.transaction_id); });
			}) }, _('Keep these networks'));
		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('Managed WPA2 Wi-Fi')),
			E('p', {}, _('First save the base Wi-Fi name and password in Settings. Existing LAN and SSIDs are preserved. All four WPA2 SSIDs use the same name on 2.4 and 5 GHz; the client chooses its band. VPN and zapret networks block traffic until their backend is ready.')),
			E('p', {}, _('State: %s').format(state.ok ? state.phase : state.code)),
			pending ? E('p', {}, _('Check the new Wi-Fi, then confirm before %s. Without confirmation the old network configuration will be restored.').format(new Date(state.deadline * 1000).toLocaleTimeString())) : '',
			E('ul', {}, (state.ssids || []).map(function(item) {
				return E('li', {}, item.ssid + ' — ' + (item.enabled ? _('enabled on available bands') : _('disabled')));
			})),
			E('p', {}, _('After confirming, use Refresh snapshot or Apply saved VPN settings on Overview to start the VPN. The installer primary LAN Wi-Fi retains administration access; isolated VPN and legacy direct managed networks do not.')),
			E('div', { 'class': 'cbi-page-actions' }, [setup, confirm])
		]);
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
