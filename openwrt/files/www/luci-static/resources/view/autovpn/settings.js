'use strict';
'require view';
'require form';
'require uci';

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
		section = map.section(form.NamedSection, 'wifi', 'wifi', _('Managed Wi-Fi networks'));
		section.addremove = false;
		option = section.option(form.Value, 'base_ssid', _('Base Wi-Fi name'));
		option.description = _('Choose the name without suffixes, up to 21 UTF-8 bytes (Cyrillic uses more than one byte per letter). The same names are used on both enabled bands. Save and apply, then open Networks to create the SSIDs.');
		option = section.option(form.Value, 'password', _('WPA2-PSK password'));
		option.password = true;
		option.datatype = 'wpakey';
		option.description = _('Used by all managed SSIDs: 8–63 printable ASCII characters, or a 64-digit hexadecimal PSK. No default password. Existing management Wi-Fi is not changed.');
		return map.render();
	}
});
