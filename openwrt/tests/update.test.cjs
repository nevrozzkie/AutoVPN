'use strict';

const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');

const source = readFileSync(join(__dirname, '../files/usr/libexec/autovpn/update-helper'), 'utf8');

test('updater is APK-only and keeps firmware, feeds and remote scripts out of scope', () => {
  assert.match(source, /autovpn-controller/);
  assert.match(source, /apk --no-network --cache-dir \"\$WORK\/empty-cache\" --keys-dir \"\$WORK\/update-key\" add/);
  assert.doesNotMatch(source, /sysupgrade|opkg|apk upgrade|--allow-untrusted|--force-overwrite|wget .*\| *sh/i);
  assert.doesNotMatch(source, /kmod-amneziawg.*apk --no-network add/s);
});

test('update trust is pinned locally and only immutable tags in the recorded GitHub repo are accepted', () => {
  assert.match(source, /RECEIPT=\"\$ROOT\/release\.json\"/);
  assert.match(source, /KEY=\"\$ROOT\/release-signing\.pem\"/);
  assert.match(source, /sha256sum \"\$KEY\"/);
  assert.match(source, /https:\/\/github\.com/);
  assert.match(source, /latest\|main\|master\|head/);
  assert.match(source, /--proto '=https'/);
});

test('a plan is frozen, then rechecked before install, under both maintenance locks', () => {
  assert.match(source, /PLAN_TTL=900/);
  assert.match(source, /expires_uptime/);
  assert.match(source, /check_now \"\$plan_tag\"/);
  assert.match(source, /update_plan_changed/);
  assert.match(source, /exec 8>>"\$LOCK"/);
  assert.match(source, /flock -n 8/);
  assert.match(source, /exec 9>>\/var\/lock\/autovpn-controller\.lock/);
  assert.match(source, /flock -n 9/);
	assert.match(source, /printf '0\\n' >"\$LOCK"/);
	assert.match(source, /exec 8>&-/);
	assert.match(source, /exec 9>&-/);
	assert.doesNotMatch(source, /flock -u|lock -u|rm[^\n]*autovpn-(?:update|controller)\.lock/);
  assert.match(source, /maintenance\.lock/);
  assert.match(source, /network-helper\.uc network-gate/);
  assert.match(source, /runtime-adapter fail-closed/);
  assert.match(source, /uci set autovpn\.main\.enabled=0/);
	assert.match(source, /STATE=\"\$ROOT\/state\/update-status\.json\"/);
	assert.match(source, /GATE=\"\$ROOT\/state\/update\.lock\"/);
	assert.match(source, /case \"\$gate_phase\" in running\|failed/);
	assert.match(source, /GATE_OWNED=1/);
});

test('LuCI request queues a private copied worker and exposes only redacted status', () => {
  assert.match(source, /update-worker/);
  assert.match(source, /write_state queued/);
  assert.match(source, /\(exec 8>&- 9>&-; "\$worker" worker "\$id"\).*&/);
  assert.match(source, /phase.*current_version.*candidate_id.*candidate_version/s);
  assert.doesNotMatch(source, /credential|token|private_key/i);
});
