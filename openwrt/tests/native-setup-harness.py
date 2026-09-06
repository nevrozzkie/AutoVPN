#!/usr/bin/env python3
"""Emit an isolated native-ucode setup-helper regression harness on stdout."""

from pathlib import Path
import sys


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
FILES = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "files"
HELPER = FILES / "usr/libexec/autovpn/setup-helper.uc"
POLICY = FILES / "usr/share/ucode/autovpn/setup_policy.uc"


def replace_once(source: str, old: str, new: str, label: str) -> str:
    if source.count(old) != 1:
        raise SystemExit(f"expected exactly one {label} in {HELPER}")
    return source.replace(old, new, 1)


helper = HELPER.read_text()
if helper.startswith("#!"):
    helper = helper.split("\n", 1)[1]
helper = replace_once(
    helper,
    "import { access, chmod, error as fsError, lstat, mkdir, readfile, rename, stat, unlink, writefile } from 'fs';",
    "",
    "fs import",
)
helper = replace_once(helper, "import { cursor } from 'uci';", "", "UCI import")
helper = replace_once(
    helper,
    "const processRunner = require('autovpn.process');",
    "const processRunner = fixtureProcessRunner;",
    "process dependency",
)

policy = POLICY.read_text().replace("'use strict';", "", 1)
helper = replace_once(
    helper,
    "const setupPolicy = require('autovpn.setup_policy');",
    f"const setupPolicy = (function() {{\n{policy}\n}})();",
    "setup policy dependency",
)

entry = helper.find("let action = ARGV[0];")
if entry < 0:
    raise SystemExit(f"setup-helper entry point not found in {HELPER}")
helper = helper[:entry] + r"""
let fixtureFailures = 0;

function fixtureFail(name, message, detail) {
	fixtureFailures++;
	printf('FAIL %s: %s; detail=%J; events=%J\n', name, message, detail, fixtureEvents);
}

function fixtureRun(name, baseUrl, expectedOk, expectedCode, baseSsid) {
	if (baseSsid == null) baseSsid = 'вифи';
	fixtureReset(baseUrl, baseSsid);
	let response;
	try { response = configure('nonce-0123456789'); }
	catch (e) {
		fixtureFail(name, 'native exception', { type: type(e), exception: e });
		return;
	}
	if (expectedOk) {
		if (response.ok !== true || fixtureValues['wifi.password'] != 'bootstrap-pass' ||
			fixtureValues['wifi.base_ssid'] != baseSsid || fixtureFiles['/etc/autovpn/credentials'] == null)
			fixtureFail(name, 'expected successful confirmed-bootstrap configure', response);
	}
	else if (response.ok !== false || response.code != expectedCode || fixtureFiles['/etc/autovpn/credentials'] != null) {
		fixtureFail(name, 'unexpected rejection result', response);
	}
	printf('CASE %s: %s\n', name, response.ok === true ? 'ok' : response.code);
}

fixtureRun('dns-origin', 'https://vpn.example', true, null);
fixtureRun('ipv4-origin', 'https://192.0.2.10', true, null);
fixtureRun('port-and-path', 'https://vpn.example:8443/api/v1', true, null);
fixtureRun('max-length', 'https://' + fixtureRepeat('a', 247), true, null);
fixtureRun('over-limit', 'https://' + fixtureRepeat('a', 248), false, 'setup_request_invalid');
fixtureRun('query-rejected', 'https://vpn.example?mode=pair', false, 'setup_request_invalid');
fixtureRun('fragment-rejected', 'https://vpn.example/#pair', false, 'setup_request_invalid');
fixtureRun('userinfo-rejected', 'https://user@vpn.example', false, 'setup_request_invalid');
fixtureRun('plaintext-rejected', 'http://vpn.example', false, 'setup_request_invalid');
fixtureRun('ascii-ssid-wpa2', 'https://vpn.example', true, null, 'Dorm');
fixtureRun('nul-ssid-rejected', 'https://vpn.example', false, 'setup_request_invalid', 'bad\x00ssid');
fixtureRun('control-ssid-rejected', 'https://vpn.example', false, 'setup_request_invalid', 'bad\x01ssid');
fixtureRun('unit-separator-ssid-rejected', 'https://vpn.example', false, 'setup_request_invalid', 'bad\x1fssid');
fixtureRun('del-ssid-rejected', 'https://vpn.example', false, 'setup_request_invalid', 'bad\x7fssid');

if (fixtureFailures > 0) exit(1);
printf('All native setup-helper cases passed.\n');
exit(0);
"""

