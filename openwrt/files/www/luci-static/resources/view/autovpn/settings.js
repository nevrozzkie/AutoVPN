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
		], cidrs: ['149.154.160.0/20', '91.108.4.0/22'],
		status: _('Access is restricted on many Russian networks, but behavior differs by provider.')
	},
	{
		name: 'negative_youtube', label: _('YouTube'), domains: [
			'youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
			'ytimg.com', 'youtubei.googleapis.com', 'youtube.googleapis.com', 'yt3.ggpht.com'
		], status: _('Access is heavily throttled or unavailable on many Russian networks.')
	},
	{
		name: 'negative_instagram', label: _('Instagram'), domains: [
			'instagram.com', 'cdninstagram.com', 'i.instagram.com', 'graph.instagram.com',
			'api.instagram.com', 'l.instagram.com'
		], status: _('Access is blocked on Russian networks.')
	},
	{
		name: 'negative_x', label: _('X (Twitter)'), domains: [
			'x.com', 'twitter.com', 't.co', 'twimg.com', 'api.x.com', 'api.twitter.com'
		], status: _('Access is blocked on Russian networks.')
	},
	{
		name: 'negative_chatgpt', label: _('ChatGPT and OpenAI'), domains: [
			'chatgpt.com', 'chat.openai.com', 'openai.com', 'auth.openai.com',
			'platform.openai.com', 'oaistatic.com', 'oaiusercontent.com'
		], status: _('Unavailable by the service for many Russian accounts; this is not an RKN block.')
	},
	{
		name: 'negative_claude', label: _('Claude and Anthropic'), domains: [
			'claude.ai', 'anthropic.com', 'console.anthropic.com', 'api.anthropic.com',
			'anthropic-static.com'
		], status: _('Unavailable by the service for many Russian accounts; this is not an RKN block.')
	},
	{
		name: 'negative_discord', label: _('Discord'), domains: [
			'discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net', 'discord.media',
			'discordstatus.com'
		], status: _('Access is blocked on Russian networks; this preset covers the website and Discord media domains.')
	},
	{
		name: 'negative_facebook', label: _('Facebook and Messenger'),
		domains: ['facebook.com', 'fb.com', 'fb.me', 'fbcdn.net', 'messenger.com'],
		status: _('Access is blocked on Russian networks.')
	},
	{
		name: 'negative_linkedin', label: _('LinkedIn'), domains: ['linkedin.com', 'licdn.com'],
		status: _('Access has long been restricted in Russia.')
	},
	{
		name: 'negative_reddit', label: _('Reddit (optional)'),
		domains: ['reddit.com', 'redd.it', 'redditmedia.com', 'redditstatic.com'],
		status: _('No nationwide block is currently confirmed; enable only if your provider has access problems.')
	},
	{
		name: 'negative_tiktok', label: _('TikTok'), domains: ['tiktok.com', 'tiktokv.com', 'tiktokcdn.com'],
		status: _('Availability and publishing restrictions can differ from a network block.')
	},
	{
		name: 'negative_soundcloud', label: _('SoundCloud'), domains: ['soundcloud.com'],
		status: _('Optional service route preset; availability can depend on provider and content.')
	},
	{
		name: 'negative_clubhouse', label: _('Clubhouse'), domains: ['clubhouse.com'],
		status: _('Optional service route preset.')
	},
	{
		name: 'negative_patreon', label: _('Patreon'), domains: ['patreon.com'],
		status: _('Optional service route preset; availability can depend on provider and content.')
	},
	{
		name: 'negative_signal', label: _('Signal'),
		domains: ['signal.org', 'signal.art', 'signal.me', 'whispersystems.org'],
		status: _('Access is restricted on Russian networks.')
	},
	{
		name: 'negative_twitch', label: _('Twitch (optional)'),
		domains: ['twitch.tv', 'ttvnw.net', 'jtvnw.net'],
		status: _('No nationwide block is currently confirmed; enable only if streams fail on your provider.')
	},
	{
		name: 'negative_viber', label: _('Viber'), domains: ['viber.com', 'viber.me', 'viber.net'],
		status: _('Access is blocked on Russian networks.')
	},
	{
		name: 'negative_whatsapp', label: _('WhatsApp'), domains: ['whatsapp.com', 'whatsapp.net', 'wa.me'],
		status: _('Calls and access can be restricted; behavior differs by provider.')
	},
	{
		name: 'negative_bluesky', label: _('Bluesky'), domains: ['bsky.app', 'bsky.social'],
		status: _('Measurements show ongoing blocking on many Russian networks.')
	}
];

function negativePresetDescription(preset) {
	var cidrs = preset.cidrs ? _(' Supplemental IPv4 ranges: %s; Telegram can change them.').format(preset.cidrs.join(', ')) : '';
	return _('%s Locally adds these domain suffixes to the negative VPN network: %s.%s The list is fixed in this controller release and is not a live blocking register.').format(preset.status || '', preset.domains.join(', '), cidrs);
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
		section.description = _('Select services individually. Restrictions change over time and differ by provider; Twitch and Reddit are deliberately separate optional presets and are not treated as nationally blocked.');
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
