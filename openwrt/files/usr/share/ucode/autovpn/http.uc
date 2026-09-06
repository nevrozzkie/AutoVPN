'use strict';

const MAX_BODY_BYTES = 16384;
const MAX_HEADER_BYTES = 16384;
const MAX_DIAGNOSTICS_BYTES = 8192;
const MAX_JOURNAL_BYTES = 262144;
const CAPABILITIES = ['vless', 'hysteria2', 'amneziawg', 'zapret', 'policy_routing'];

function isObject(value) {
	return type(value) == 'object';
}

function exactKeys(value, expected) {
	if (!isObject(value))
		return false;
	let actual = sort(keys(value));
	let wanted = sort(expected);
	if (length(actual) != length(wanted))
		return false;
	for (let i = 0; i < length(wanted); i++)
		if (actual[i] != wanted[i])
			return false;
	return true;
}

function strongEtag(value) {
	return type(value) == 'string' && match(value, /^"[0-9a-f]{64}"$/) != null;
}

function idempotencyKey(value) {
	return type(value) == 'string' && match(value, /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/) != null;
}

function boundedTimeout(value, minimum, maximum) {
	if (type(value) == 'int')
		return value >= minimum && value <= maximum ? value : null;
	if (type(value) != 'string' || match(value, /^[0-9]+$/) == null)
		return null;
	let parsed = int(value);
	return parsed >= minimum && parsed <= maximum ? parsed : null;
}

