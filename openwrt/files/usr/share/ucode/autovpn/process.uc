'use strict';

/* OpenWrt 25.12 fs.popen() accepts a command string, not an argv array. */
import { popen as nativePopen } from 'fs';

function validExecutable(value) {
	return type(value) == 'string' && length(value) > 1 && length(value) <= 192 &&
		match(value, /^\/[A-Za-z0-9._+\/-]+$/) != null && index(value, '//') < 0;
}

function quote(value) {
	/* POSIX: close quote, emit literal quote, re-open quote. */
	return "'" + replace(value, /'/g, "'\\''") + "'";
}

function popen(argv, mode) {
	if (type(argv) != 'array' || length(argv) < 1 || length(argv) > 32 || !validExecutable(argv[0])) return null;
	if (mode != 'r' && mode != 'w') return null;
	let command = '';
	for (let i = 0; i < length(argv); i++) {
		let value = argv[i];
		if (type(value) != 'string' || length(value) > 1024 || index(value, '\u0000') >= 0) return null;
		command += (i ? ' ' : '') + quote(value);
	}
	return length(command) <= 8192 ? nativePopen(command, mode) : null;
}

return { popen };
