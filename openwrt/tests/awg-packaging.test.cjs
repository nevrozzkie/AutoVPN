'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const moduleRecipe = fs.readFileSync(path.join(root, 'packages/amneziawg/Makefile'), 'utf8');
const toolsRecipe = fs.readFileSync(path.join(root, 'packages/amneziawg-tools/Makefile'), 'utf8');
const uapiPatch = fs.readFileSync(path.join(root, 'packages/amneziawg-tools/patches/010-uapi-version-2.patch'), 'utf8');
const receiptPath = path.join(root, 'packages/amneziawg-tools/files/usr/share/autovpn/awg-engine.json');

test('AmneziaWG packages pin the audited standalone module and matching tools sources', () => {
	assert.match(moduleRecipe, /PKG_VERSION:=1\.0\.20251004/);
	assert.match(moduleRecipe, /PKG_HASH:=c60393ed591c87abebcdd6f7bd87e3faef747902a4f76a6b41bf4ff189220e75/);
	assert.match(moduleRecipe, /amneziawg-linux-kernel-module\/archive\/refs\/tags/);
	assert.match(moduleRecipe, /\$\(KERNEL_MAKE_FLAGS\)/);
	assert.match(moduleRecipe, /FILES:=\$\(PKG_BUILD_DIR\)\/src\/amneziawg\.ko/);
	assert.doesNotMatch(moduleRecipe, /kernel=6\.12|5a6c1f71|KERNEL_ABI/,
		'KernelPackage must derive the exact SDK kernel ABI dependency');

	assert.match(toolsRecipe, /PKG_VERSION:=1\.0\.20250903/);
	assert.match(toolsRecipe, /PKG_HASH:=d729a6f54aafcd55b2cbb7324f09ca8f0d2536772970652bf822a271d0c907d7/);
	assert.match(toolsRecipe, /\$\(INSTALL_BIN\) \$\(PKG_BUILD_DIR\)\/src\/wg \$\(1\)\/usr\/bin\/awg/);
});

test('tools package contains only awg and the exact immutable engine contract', () => {
	assert.deepEqual(JSON.parse(fs.readFileSync(receiptPath, 'utf8')),
		{ schema_version: 1, config_mode: 'awg1-on-uapi2' });
	assert.doesNotMatch(toolsRecipe, /awg-quick|watchdog|netifd|\/etc\/config/);
	assert.match(toolsRecipe, /\$\(INSTALL_DATA\) \.\/files\/usr\/share\/autovpn\/awg-engine\.json/);
	assert.match(uapiPatch, /-#define WG_GENL_VERSION 1\n\+#define WG_GENL_VERSION 2/);
});
