'use strict';

/* Fixed local domain suffix presets for the negative VPN lane. The server
 * never supplies these names. Keep the LuCI descriptions in sync. */
const CATALOG = {
	negative_telegram: [
		't.me', 'telegram.org', 'telegram.me', 'telegram.dog', 'tdesktop.com',
		'telegram-cdn.org', 'api.telegram.org', 'core.telegram.org',
		'web.telegram.org', 'desktop.telegram.org', 'updates.tdesktop.com',
	],
	negative_youtube: [
		'youtube.com', 'youtu.be', 'youtube-nocookie.com', 'googlevideo.com',
		'ytimg.com', 'youtubei.googleapis.com', 'youtube.googleapis.com', 'yt3.ggpht.com',
	],
	negative_instagram: [
		'instagram.com', 'cdninstagram.com', 'i.instagram.com', 'graph.instagram.com',
		'api.instagram.com', 'l.instagram.com',
	],
	negative_x: [
		'x.com', 'twitter.com', 't.co', 'twimg.com', 'api.x.com', 'api.twitter.com',
	],
	negative_chatgpt: [
		'chatgpt.com', 'chat.openai.com', 'openai.com', 'auth.openai.com',
		'platform.openai.com', 'oaistatic.com', 'oaiusercontent.com',
	],
	negative_claude: [
		'claude.ai', 'anthropic.com', 'console.anthropic.com', 'api.anthropic.com',
		'anthropic-static.com',
	],
	/* This deliberately excludes the six individual presets above. */
	negative_extended_blocked_services: [
		'discord.com', 'discord.gg', 'discordapp.com', 'discordapp.net', 'discord.media',
		'discordstatus.com', 'facebook.com', 'fb.com', 'fb.me', 'fbcdn.net', 'messenger.com',
		'linkedin.com', 'licdn.com', 'reddit.com', 'redd.it', 'redditmedia.com', 'tiktok.com',
		'redditstatic.com', 'tiktokv.com', 'tiktokcdn.com', 'soundcloud.com', 'clubhouse.com',
		'patreon.com', 'signal.org', 'signal.art', 'signal.me', 'whispersystems.org',
		'twitch.tv', 'ttvnw.net', 'jtvnw.net', 'viber.com', 'viber.me',
	],
};
const CIDRS = {
	/* Supplemental Telegram ranges published by Telegram; they can change. */
	negative_telegram: ['149.154.160.0/20', '91.108.4.0/22'],
};

function copy(value) { return json(sprintf('%J', value)); }

function names() { return sort(keys(CATALOG)); }

function enabled(flags) {
	if (type(flags) != 'object') return [];
	let out = [];
	let seen = {};
	for (let i = 0; i < length(names()); i++) {
		let name = names()[i];
		if (flags[name] !== true) continue;
		for (let n = 0; n < length(CATALOG[name]); n++) {
			let domain = CATALOG[name][n];
			if (seen[domain] === true) continue;
			seen[domain] = true;
			push(out, domain);
		}
	}
	return out;
}

function enabledCidrs(flags) {
	if (type(flags) != 'object') return [];
	let out = [];
	for (let i = 0; i < length(names()); i++) {
		let name = names()[i];
		if (flags[name] !== true) continue;
		for (let n = 0; n < length(CIDRS[name] || []); n++) push(out, CIDRS[name][n]);
	}
	return out;
}

return { catalog: copy(CATALOG), cidrs: copy(CIDRS), names: names,
	enabled: enabled, enabledCidrs: enabledCidrs };
