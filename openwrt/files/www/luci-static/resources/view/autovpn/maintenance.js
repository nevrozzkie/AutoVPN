'use strict';
'require view';
'require rpc';
'require ui';
'require uci';
'require poll';

var maintain = rpc.declare({ object: 'luci.autovpn', method: 'maintenance_action', params: ['nonce', 'action', 'base_url', 'router_id', 'credential', 'confirmation'], expect: { '': {} } });
var resume = rpc.declare({ object: 'luci.autovpn', method: 'maintenance_resume', expect: { '': {} } });
var check = rpc.declare({ object: 'luci.autovpn', method: 'update_check', params: ['tag'], expect: { '': {} } });
var apply = rpc.declare({ object: 'luci.autovpn', method: 'update_apply', params: ['candidate_id'], expect: { '': {} } });
var status = rpc.declare({ object: 'luci.autovpn', method: 'update_status', expect: { '': {} } });

function nonce() {
	var bytes = new Uint8Array(24);
	window.crypto.getRandomValues(bytes);
	return Array.prototype.map.call(bytes, function(b) { return ('0' + b.toString(16)).slice(-2); }).join('');
}

return view.extend({
	load: function() { return uci.load('autovpn'); },
	render: function() {
		var candidate = null;
		var busy = false;
		var buttons = [];
		var message = E('p', { 'role': 'status' });
		var updateState = E('p', { 'role': 'status' });
		function field(label, type, value) {
			var control = E('input', { 'type': type, 'value': value || '', 'autocomplete': type === 'password' ? 'new-password' : 'off' });
			return { input: control, node: E('label', { 'class': 'cbi-value' }, [E('span', { 'class': 'cbi-value-title' }, label), control]) };
		}
		var url = field(_('AutoVPN server URL'), 'url', uci.get('autovpn', 'main', 'base_url'));
		var id = field(_('Router ID'), 'text', uci.get('autovpn', 'main', 'router_id'));
		var token = field(_('New pairing token'), 'password');
		var confirmation = field(_('Confirmation (REBIND or RESET)'), 'text');
		var tag = field(_('GitHub release tag'), 'text');
		function run(action) {
			if (busy) return Promise.resolve();
			busy = true;
			buttons.forEach(function(button) { button.disabled = true; });
			message.textContent = _('Working…');
			return action().then(function(result) {
				if (!result.ok) throw new Error(result.code || 'operation_failed');
				message.textContent = result.phase === 'queued' || result.phase === 'checking' ? _('Started in the background. Wait for the status below; do not resume before ready.') : result.reset ? _('Reset complete. Enter pairing data, press Rebind, then Resume AutoVPN.') : result.requires_activation ? _('Saved. Press Resume AutoVPN when ready.') : _('Done.');
				return result;
			}).catch(function(error) {
				message.textContent = _('Operation failed or connection interrupted: %s. Check status before retrying.').format(error.message || 'RPC');
			}).finally(function() {
				busy = false;
				buttons.forEach(function(button) { button.disabled = false; });
				install.disabled = !candidate;
			});
		}
		function button(label, action) {
			var node = E('button', { 'class': 'btn cbi-button-action', 'click': function() { return run(action); } }, label);
			buttons.push(node);
			return node;
		}
		function binding(action) {
			var secret = token.input.value;
			token.input.value = '';
			return maintain(nonce(), action, url.input.value.trim(), id.input.value.trim(), secret, confirmation.input.value.trim());
		}
		var install = button(_('Install checked version'), function() {
			if (!candidate) return Promise.reject(new Error('check_update_first'));
			return new Promise(function(resolve) {
				ui.showModal(_('Update AutoVPN application'), [
					E('p', {}, _('VPN clients will disconnect and remain blocked until you press Resume AutoVPN. Router firmware, kernel and Wi-Fi settings are not updated.')),
					E('div', { 'class': 'right' }, [
						E('button', { 'class': 'btn', 'click': function() { ui.hideModal(); resolve({ ok: true }); } }, _('Cancel')),
						E('button', { 'class': 'btn cbi-button-negative', 'click': function() {
							ui.hideModal(); var approved = candidate; candidate = null; resolve(apply(approved));
						} }, _('Install'))
					])
				]);
			});
		});
		install.disabled = true;
		tag.input.addEventListener('input', function() { candidate = null; install.disabled = true; });
		poll.add(function() {
			return status().then(function(result) {
				updateState.textContent = _('Installed: %s; update: %s; candidate: %s; detail: %s').format(result.current_version || '?', result.phase || '?', result.candidate_version || '—', result.code || '—');
				if (result.phase === 'checked' && result.candidate_tag === tag.input.value.trim()) {
					candidate = result.candidate_id || null;
					install.disabled = busy || !candidate;
				} else if (!busy) {
					candidate = null;
					install.disabled = true;
				}
			}).catch(function() { updateState.textContent = _('Status unavailable; LuCI may be restarting. Reopen this page if necessary.'); });
		}, 5);
		return E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, _('AutoVPN maintenance')),
			E('p', {}, _('Use HTTPS LuCI or a trusted wired management LAN. Tokens are write-only and never returned by this page. Generate a token in the AutoVPN website first; this page replaces only the local credential.')),
			url.node, id.node, token.node, confirmation.node,
			E('p', {}, _('Rotation keeps the current router identity and VPN running. Rebind (type REBIND) clears old VPN state and pauses VPN. Reset (type RESET) clears AutoVPN pairing and policy, but preserves WAN, LAN, Wi-Fi names/passwords and network recovery data. It is not a factory reset.')),
			E('div', { 'class': 'cbi-page-actions' }, [
				button(_('Replace token'), function() { return binding('rotate'); }),
				button(_('Rebind router'), function() { return binding('rebind'); }),
				button(_('Reset AutoVPN only'), function() { return binding('reset'); }),
				button(_('Resume AutoVPN'), resume)
			]),
			message,
			E('h3', {}, _('Application update')),
			E('p', {}, _('Only signed application releases from the GitHub repository pinned by the initial installer are accepted. Enter a published release tag, check it, then approve that exact version. No remote installation script is executed.')),
			tag.node, updateState,
			E('div', { 'class': 'cbi-page-actions' }, [button(_('Check release'), function() {
				candidate = null;
				return check(tag.input.value.trim()).then(function(result) {
					if (result.ok) {
						candidate = result.id || null;
						updateState.textContent = result.id ? _('Checked version: %s').format(result.version) : _('Checking release in the background…');
					}
					return result;
				});
			}), install]),
			E('p', {}, _('After reset, enter pairing data and use Rebind, then Resume. After a successful update, use Resume. Interrupted or failed operations remain blocked; do not delete recovery files manually.'))
		]);
	},
	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
