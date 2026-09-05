'use strict';

const assert = require('node:assert/strict');
const { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, chmodSync } = require('node:fs');
const { tmpdir } = require('node:os');
const { join } = require('node:path');
const { spawnSync } = require('node:child_process');
const { createHash } = require('node:crypto');
const test = require('node:test');

const root = join(__dirname, '..');
const assembler = join(root, 'scripts', 'prepare-release.py');

test('default release version matches the installed controller package', () => {
  const version = readFileSync(join(root, 'Makefile'), 'utf8').match(/^PKG_VERSION:=(.+)$/m)[1];
  const defaultVersion = readFileSync(assembler, 'utf8').match(/^APP_VERSION = "([^"]+)"$/m)[1];
  assert.equal(defaultVersion, version);
});

function sha256(path) {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

function fixture() {
  const dir = mkdtempSync(join(tmpdir(), 'autovpn-release-test-'));
  const fakeApk = join(dir, 'apk');
  writeFileSync(fakeApk, `#!/bin/sh
if [ "$1" = verify ]; then exit 0; fi
if [ "$1" = adbdump ]; then
  file="${'$'}4"
  case "${'$'}file" in
    *autovpn-controller*) printf '{"info":{"name":"autovpn-controller","version":"0.7.0-r1","arch":"%s","depends":[]}}\\n' "${'$'}{FAKE_CONTROLLER_ARCH:-all}" ;;
    *kmod-amneziawg*) printf '{"info":{"name":"kmod-amneziawg","arch":"%s","depends":["kernel=6.6.99~fixture"]}}\\n' "${'$'}{FAKE_KMOD_ARCH:-aarch64_cortex-a53}" ;;
    *amneziawg-tools*) echo '{"info":{"name":"amneziawg-tools","arch":"aarch64_cortex-a53","depends":[]}}' ;;
    *) exit 4 ;;
  esac
  exit 0
fi
exit 5
`);
  chmodSync(fakeApk, 0o755);
  const key = join(dir, 'public.pem');
  writeFileSync(key, '-----BEGIN PUBLIC KEY-----\nfixture-public-key\n-----END PUBLIC KEY-----\n');
  const template = join(dir, 'install.template.sh');
  writeFileSync(template, '#!/bin/sh\nbase=@AUTOVPN_RELEASE_BASE@\nkey=@AUTOVPN_SIGNING_KEY_SHA256@\nmanifest=@AUTOVPN_MANIFEST_SHA256@\n');
  const controller = join(dir, 'autovpn-controller-0.7.0-r1.apk');
  const module = join(dir, 'kmod-amneziawg-1-r1.apk');
  const tools = join(dir, 'amneziawg-tools-1-r1.apk');
  for (const path of [controller, module, tools]) writeFileSync(path, `fixture ${path}`);
  return { dir, fakeApk, key, template, controller, module, tools };
}

function run(f, output, packages = [f.controller, f.module, f.tools], extra = [], options = {}) {
  const args = [options.repoRoot ? 'openwrt/scripts/prepare-release.py' : assembler,
    '--app-version', '0.7.0',
    '--apk', f.fakeApk,
    '--release', '25.12.3',
    '--target', 'mediatek/filogic',
    '--architecture', 'aarch64_cortex-a53',
    '--kernel-release', '6.6.99',
    '--kernel-package', '6.6.99~fixture',
    '--release-base', 'https://github.com/nevrozzkie/AutoVPN/releases/download/router-v0.7.0',
    '--signing-key', f.key,
    '--min-free-kib', '32768',
    '--min-tmp-kib', '8192',
    '--output', output,
    ...(options.defaultTemplate ? [] : ['--install-template', f.template]),
    ...extra,
  ];
  for (const pkg of packages) args.push('--package', pkg);
  return spawnSync('python3', args, {
    encoding: 'utf8',
    env: {...process.env, ...(options.env || {})},
    ...(options.repoRoot ? { cwd: join(root, '..') } : {}),
  });
}

test('monorepo root invocation uses the router template, never the server installer', () => {
  const f = fixture();
  const output = join(f.dir, 'from-monorepo-root');
  const result = run(f, output, [f.controller], [], { repoRoot: true, defaultTemplate: true });
  assert.equal(result.status, 0, result.stderr);
  const install = readFileSync(join(output, 'install.sh'), 'utf8');
  assert.match(install, /Configure the site URL and token in LuCI/);
  assert.match(install, /arch_file=\/etc\/apk\/arch/);
  assert.doesNotMatch(install, /@AUTOVPN_|APP_DIR=.*opt\/autovpn/);
});

test('assembles immutable release artifacts and pins the manifest', () => {
  const f = fixture();
  const output = join(f.dir, 'release');
  const result = run(f, output);
  assert.equal(result.status, 0, result.stderr);
  const manifestPath = join(output, 'manifest-25.12.3-mediatek-filogic-aarch64_cortex-a53.json');
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  assert.equal(manifest.schema_version, 1);
  assert.equal(manifest.version, '0.7.0');
  assert.equal(manifest.kernel_package, '6.6.99~fixture');
  assert.equal(manifest.packages[0].version, '0.7.0-r1');
  assert.equal(manifest.capabilities.amneziawg, true);
  assert.deepEqual(manifest.packages.map((pkg) => pkg.name), ['autovpn-controller', 'kmod-amneziawg', 'amneziawg-tools']);
  const install = readFileSync(join(output, 'install.sh'), 'utf8');
  assert.match(install, /https:\/\/github\.com\/nevrozzkie\/AutoVPN\/releases\/download\/router-v0\.7\.0/);
  assert.match(install, new RegExp(sha256(manifestPath)));
  assert.equal(existsSync(join(output, 'autovpn-signing.pem')), true);
  const repair = readFileSync(join(output, 'repair-bootstrap.sh'), 'utf8');
  assert.match(repair, new RegExp(sha256(manifestPath)));
  assert.match(repair, new RegExp(sha256(f.key)));
  assert.doesNotMatch(repair, /@AUTOVPN_/);
});

test('accepts APK v3 noarch metadata only for the controller', () => {
  const f = fixture();
  const controllerNoarch = run(f, join(f.dir, 'controller-noarch'), undefined, [], {
    env: {FAKE_CONTROLLER_ARCH: 'noarch'},
  });
  assert.equal(controllerNoarch.status, 0, controllerNoarch.stderr);

  const nativeNoarch = run(f, join(f.dir, 'native-noarch'), undefined, [], {
    env: {FAKE_KMOD_ARCH: 'noarch'},
  });
  assert.notEqual(nativeNoarch.status, 0);
  assert.match(nativeNoarch.stderr, /kmod-amneziawg has arch 'noarch'; expected aarch64_cortex-a53/);
});

test('refuses mutable release bases and partial AWG packages', () => {
  const f = fixture();
  const mutable = run(f, join(f.dir, 'latest'), [f.controller], ['--release-base', 'https://github.com/nevrozzkie/AutoVPN/releases/download/latest']);
  assert.notEqual(mutable.status, 0);
  assert.match(mutable.stderr, /immutable/i);
  const partial = run(f, join(f.dir, 'partial'), [f.controller, f.module]);
  assert.notEqual(partial.status, 0);
  assert.match(partial.stderr, /all-or-nothing/);
});

test('does not overwrite an existing release directory', () => {
  const f = fixture();
  const output = join(f.dir, 'exists');
  mkdirSync(output);
  const result = run(f, output);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /must not already exist/);
});

test('never accepts a private signing key', () => {
  const f = fixture();
  writeFileSync(f.key, '-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----\n');
  const result = run(f, join(f.dir, 'private-key'));
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /public APK key/i);
});
