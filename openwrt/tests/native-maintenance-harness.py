#!/usr/bin/env python3
"""Emit an isolated native-ucode maintenance rebind harness on stdout."""

from pathlib import Path
import sys


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1])
FILES = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT / "files"
HELPER = FILES / "usr/libexec/autovpn/maintenance-helper.uc"


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
entry = helper.find("let nonce = ARGV[0];")
if entry < 0:
    raise SystemExit(f"maintenance-helper entry point not found in {HELPER}")
helper = helper[:entry] + r"""
let fixtureFailures = 0;

function fixtureFail(name, message, detail) {
	fixtureFailures++;
	printf('FAIL %s: %s; detail=%J; events=%J\n', name, message, detail, fixtureEvents);
}

function fixtureRun(name, runningGate, confirmation, expectSuccess) {
	fixtureReset(runningGate, confirmation);
	let response;
	try { response = perform(readRequest()); }
	catch (e) {
		fixtureFail(name, 'native exception', { type: type(e), exception: e });
		return;
	}
	let lock = fixtureFiles['/etc/autovpn/state/maintenance.lock'];
	let lockState;
	try { lockState = lock != null ? json(lock) : null; } catch (e) { lockState = null; }
	if (expectSuccess) {
		if (response.ok !== true || response.rebound !== true || response.requires_activation !== true ||
			fixtureValues['main.enabled'] != '0' || fixtureValues['main.setup_prepared'] != '1' ||
			fixtureValues['main.base_url'] != 'https://vpn.example' || fixtureValues['main.router_id'] != 'router123' ||
			fixtureFiles['/etc/autovpn/credentials'] != 'avrt_router123.sssssssssssssssssssssssssssssssssssssssssss\n' ||
			type(lockState) != 'object' || lockState.action != 'rebind' || lockState.phase != 'ready' ||
			fixtureFiles['/etc/autovpn/state/journal.json'] != null || fixtureFiles['/etc/autovpn/runtime/current.json'] != null)
			fixtureFail(name, 'successful rebind invariants failed', { response: response, lock: lockState });
	}
	else if (response.ok !== false || response.code != 'rebind_invalid' || lock != null ||
		fixtureValues['main.base_url'] != 'https://old.example' ||
		fixtureFiles['/etc/autovpn/credentials'] == null) {
		fixtureFail(name, 'invalid rebind mutated state', { response: response, lock: lockState });
	}
	printf('CASE %s: %s\n', name, response.ok === true ? 'ok' : response.code);
}

fixtureRun('rebind', false, 'REBIND', true);
fixtureRun('retry-running-rebind', true, 'REBIND', true);
fixtureRun('invalid-confirmation', false, 'WRONG', false);

if (fixtureFailures > 0) exit(1);
printf('All native maintenance-helper cases passed.\n');
exit(0);
"""

fixture = r"""/* Generated diagnostic fixture: no native fs, UCI or process calls. */
let fixtureLastFsError;
let fixtureEvents;
let fixtureDirectories;
let fixtureFiles;
let fixtureValues;

function fixtureReset(runningGate, confirmation) {
	fixtureLastFsError = null;
	fixtureEvents = [];
	fixtureDirectories = {
		'/etc/autovpn': true,
		'/etc/autovpn/state': true,
		'/etc/autovpn/runtime': true,
		'/etc/autovpn/runtime-zapret': true,
	};
	fixtureFiles = {
		'/dev/stdin': sprintf('%J', {
			action: 'rebind',
			confirmation: confirmation,
			base_url: 'https://vpn.example',
			router_id: 'router123',
			credential: 'avrt_router123.sssssssssssssssssssssssssssssssssssssssssss',
		}),
		'/etc/autovpn/credentials': 'avrt_oldrouter.ttttttttttttttttttttttttttttttttttttttttttt\n',
		'/etc/autovpn/networks/journal.json': '{"phase":"confirmed"}',
		'/etc/autovpn/state/journal.json': '{"phase":"prepared"}',
		'/etc/autovpn/runtime/current.json': '{"old":true}',
	};
	if (runningGate)
		fixtureFiles['/etc/autovpn/state/maintenance.lock'] =
			'{"schema_version":1,"action":"rebind","phase":"running"}\n';
	fixtureValues = {
		'main.credential_file': '/etc/autovpn/credentials',
		'main.state_dir': '/etc/autovpn/state',
		'main.runtime_adapter': '/usr/libexec/autovpn/runtime-adapter',
		'main.http_adapter': '/usr/libexec/autovpn/http-adapter',
		'main.base_url': 'https://old.example',
		'main.router_id': 'oldrouter',
		'main.enabled': '0',
		'main.setup_prepared': '1',
	};
}

function fixtureEvent(name) { push(fixtureEvents, name); }
function fixtureMissing() { fixtureLastFsError = 'No such file or directory'; return null; }
function access(name, mode) {
	fixtureEvent('access:' + name + ':' + mode);
	if (fixtureDirectories[name] === true || fixtureFiles[name] != null) { fixtureLastFsError = null; return true; }
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
	delete: function(config, section, option) {
		fixtureEvent('uci.delete:' + section + '.' + option);
		fixtureValues[section + '.' + option] = null;
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
		fixtureEvent('process.popen:' + join(' ', argv));
		let command = argv[0];
		return {
			read: function(limit) {
				fixtureEvent('process.read:' + limit);
				return command == '/bin/sync' ? '' : '{"ok":true}';
			},
			close: function() { fixtureEvent('process.close:' + command); return 0; },
		};
	},
};
"""

sys.stdout.write(fixture + "\n" + helper)