function normalizeBaseUrl(value) {
	if (type(value) != 'string' || length(value) < 9 || length(value) > 2048 ||
		index(value, '\x00') >= 0 || match(value, /^https:\/\//) == null ||
		match(value, /[?#@\\\x01-\x20\x7f]/) != null)
		return null;

	let rest = substr(value, 8);
	let slash = index(rest, '/');
	let authority = slash < 0 ? rest : substr(rest, 0, slash);
	let path = slash < 0 ? '' : substr(rest, slash);
	if (!length(authority) || length(authority) > 320 ||
		!(path == '' || match(path, /^\/[A-Za-z0-9._~!$&'()*+,;=:\/-]*$/) != null))
		return null;
	while (length(path) > 0 && substr(path, length(path) - 1) == '/')
		path = substr(path, 0, length(path) - 1);
	if (match(path, /(^|\/)\.{1,2}(\/|$)/) != null || match(path, /\/\//) != null)
		return null;

	let port = null;
	if (substr(authority, 0, 1) == '[') {
		let ipv6 = match(authority, /^\[([0-9A-Fa-f:.]+)\](:([0-9]+))?$/);
		if (ipv6 == null || index(ipv6[1], ':') < 0)
			return null;
		port = ipv6[3];
	}
	else {
		let host = match(authority, /^([A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?)(:([0-9]+))?$/);
		if (host == null || length(host[1]) > 253 || match(host[1], /\.\./) != null)
			return null;
		let labels = split(host[1], '.');
		for (let i = 0; i < length(labels); i++)
			if (length(labels[i]) > 63 || match(labels[i], /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$/) == null)
				return null;
		port = host[4];
	}
	if (port != null && boundedTimeout(port, 1, 65535) == null)
		return null;
	return 'https://' + authority + path;
}

function endpoint(baseUrl, operation, key) {
	let base = normalizeBaseUrl(baseUrl);
	if (base == null)
		return null;
	if (operation == 'snapshot')
		return base + '/api/v2/router/snapshot/dual';
	if (operation == 'apply-result' && idempotencyKey(key))
		return base + '/api/v2/router/apply-results/' + key;
	return null;
}

function credential(raw) {
	if (type(raw) != 'string')
		return null;
	let token = match(raw, /^(avrt_[A-Za-z0-9_-]{8,64}\.[A-Za-z0-9_-]{43,128})\n?$/);
	return token == null ? null : token[1];
}

function responseHandoff(stateDir, value) {
	if (type(stateDir) != 'string' || type(value) != 'string' ||
		match(stateDir, /^\/etc\/autovpn\/[A-Za-z0-9_.-]+$/) == null)
		return false;
	let prefix = stateDir + '/http-response.';
	return substr(value, 0, length(prefix)) == prefix &&
		match(substr(value, length(prefix)), /^[0-9]+$/) != null;
}

function quoteConfig(value) {
	return '"' + replace(replace(value, /\\/g, '\\\\'), /"/g, '\\"') + '"';
}

function baseCurlConfig(url, token, connectTimeout, requestTimeout, headersPath, responsePath) {
	return [
		'silent',
		'no-progress-meter',
		'no-location',
		'no-insecure',
		'proto = "=https"',
		'proto-redir = "=https"',
		'noproxy = "*"',
		'cacert = "/etc/ssl/certs/ca-certificates.crt"',
		'connect-timeout = ' + connectTimeout,
		'max-time = ' + requestTimeout,
		'max-filesize = ' + MAX_BODY_BYTES,
		'dump-header = ' + quoteConfig(headersPath),
		'output = ' + quoteConfig(responsePath),
		'write-out = "%{http_code}"',
		'url = ' + quoteConfig(url),
		'header = ' + quoteConfig('Authorization: Bearer ' + token),
	];
}

function headerEtag(raw) {
	if (type(raw) != 'string' || length(raw) > MAX_HEADER_BYTES)
		return null;
	let found = null;
	let lines = split(raw, '\n');
	for (let i = 0; i < length(lines); i++) {
		let candidate = match(lines[i], /^[Ee][Tt][Aa][Gg]:[ \t]*("[0-9a-f]{64}")[ \t]*\r?$/);
		if (candidate != null) {
			if (found != null)
				return null;
			found = candidate[1];
		}
	}
	return found;
}

function safeStatus(status) {
	let allowed = ['400', '401', '403', '404', '409', '412', '413', '415', '428', '429', '500', '502', '503', '504'];
	for (let i = 0; i < length(allowed); i++)
		if (status == allowed[i])
			return 'http_' + status;
	return 'unexpected_http_status';
}

function diagnosticValue(value, depth, budget) {
	if (depth > 5 || budget[0] >= 128)
		return false;
	budget[0]++;
	if (value == null || type(value) == 'bool' || type(value) == 'int' || type(value) == 'double')
		return true;
	if (type(value) == 'string')
		return length(value) <= 500 && match(value, /^[\x20-\x7e]*$/) != null &&
			index(value, '<') < 0 && index(value, '>') < 0 &&
			match(lc(value), /(authorization:|bearer |password=|private key|presharedkey|avrt_|vless:\/\/|hy2:\/\/|vpn:\/\/)/) == null &&
			match(value, /^[A-Za-z0-9_+\/=-]{32,256}$/) == null &&
			match(value, /^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$/) == null;
	if (type(value) == 'array') {
		if (length(value) > 32)
			return false;
		for (let i = 0; i < length(value); i++)
			if (!diagnosticValue(value[i], depth + 1, budget))
				return false;
		return true;
	}
	if (isObject(value)) {
		if (length(keys(value)) > 32)
			return false;
		for (let key in value) {
			let normalized = replace(lc(key), /-/g, '_');
			let compact = replace(normalized, /_/g, '');
			if (length(key) < 1 || length(key) > 64 || match(key, /^[\x20-\x7e]+$/) == null ||
				index(key, '<') >= 0 || index(key, '>') >= 0 ||
				match(normalized, /(authorization|cookie|password|privatekey|private_key|preshared|secret|token)/) != null ||
				match(compact, /(authorization|cookie|password|privatekey|preshared|secret|token)/) != null ||
				!diagnosticValue(value[key], depth + 1, budget))
				return false;
		}
		return true;
	}
	return false;
}

function applyBody(value) {
	if (!exactKeys(value, ['schema_version', 'revision', 'snapshot_sha256', 'outcome', 'active_profile', 'capabilities', 'diagnostics']) ||
		value.schema_version != 1 || type(value.revision) != 'int' || value.revision < 0 ||
		type(value.snapshot_sha256) != 'string' || match(value.snapshot_sha256, /^[0-9a-f]{64}$/) == null ||
		type(value.outcome) != 'string' || match(value.outcome, /^(APPLIED|DEGRADED|FAILED)$/) == null ||
		!(value.active_profile == null || (type(value.active_profile) == 'string' && match(value.active_profile, /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$/) != null)) ||
		!exactKeys(value.capabilities, CAPABILITIES) || !isObject(value.diagnostics))
		return false;
	for (let i = 0; i < length(CAPABILITIES); i++)
		if (type(value.capabilities[CAPABILITIES[i]]) != 'bool')
			return false;
	if (!diagnosticValue(value.diagnostics, 0, [0]))
		return false;
	return length(sprintf('%J', value.diagnostics)) <= MAX_DIAGNOSTICS_BYTES;
}

function parseObject(raw, maximum) {
	if (type(raw) != 'string' || length(raw) < 2 || length(raw) > maximum)
		return null;
	try {
		let value = json(raw);
		return isObject(value) ? value : null;
	}
	catch (e) {
		return null;
	}
}

function prepareFetch(args, io) {
	let url = endpoint(args[1], 'snapshot');
	let token = credential(io.read(args[2], 257));
	let etag = args[4];
	let connectTimeout = boundedTimeout(args[5], 1, 60);
	let requestTimeout = boundedTimeout(args[6], 2, 120);
	if (url == null || token == null || !(etag == '' || strongEtag(etag)) ||
		connectTimeout == null || requestTimeout == null || requestTimeout < connectTimeout ||
		!responseHandoff(args[3], args[10]))
		return { ok: false, code: 'invalid_request' };
	let config = baseCurlConfig(url, token, connectTimeout, requestTimeout, args[8], args[9]);
	push(config, 'request = "GET"');
	push(config, 'header = "Accept: application/json"');
	if (length(etag))
		push(config, 'header = ' + quoteConfig('If-None-Match: ' + etag));
	return io.writePrivate(args[7], join('\n', config) + '\n')
		? { ok: true }
		: { ok: false, code: 'temporary_file_failed' };
}

function finishFetch(args, io) {
	let headers = io.read(args[2], MAX_HEADER_BYTES + 1);
	if (type(headers) != 'string' || length(headers) > MAX_HEADER_BYTES)
		return { ok: false, code: 'response_headers_too_large' };
	let etag = headerEtag(headers);
	if (args[1] == '304') {
		let body304 = io.read(args[3], 1);
		return strongEtag(args[5]) && etag == args[5] && type(body304) == 'string' && length(body304) == 0
			? { ok: true, status: 304, etag: etag }
			: { ok: false, code: 'invalid_304_response' };
	}
	if (args[1] != '200')
		return { ok: false, code: safeStatus(args[1]) };
	let body = io.read(args[3], MAX_BODY_BYTES + 1);
	if (etag == null)
		return { ok: false, code: 'invalid_response_etag' };
	if (parseObject(body, MAX_BODY_BYTES) == null)
		return { ok: false, code: length(body || '') > MAX_BODY_BYTES ? 'response_too_large' : 'invalid_response_json' };
	if (!io.writePrivate(args[4], body))
		return { ok: false, code: 'response_file_failed' };
	return { ok: true, status: 200, etag: etag, response_file: args[4] };
}

function preparePut(args, io) {
	let url = endpoint(args[1], 'apply-result', args[3]);
	let token = credential(io.read(args[2], 257));
	let connectTimeout = boundedTimeout(args[6], 1, 60);
	let requestTimeout = boundedTimeout(args[7], 2, 120);
	let journal = parseObject(io.read(args[5], MAX_JOURNAL_BYTES + 1), MAX_JOURNAL_BYTES);
	let pending = journal == null ? null : journal.pending_report;
	if (url == null || token == null || !strongEtag(args[4]) || connectTimeout == null ||
		requestTimeout == null || requestTimeout < connectTimeout || !isObject(pending) ||
		!exactKeys(pending, ['idempotency_key', 'etag', 'body']) || pending.idempotency_key != args[3] ||
		pending.etag != args[4] || !applyBody(pending.body))
		return { ok: false, code: 'invalid_request' };
	let body = sprintf('%J', pending.body);
	if (length(body) > MAX_BODY_BYTES || !io.writePrivate(args[11], body))
		return { ok: false, code: 'request_body_failed' };
	let config = baseCurlConfig(url, token, connectTimeout, requestTimeout, args[9], args[10]);
	push(config, 'request = "PUT"');
	push(config, 'header = "Content-Type: application/json"');
	push(config, 'header = ' + quoteConfig('If-Match: ' + args[4]));
	push(config, 'data-binary = ' + quoteConfig('@' + args[11]));
	return io.writePrivate(args[8], join('\n', config) + '\n')
		? { ok: true }
		: { ok: false, code: 'temporary_file_failed' };
}

function finishPut(args, io) {
	if (args[1] != '200')
		return { ok: false, code: safeStatus(args[1]) };
	let headers = io.read(args[2], MAX_HEADER_BYTES + 1);
	if (type(headers) != 'string' || length(headers) > MAX_HEADER_BYTES)
		return { ok: false, code: 'response_headers_too_large' };
	let etag = headerEtag(headers);
	let response = parseObject(io.read(args[3], MAX_BODY_BYTES + 1), MAX_BODY_BYTES);
	let request = parseObject(io.read(args[4], MAX_BODY_BYTES + 1), MAX_BODY_BYTES);
	if (etag == null || etag != args[6])
		return { ok: false, code: 'invalid_response_etag' };
	if (response == null || request == null || !exactKeys(response, [
		'schema_version', 'result_id', 'idempotency_key', 'revision', 'snapshot_sha256',
		'outcome', 'active_profile', 'accepted_at', 'replayed',
	]) || response.schema_version != 1 || type(response.result_id) != 'int' || response.result_id < 1 ||
		response.idempotency_key != args[5] || response.revision != request.revision ||
		response.snapshot_sha256 != request.snapshot_sha256 || response.outcome != request.outcome ||
		response.active_profile != request.active_profile || type(response.accepted_at) != 'string' ||
		length(response.accepted_at) < 1 || length(response.accepted_at) > 64 || type(response.replayed) != 'bool')
		return { ok: false, code: 'invalid_apply_response' };
	return { ok: true, status: 200, replayed: response.replayed };
}

function run(args, io) {
	if (type(args) != 'array' || !isObject(io))
		return { ok: false, code: 'invalid_request' };
	if (args[0] == 'prepare-fetch' && length(args) == 11)
		return prepareFetch(args, io);
	if (args[0] == 'finish-fetch' && length(args) == 6)
		return finishFetch(args, io);
	if (args[0] == 'prepare-put' && length(args) == 12)
		return preparePut(args, io);
	if (args[0] == 'finish-put' && length(args) == 7)
		return finishPut(args, io);
	return { ok: false, code: 'invalid_request' };
}

return {
	MAX_BODY_BYTES: MAX_BODY_BYTES,
	MAX_HEADER_BYTES: MAX_HEADER_BYTES,
	MAX_JOURNAL_BYTES: MAX_JOURNAL_BYTES,
	strongEtag: strongEtag,
	idempotencyKey: idempotencyKey,
	normalizeBaseUrl: normalizeBaseUrl,
	endpoint: endpoint,
	applyBody: applyBody,
	headerEtag: headerEtag,
	run: run,
};
