'use strict';
'require view';
'require rpc';
'require ui';
'require uci';

var callConfigure = rpc.declare({
	object: 'luci.autovpn', method: 'setup_configure',
	params: ['nonce', 'base_url', 'router_id', 'credential', 'base_ssid', 'password'], expect: { '': {} }
});
var callNetworkStatus = rpc.declare({ object: 'luci.autovpn', method: 'network_status', expect: { '': {} } });
var callNetworkSetup = rpc.declare({ object: 'luci.autovpn', method: 'network_setup', expect: { '': {} } });
var callNetworkConfirm = rpc.declare({ object: 'luci.autovpn', method: 'network_confirm', params: ['transaction_id'], expect: { '': {} } });
var callActivate = rpc.declare({ object: 'luci.autovpn', method: 'setup_activate', expect: { '': {} } });

function nonce() {
	var bytes = new Uint8Array(24);
	window.crypto.getRandomValues(bytes);
	return Array.prototype.map.call(bytes, function(byte) { return ('0' + byte.toString(16)).slice(-2); }).join('');
}

function input(label, name, type, placeholder, description, value, readonly) {
	return E('div', { 'class': 'cbi-value' }, [
		E('label', { 'class': 'cbi-value-title', 'for': 'autovpn-setup-' + name }, label),
		E('div', { 'class': 'cbi-value-field' }, [
			E('input', { 'id': 'autovpn-setup-' + name, 'name': name, 'type': type, 'placeholder': placeholder, 'value': value || '', 'readonly': readonly ? '' : null, 'autocomplete': type == 'password' ? 'new-password' : 'off' }),
			E('div', { 'class': 'cbi-value-description' }, description)
		])
	]);
}

