'use strict';

import { lsdir } from 'fs';

/* Run with the real OpenWrt ucode, with the package's module root on -L. */
let root = ARGV[0];
if (type(root) != 'string' || !length(root)) die('module directory argument required\n');
let files = lsdir(root);
if (files == null || !length(files)) die('module directory is empty or unreadable\n');
let count = 0;
for (let file in files) {
	if (!match(file, /\.uc$/)) continue;
	if (!match(file, /^[A-Za-z0-9_]+\.uc$/)) die('invalid module filename: ' + file + '\n');
	let name = 'autovpn.' + replace(file, /\.uc$/, '');
	if (type(require(name)) != 'object') die('invalid module export: ' + name + '\n');
	print(name, ' OK\n');
	count++;
}
if (!count) die('no ucode modules tested\n');
print(count, ' native modules loaded\n');
