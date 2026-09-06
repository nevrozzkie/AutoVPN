'use strict';

let http = require('autovpn.http');

function assertEqual(actual, expected, label) {
	if (actual != expected)
		die(label + ': expected ' + sprintf('%J', expected) + ', got ' + sprintf('%J', actual) + '\n');
}

assertEqual(http.normalizeBaseUrl('https://vpn.example/root/'), 'https://vpn.example/root', 'valid URL');
assertEqual(http.normalizeBaseUrl('https://vpn.example:8443/root/'), 'https://vpn.example:8443/root', 'DNS URL with port');
assertEqual(http.normalizeBaseUrl('https://[2001:db8::1]:65535/root/'), 'https://[2001:db8::1]:65535/root', 'IPv6 URL with port');
assertEqual(http.normalizeBaseUrl('https://vpn.example:0'), null, 'zero port');
assertEqual(http.normalizeBaseUrl('https://[2001:db8::1]:65536'), null, 'port above maximum');
assertEqual(http.normalizeBaseUrl('https://vpn.example\x00.evil'), null, 'NUL in authority');
assertEqual(http.normalizeBaseUrl('https://vpn.example/root\x00evil'), null, 'NUL in path');
assertEqual(http.normalizeBaseUrl('https://vpn.example/root\x01evil'), null, 'control byte in path');

print('autovpn.http native URL validation OK\n');