return view.extend({
	load: function() { return Promise.all([callNetworkStatus(), uci.load('autovpn')]); },
	render: function(data) {
		var network = data[0];
		var enabled = uci.get('autovpn', 'main', 'enabled') === '1';
		var prepared = uci.get('autovpn', 'main', 'setup_prepared') === '1';
		var configured = network.phase === 'pending' || network.phase === 'confirmed';
		var pending = network.phase === 'pending';
		var confirmed = network.phase === 'confirmed';
		var bootstrap = uci.get('autovpn', 'wifi', 'bootstrap_completed') === '1';
		var installerSsid = bootstrap ? (uci.get('autovpn', 'wifi', 'base_ssid') || '') : '';
		var message = E('p', { 'class': 'alert-message notice' }, _('Enter the pairing data from your AutoVPN server. The token is sent once to the router over the authenticated LuCI session and is never shown again.'));
		var configure = E('button', { 'class': 'btn cbi-button cbi-button-action', 'disabled': enabled ? '' : null }, _('Save pairing and Wi-Fi settings'));
		var create = E('button', { 'class': 'btn cbi-button cbi-button-action', 'disabled': !prepared || configured || enabled ? '' : null }, _('Create managed Wi-Fi'));
		var confirm = E('button', { 'class': 'btn cbi-button cbi-button-positive', 'disabled': pending && network.ready ? null : '' }, _('Keep these networks'));
		var activate = E('button', { 'class': 'btn cbi-button cbi-button-positive', 'disabled': confirmed && prepared && !enabled ? null : '' }, _('Enable AutoVPN'));
		var busy = function(button, action) {
			button.disabled = true;
			return action().then(function(result) {
				if (!result.ok) ui.addNotification(null, E('p', {}, _('Operation failed: %s').format(result.code || 'unknown')));
				else window.setTimeout(function() { window.location.reload(); }, 350);
			}).catch(function() {
				ui.addNotification(null, E('p', {}, _('Connection interrupted. Reopen LuCI on the original management network and check the Wi-Fi step.')));
			}).finally(function() { button.disabled = false; });
		};
		configure.addEventListener('click', ui.createHandlerFn(this, function() {
			var values = {};
			['base_url', 'router_id', 'credential', 'base_ssid', 'password'].forEach(function(name) {
				values[name] = document.getElementById('autovpn-setup-' + name).value;
			});
			return busy(configure, function() { return callConfigure(nonce(), values.base_url, values.router_id, values.credential, values.base_ssid, values.password); });
		}));
		create.addEventListener('click', ui.createHandlerFn(this, function() {
			ui.showModal(_('Create managed Wi-Fi'), [
				E('p', {}, _('The VPN remains disabled. Existing LAN and management Wi-Fi are preserved, but radios may briefly reconnect. Test the new Wi-Fi, then return to LuCI through the original management LAN or SSID to confirm within 3 minutes; otherwise the router restores the previous network configuration.')),
				E('div', { 'class': 'right' }, [
					E('button', { 'class': 'btn', 'click': ui.hideModal }, _('Cancel')),
					E('button', { 'class': 'btn cbi-button-action', 'click': ui.createHandlerFn(this, function() { ui.hideModal(); return busy(create, callNetworkSetup); }) }, _('Create'))
				])
			]);
		}));
		confirm.addEventListener('click', ui.createHandlerFn(this, function() { return busy(confirm, function() { return callNetworkConfirm(network.transaction_id); }); }));
		activate.addEventListener('click', ui.createHandlerFn(this, function() { return busy(activate, callActivate); }));
		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('AutoVPN first setup')),
			message,
			E('p', {}, _('Use HTTPS LuCI or a trusted wired LAN when entering the token. An authenticated HTTP session alone does not encrypt it.')),
			enabled ? E('p', { 'class': 'alert-message notice' }, _('AutoVPN is enabled. Check Overview for connection status; use Settings for further changes.')) : '',
			E('div', { 'class': 'cbi-section' }, [
				input(_('AutoVPN server URL'), 'base_url', 'url', 'https://vpn.example', _('Your private AutoVPN website URL. It is not the package-download address.')),
				input(_('Router ID'), 'router_id', 'text', 'router_...', _('The device identifier created in the AutoVPN admin panel.')),
				input(_('Pairing token'), 'credential', 'password', 'avrt_...', _('Stored in a root-only file on this router.')),
				input(_('Base Wi-Fi name'), 'base_ssid', 'text', 'Dorm', bootstrap ? _('Installer Wi-Fi is already created and confirmed. Change its name later in Settings, then Networks. Managed names use -в, -з and -вз.') : _('Up to 27 UTF-8 bytes; managed names use -в, -з and -вз. The same SSID is used on 2.4 and 5 GHz, so clients choose a radio automatically.'), installerSsid, bootstrap),
				input(_('WPA2-PSK password'), 'password', 'password', '', bootstrap ? _('Leave blank to keep installer Wi-Fi password. It is never shown here.') : _('8–63 printable ASCII characters, or a 64-digit hexadecimal key.')),
				E('div', { 'class': 'cbi-page-actions' }, [configure])
			]),
			E('h3', {}, _('Connection steps')),
			E('ol', {}, [
				E('li', {}, prepared || enabled ? _('Initial settings are saved; an invalid token can be corrected by repeating the first step before activation.') : _('Save pairing and Wi-Fi settings. WAN is detected from the active OpenWrt wan interface.')),
				E('li', {}, pending ? _('Test a new managed Wi-Fi, then confirm it from the original management LAN or SSID before the deadline.') : confirmed ? _('Managed Wi-Fi is confirmed.') : _('Create managed Wi-Fi.')),
				E('li', {}, confirmed ? _('Enable AutoVPN after the network confirmation.') : _('AutoVPN stays disabled until confirmation.'))
			]),
			pending ? E('p', { 'class': 'alert-message warning' }, _('Confirmation is required before %s.').format(new Date(network.deadline * 1000).toLocaleTimeString())) : '',
			E('div', { 'class': 'cbi-page-actions' }, [create, confirm, activate]),
			E('p', {}, bootstrap ? _('Installer Wi-Fi is already confirmed. Use Settings and Networks to change it; use this page only for pairing and activation.') : _('Use Settings later for routing policy and Networks for SSID maintenance. Radio settings and hardware offloading remain standard LuCI controls.'))
		]);
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
