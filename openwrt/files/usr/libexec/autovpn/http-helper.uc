#!/usr/bin/ucode

'use strict';

import { chmod, readfile, writefile } from 'fs';

const http = require('autovpn.http');

let result = http.run(ARGV, {
	read: function(path, limit) {
		return readfile(path, limit);
	},
	writePrivate: function(path, value) {
		if (writefile(path, value) == null)
			return false;
		return chmod(path, 0o600) != null;
	},
});

printf('%J\n', result);
exit(result.ok === true ? 0 : 1);
