'use strict';
'require view';
'require form';
'require uci';
'require rpc';
'require ui';

var installZapret = rpc.declare({ object: 'luci.autovpn', method: 'zapret_install', expect: { '': {} } });
var zapretStatus = rpc.declare({ object: 'luci.autovpn', method: 'zapret_install_status', expect: { '': {} } });

return view.extend({
	load: function() { return uci.load('autovpn'); },
	render: function() {
		var map = new form.Map('autovpn', _('AutoVPN settings'),
			_('Save the settings, then use Apply saved VPN settings on the Overview page. The previous working configuration is restored if the VPN check fails.'));
		var section = map.section(form.NamedSection, 'main', 'controller', _('Subscription'));
		section.addremove = false;
		var option = section.option(form.Flag, 'enabled', _('Automatic refresh'));
		option.rmempty = false;
		option.description = _('Subscription refresh interval. Tunnel health is checked separately every 30 seconds.');
		option = section.option(form.Value, 'base_url', _('AutoVPN server URL'));
		option.placeholder = 'https://vpn.example';
		option = section.option(form.Value, 'router_id', _('Router ID'));
		option.description = _('Use the Router ID shown when you create this device in AutoVPN.');
		section = map.section(form.NamedSection, 'runtime', 'runtime', _('VPN and direct exceptions'));
		section.addremove = false;
		option = section.option(form.ListValue, 'selection', _('VPN'));
		option.value('auto', _('Automatic (failover only)'));
		option.description = _('Keeps the current VPN until three health-check rounds fail. Never switches for a lower latency or switches back just because a previous VPN recovered. Use Ping all to compare and select a VPN manually.');
		option.value('vless-reality', 'VLESS REALITY');
		option.value('hysteria2', 'Hysteria2');
		option.value('amneziawg', 'AmneziaWG');
		option.default = 'auto';
		var primaryRuntime = section;
		section = map.section(form.NamedSection, 'runtime_zapret', 'runtime', _('VPN + zapret'));
		section.addremove = false;
		option = section.option(form.ListValue, 'selection', _('VPN + zapret'));
		option.value('auto', _('Automatic (failover only)'));
		option.value('vless-reality', 'VLESS REALITY');
		option.value('hysteria2', 'Hysteria2');
		option.value('amneziawg', 'AmneziaWG');
		option.default = 'auto';
		option.description = _('Independent selection for the VPN + zapret network.');
		section = primaryRuntime;
		option = section.option(form.ListValue, 'hysteria_tls_mode', _('Hysteria2 certificate verification'));
		option.value('subscription', _('Use subscription setting'));
		option.value('strict', _('Require a trusted certificate'));
		option.default = 'subscription';
		option.rmempty = false;
		option.description = _('Subscription mode accepts insecure=true from your server. Encryption remains enabled, but the VPN server certificate may not be verified.');
		option = section.option(form.Value, 'wan_device', _('WAN device'));
		option.description = _('Linux interface used by the internet connection, for example pppoe-wan or eth1.');
		option.rmempty = false;
		option = section.option(form.Value, 'dns_server', _('DNS server through VPN'));
		option.datatype = 'ip4addr';
		option.default = '1.1.1.1';
		option.rmempty = false;
		option = section.option(form.DynamicList, 'direct_domains', _('Domains without VPN'));
		option.description = _('Domain suffixes, one per entry, without URLs or wildcards. Use punycode for Cyrillic names. The ru suffix does not include every Russian website.');
		option = section.option(form.DynamicList, 'direct_cidrs', _('IPv4 networks without VPN'));
		option.description = _('CIDR notation, for example 203.0.113.0/24.');
		option = section.option(form.Flag, 'ru_bypass', _('Automatic Russian bypass'));
		option.default = '1';
		option.rmempty = false;
		option.description = _('Applies the downloaded community SagerNet Russian domain and IPv4 database to both VPN networks. It is refreshed daily, is not guaranteed to cover every Russian site, and GeoIP entries can include foreign co-hosted services. Your manual domain and IPv4 rules are kept; disabling this excludes only the downloaded database, not manual rules.');
		option = section.option(form.Button, '_install_zapret', _('Zapret2 engine'));
		option.inputtitle = _('Install / check installation');
		option.inputstyle = 'action';
		option.description = _('Downloads a pinned official bol-van/zapret2 release with SHA-256 verification. Does not enable zapret or change your VPN. No automatic engine updates.');
		option.onclick = function() {
			return installZapret().then(function(result) {
				if (!result.ok) throw new Error(result.code || 'installation_failed');
				var attempts = 0;
				function check() {
					return zapretStatus().then(function(state) {
						if ((state.phase === 'queued' || state.phase === 'running') && attempts++ < 100)
							return new Promise(function(resolve) { window.setTimeout(resolve, 2000); }).then(check);
						ui.addNotification(null, E('p', {}, _('Zapret installation: %s').format(state.code || state.phase || 'unknown')));
					});
				}
				return check();
			}).catch(function(error) {
				ui.addNotification(null, E('p', {}, _('Zapret installation failed: %s').format(error.message)));
			});
		};
		option = section.option(form.Flag, 'zapret_enabled', _('Zapret on VPN transport'));
		option.rmempty = false;
		option.description = _('Processes only AutoVPN connections to VPN servers, before the encrypted tunnel reaches the provider. Applies only to Wi-Fi -вз and its candidate probes; it does not process Wi-Fi -в.');
		option = section.option(form.ListValue, 'zapret_vless', _('VLESS zapret strategy'));
		option.depends('zapret_enabled', '1');
		option.value('split', _('TLS ClientHello split (experimental)'));
		option.value('off', _('Off'));
		option.default = 'split';
		['hysteria2', 'amneziawg'].forEach(function(protocol) {
			var strategy = section.option(form.ListValue, 'zapret_' + protocol, protocol + ' zapret');
			strategy.depends('zapret_enabled', '1');
			strategy.value('off', _('Off'));
			strategy.value('fake', _('UDP fake packets (experimental, provider-dependent)'));
			strategy.default = protocol === 'hysteria2' ? 'fake' : 'off';
		});
		option = section.option(form.Value, 'zapret_repeats', _('UDP fake repeats'));
		option.depends('zapret_enabled', '1');
		option.datatype = 'range(1,6)';
		option.default = '2';
		option.description = _('No automatic strategy selection. Save and apply the VPN settings, then check Ping all. A disabled strategy leaves that VPN candidate unmodified.');
		section = map.section(form.NamedSection, 'direct', 'policy', _('Direct zapret'));
		section.addremove = false;
		option = section.option(form.Flag, 'zapret_enabled', _('Enable zapret for Wi-Fi -з'));
		option.rmempty = false;
		option.default = '1';
		option.description = _('Enables zapret processing for Wi-Fi -з. When disabled, Wi-Fi -з remains closed; it never becomes an unprocessed direct bypass.');
		option = section.option(form.ListValue, 'web_strategy', _('Web / YouTube strategy'));
		option.value('upstream', _('Upstream zapret2 web preset'));
		option.value('legacy-split', _('Legacy TLS split + QUIC fake'));
		option.value('legacy-split-badsum', _('Legacy split + experimental bad checksum'));
		option.default = 'upstream';
		option.rmempty = false;
		option.description = _('Applies to TCP 80/443 and IETF QUIC Initial packets on Wi-Fi -з. The default follows the bounded upstream zapret2 web recipe; legacy variants remain available for provider-specific testing.');
		option = section.option(form.Flag, 'discord_media', _('Discord media discovery'));
		option.rmempty = false;
		option.default = '0';
		option.description = _('Applies only to Wi-Fi -з. New installations enable it; an absent option in an older configuration stays off for compatibility. It does not change VPN or VPN + zapret.');
		option = section.option(form.Flag, 'stun', _('WebRTC STUN'));
		option.rmempty = false;
		option.default = '0';
		option.description = _('Applies only to Wi-Fi -з and can affect WebRTC and other applications that use STUN. New installations enable it; an absent option in an older configuration stays off.');
		option = section.option(form.ListValue, 'media_strategy', _('Discord / STUN strategy'));
		option.value('fake', _('Fake packets (upstream zero payload)'));
		option.value('fake_badsum', _('Fake packets with experimental bad checksum'));
		option.default = 'fake';
		option.rmempty = false;
		option.description = _('Applies only to Wi-Fi -з. No custom command or arbitrary zapret profile can be entered here.');
		option = section.option(form.Value, 'media_repeats', _('Discord / STUN fake repeats'));
		option.datatype = 'and(uinteger,range(1,6))';
		option.default = '2';
		option.rmempty = false;
		option.description = _('Applies only to Wi-Fi -з. The value is provider-dependent and does not change existing VPN zapret settings.');
		section = map.section(form.NamedSection, 'wifi', 'wifi', _('Managed Wi-Fi networks'));
		section.addremove = false;
		option = section.option(form.Value, 'base_ssid', _('Base Wi-Fi name'));
		option.description = _('Choose the name without suffixes, up to 27 UTF-8 bytes (Cyrillic uses more than one byte per letter). Managed names use -в, -з and -вз. The same SSID is used on both enabled bands, so clients choose a radio automatically. Save and apply, then open Networks to create the SSIDs.');
		option = section.option(form.Value, 'password', _('WPA2-PSK password'));
		option.password = true;
		option.datatype = 'wpakey';
		option.description = _('Used by all managed SSIDs: 8–63 printable ASCII characters, or a 64-digit hexadecimal PSK. No default password. Existing management Wi-Fi is not changed.');
		return map.render();
	}
});