fixture = r"""/* Generated diagnostic fixture: no native fs, UCI or process calls. */
let fixtureLastFsError;
let fixtureEvents;
let fixtureFiles;
let fixtureDirectories;
let fixtureValues;

function fixtureRepeat(character, count) {
	let result = '';
	for (let i = 0; i < count; i++) result += character;
	return result;
}

function fixtureReset(baseUrl, baseSsid) {
	fixtureLastFsError = null;
	fixtureEvents = [];
	fixtureFiles = { '/etc/autovpn/networks/journal.json': '{"phase":"confirmed"}' };
	fixtureDirectories = { '/etc/autovpn': true, '/etc/autovpn/state': true };
	fixtureValues = {
		'main.credential_file': '/etc/autovpn/credentials',
		'main.enabled': '0',
		'main.setup_prepared': '0',
		'main.base_url': '',
		'main.router_id': '',
		'runtime.wan_device': '',
		'wifi.primary_lan': '1',
		'wifi.bootstrap_completed': '1',
		'wifi.base_ssid': baseSsid,
		'wifi.password': 'bootstrap-pass',
	};
	fixtureFiles['/dev/stdin'] = sprintf('%J', {
		base_url: baseUrl,
		router_id: 'router123',
		credential: 'avrt_router123.sssssssssssssssssssssssssssssssssssssssssss',
		base_ssid: baseSsid,
		password: '',
		wan_device: 'eth0',
	});
}

function fixtureEvent(name) { push(fixtureEvents, name); }
function fixtureMissing() { fixtureLastFsError = 'No such file or directory'; return null; }
function access(name, mode) {
	fixtureEvent('access:' + name + ':' + mode);
	if (name == '/sys/class/net/eth0' || fixtureDirectories[name] === true || fixtureFiles[name] != null) {
		fixtureLastFsError = null;
		return true;
	}
	return fixtureMissing();
}
function chmod(name, mode) { fixtureEvent('chmod:' + name); fixtureLastFsError = null; return true; }
function fsError() { return fixtureLastFsError; }
function lstat(name) {
	fixtureEvent('lstat:' + name);
	if (fixtureFiles[name] != null) { fixtureLastFsError = null; return { type: 'file' }; }
	if (fixtureDirectories[name] === true) { fixtureLastFsError = null; return { type: 'directory' }; }
	return fixtureMissing();
}
function mkdir(name) { fixtureEvent('mkdir:' + name); fixtureDirectories[name] = true; fixtureLastFsError = null; return true; }
function readfile(name, limit) {
	fixtureEvent('readfile:' + name + ':' + limit);
	if (fixtureFiles[name] != null) { fixtureLastFsError = null; return fixtureFiles[name]; }
	return fixtureMissing();
}
function rename(oldName, newName) {
	fixtureEvent('rename:' + oldName + ':' + newName);
	if (fixtureFiles[oldName] == null) return fixtureMissing();
	fixtureFiles[newName] = fixtureFiles[oldName];
	fixtureFiles[oldName] = null;
	fixtureLastFsError = null;
	return true;
}
function stat(name) { fixtureEvent('stat:' + name); return lstat(name); }
function unlink(name) {
	fixtureEvent('unlink:' + name);
	if (fixtureFiles[name] == null) return fixtureMissing();
	fixtureFiles[name] = null;
	fixtureLastFsError = null;
	return true;
}
function writefile(name, value) {
	fixtureEvent('writefile:' + name);
	fixtureFiles[name] = value;
	fixtureLastFsError = null;
	return length(value);
}

let fixtureCtx = {
	load: function(config) { fixtureEvent('uci.load:' + config); return true; },
	get: function(config, section, option) {
		fixtureEvent('uci.get:' + section + '.' + option);
		return fixtureValues[section + '.' + option];
	},
	set: function(config, section, option, value) {
		fixtureEvent('uci.set:' + section + '.' + option);
		fixtureValues[section + '.' + option] = value;
		return true;
	},
	commit: function(config) { fixtureEvent('uci.commit:' + config); return true; },
	changes: function(config) {
		fixtureEvent('uci.changes:' + config);
		let answer = {};
		answer[config] = [];
		return answer;
	},
};
function cursor() { fixtureEvent('uci.cursor'); return fixtureCtx; }

const fixtureProcessRunner = {
	popen: function(argv, mode) {
		fixtureEvent('process.popen:' + argv[1] + ':' + argv[2]);
		return {
			read: function(limit) { fixtureEvent('process.read:' + limit); return '{"ok":true}'; },
			close: function() { fixtureEvent('process.close'); return 0; },
		};
	},
};
"""

sys.stdout.write(fixture + "\n" + helper)
