'use strict';

const PROFILES = ['vless-reality', 'hysteria2', 'amneziawg'];
const TARGETS = {
	youtube: 'https://www.youtube.com/generate_204',
	instagram: 'https://www.instagram.com/',
	health_google: 'https://www.gstatic.com/generate_204',
	health_cloudflare: 'https://cp.cloudflare.com/generate_204',
};

function argumentsFor(profile, target) {
	let offset = index(PROFILES, profile);
	if (offset < 0 || TARGETS[target] == null) return null;
	return ['/usr/bin/curl', '--disable', '--silent', '--head', '--output', '/dev/null',
		'--write-out', '%{http_code} %{time_starttransfer}', '--proxy',
		'socks5h://127.0.0.1:' + (1089 + offset), '--noproxy', '', '--proto', '=https',
		'--connect-timeout', '3', '--max-time', '5', '--max-redirs', '0', TARGETS[target]];
}

function parseResult(profile, target, raw, exitCode) {
	let result = { profile: profile, status: 'failed', latency_ms: null,
		http_status: null, code: 'connection_failed' };
	if (exitCode != 0) {
		result.code = exitCode == 28 ? 'timeout' : 'connection_failed';
		return result;
	}
	if (type(raw) != 'string' || match(raw, /^[0-9]{3} [0-9]{1,2}\.[0-9]{1,6}$/) == null) {
		result.code = 'invalid_probe_response';
		return result;
	}
	let fields = split(raw, ' ');
	let status = int(fields[0]);
	let elapsed = json(fields[1]);
	if (status < 100 || status > 599 || elapsed < 0 || elapsed > 6) {
		result.code = 'invalid_probe_response';
		return result;
	}
	result.http_status = status;
	result.latency_ms = int(elapsed * 1000);
	let success = target == 'instagram' ? status >= 200 && status < 400 : status == 204;
	result.status = success ? 'ok' : 'http_error';
	result.code = success ? null : 'unexpected_http_status';
	return result;
}

/* Latency is deliberately absent from the failover decision. */
function observe(previous, identity, success) {
	let failures = previous?.identity == identity && type(previous.failures) == 'int' &&
		previous.failures >= 0 && previous.failures <= 3 ? previous.failures : 0;
	return { identity: identity, failures: success ? 0 : (failures < 3 ? failures + 1 : 3) };
}

return { profiles: PROFILES, argumentsFor: argumentsFor, parseResult: parseResult, observe: observe };
