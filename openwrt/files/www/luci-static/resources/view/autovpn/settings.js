'use strict';
'require view';
'require form';
'require uci';
'require rpc';

/* Fixed local presets for the negative VPN lane. Keep this list byte-for-byte
 * aligned with /usr/share/ucode/autovpn/negative_presets.uc. */
var NEGATIVE_PRESETS = [
	{
		name: 'negative_telegram', label: _('Telegram and Telegram API'), domains: [
			't.me', 'telegram.org', 'telegram.me', 'telegram.dog', 'tdesktop.com',
			'telegram-cdn.org', 'api.telegram.org', 'core.telegram.org',
			'web.telegram.org', 'desktop.telegram.org', 'updates.tdesktop.com'
		], cidrs: ['149.154.160.0/20', '91.108.4.0/22']
	},
	{
		name: 'negative_youtube', label: _('YouTube'), domains: [
			'youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
			'ytimg.com', 'youtubei.googleapis.com', 'youtube.googleapis.com', 'yt3.ggpht.com'
		]
	},
	{
		name: 'negative_instagram', label: _('Instagram'), domains: [
			'instagram.com', 'cdninstagram.com', 'i.instagram.com', 'graph.instagram.com',
			'api.instagram.com', 'l.instagram.com'
		]
	},
	{
		name: 'negative_x', label: _('X (Twitter)'), domains: [
			'x.com', 'twitter.com', 't.co', 'twimg.com', 'api.x.com', 'api.twitter.com'
		]
	},
	{
		name: 'negative_chatgpt', label: _('ChatGPT and OpenAI'), domains: [
			'chatgpt.com', 'chat.openai.com', 'openai.com', 'auth.openai.com',
			'platform.openai.com', 'oaistatic.com', 'oaiusercontent.com'
		]
	},
	{
		name: 'negative_claude', label: _('Claude and Anthropic'), domains: [
			'claude.ai', 'anthropic.com', 'console.anthropic.com', 'api.anthropic.com',
			'anthropic-static.com'
		]
	},
	{
		name: 'negative_extended_blocked_services', label: _('Expanded maintained list (not every blocked website)'), domains: [
			'discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net', 'discord.media',
			'discordstatus.com', 'facebook.com', 'fb.com', 'fb.me', 'fbcdn.net', 'messenger.com',
			'linkedin.com', 'licdn.com', 'reddit.com', 'redd.it', 'redditmedia.com', 'redditstatic.com',
			'tiktok.com', 'tiktokv.com', 'tiktokcdn.com', 'soundcloud.com', 'clubhouse.com',
			'patreon.com', 'signal.org', 'signal.art', 'signal.me', 'whispersystems.org',
			'twitch.tv', 'ttvnw.net', 'jtvnw.net', 'viber.com', 'viber.me'
		]
	}
];

function negativePresetDescription(preset) {
	var cidrs = preset.cidrs ? _(' Supplemental IPv4 ranges: %s; Telegram can change them.').format(preset.cidrs.join(', ')) : '';
	return _('Locally adds these domain suffixes to the negative VPN network: %s.%s This is a fixed curated list, not a complete or current register of all websites blocked in Russia.').format(preset.domains.join(', '), cidrs);
}

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
		option.description = _('Applies the downloaded community SagerNet Russian domain and IPv4 database to the VPN network. It is refreshed daily, is not guaranteed to cover every Russian site, and GeoIP entries can include foreign co-hosted services. Your manual domain and IPv4 rules are kept; disabling this excludes only the downloaded database, not manual rules.');
		section = map.section(form.NamedSection, 'runtime_negative', 'runtime', _('Negative VPN network'));
		section.addremove = false;
		option = section.option(form.DynamicList, 'vpn_cidrs', _('IPv4 networks through VPN'));
		option.description = _('CIDR notation, for example 203.0.113.0/24. Selected destinations use the same active VPN as Wi-Fi -в; all other IPv4 destinations are direct.');
		option = section.option(form.DynamicList, 'vpn_domains', _('Domains through VPN'));
		option.description = _('Domain suffixes, one per entry, without URLs or wildcards. Use punycode for Cyrillic names. Domain matching uses DNS and protocol sniffing; manually entered IPv4 CIDRs are exact. If the shared VPN is unavailable, this network is closed to prevent selected traffic leaking directly.');
		for (var presetIndex = 0; presetIndex < NEGATIVE_PRESETS.length; presetIndex++) {
			var preset = NEGATIVE_PRESETS[presetIndex];
			option = section.option(form.Flag, preset.name, preset.label);
			option.default = '0';
			option.rmempty = false;
			option.description = negativePresetDescription(preset);
		}
		section = map.section(form.NamedSection, 'wifi', 'wifi', _('Managed Wi-Fi networks'));
		section.addremove = false;
		option = section.option(form.Value, 'base_ssid', _('Base Wi-Fi name'));
		option.description = _('Choose the name without suffixes, up to 27 UTF-8 bytes (Cyrillic uses more than one byte per letter). The managed names are the base SSID, -в for VPN, and -нв for negative VPN. The same SSID is used on both enabled bands, so clients choose a radio automatically. Save and apply, then open Networks to create the SSIDs.');
		option = section.option(form.Value, 'password', _('WPA2-PSK password'));
		option.password = true;
		option.datatype = 'wpakey';
		option.description = _('Used by all managed SSIDs: 8–63 printable ASCII characters, or a 64-digit hexadecimal PSK. No default password. Existing management Wi-Fi is not changed.');
		return map.render();
	}
});
