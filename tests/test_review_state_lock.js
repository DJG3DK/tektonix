'use strict';
/**
 * state.json has two writers -- commit-reviewer (sync) and agent-review
 * (async) -- in separate processes. A read-modify-write by one that
 * straddled a write by the other silently undid it. shared/json-state puts
 * every read-modify-write under one lockfile.
 *
 * Run: node tests/test_review_state_lock.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const HELPER = path.resolve(__dirname, '../services/shared/json-state.js');
const { readJson, updateJsonSync, updateJson, STALE_MS } = require(HELPER);

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'review-state-lock-'));
const ROUNDS = 150;

function worker(file, key, mode) {
  const body = mode === 'async'
    ? `const { updateJson } = require(${JSON.stringify(HELPER)});
       (async () => { for (let i = 0; i < ${ROUNDS}; i++) await updateJson(${JSON.stringify(file)}, (s) => {
         s.total = (s.total || 0) + 1; s[${JSON.stringify(key)}] = (s[${JSON.stringify(key)}] || 0) + 1; }); })();`
    : mode === 'sync'
      ? `const { updateJsonSync } = require(${JSON.stringify(HELPER)});
         for (let i = 0; i < ${ROUNDS}; i++) updateJsonSync(${JSON.stringify(file)}, (s) => {
           s.total = (s.total || 0) + 1; s[${JSON.stringify(key)}] = (s[${JSON.stringify(key)}] || 0) + 1; });`
      : `const fs = require('fs'); const { readJson } = require(${JSON.stringify(HELPER)});
         for (let i = 0; i < ${ROUNDS}; i++) { const s = readJson(${JSON.stringify(file)});
           s.total = (s.total || 0) + 1; s[${JSON.stringify(key)}] = (s[${JSON.stringify(key)}] || 0) + 1;
           const t = ${JSON.stringify(file)} + '.tmp-' + process.pid + '-' + i;
           fs.writeFileSync(t, JSON.stringify(s)); fs.renameSync(t, ${JSON.stringify(file)}); }`;
  return new Promise((resolve, reject) => {
    const p = spawn(process.execPath, ['-e', body], { stdio: ['ignore', 'inherit', 'inherit'] });
    p.on('exit', (code) => (code === 0 ? resolve() : reject(new Error(`${mode} worker exited ${code}`))));
  });
}

async function concurrentWritersLoseNothing() {
  const file = path.join(tmp, 'state.json');
  const modes = ['sync', 'async', 'sync', 'async'];
  await Promise.all(modes.map((m, i) => worker(file, `w${i}`, m)));
  const state = readJson(file);
  assert.strictEqual(state.total, ROUNDS * modes.length, `lost updates: ${JSON.stringify(state)}`);
  modes.forEach((_, i) => assert.strictEqual(state[`w${i}`], ROUNDS));
  assert.ok(!fs.existsSync(`${file}.lock`), 'lock released');
  assert.deepStrictEqual(fs.readdirSync(tmp).filter((f) => f.includes('.tmp-')), [], 'no temp files left');

  // The same load comparison without the lock, so a pass above means the lock
  // did the work rather than the workers never overlapping.
  const naive = path.join(tmp, 'naive.json');
  await Promise.all(modes.map((_, i) => worker(naive, `w${i}`, 'naive')));
  console.log(`  unlocked control: ${readJson(naive).total}/${ROUNDS * modes.length} updates survived`);
}

function aStaleLockIsBroken() {
  const file = path.join(tmp, 'stale.json');
  const lock = `${file}.lock`;
  fs.writeFileSync(lock, '999999');
  const old = (Date.now() - STALE_MS - 1000) / 1000;
  fs.utimesSync(lock, old, old);
  updateJsonSync(file, (s) => { s.ok = true; });
  assert.deepStrictEqual(readJson(file), { ok: true });
  assert.ok(!fs.existsSync(lock));
}

async function aLiveLockIsWaitedFor() {
  const file = path.join(tmp, 'live.json');
  const lock = `${file}.lock`;
  fs.writeFileSync(lock, String(process.pid));
  let done = false;
  const pending = updateJson(file, (s) => { s.after = true; }).then(() => { done = true; });
  await new Promise((r) => setTimeout(r, 150));
  assert.strictEqual(done, false, 'must not write while another holder has the lock');
  fs.unlinkSync(lock);
  await pending;
  assert.deepStrictEqual(readJson(file), { after: true });
}

function returningFalseSkipsTheWrite() {
  const file = path.join(tmp, 'skip.json');
  updateJsonSync(file, () => false);
  assert.ok(!fs.existsSync(file));
}

function bothServicesWriteThroughTheLock() {
  const reviewer = fs.readFileSync(path.join(__dirname, '../services/commit-reviewer/reviewer.js'), 'utf8');
  assert.ok(!/saveState\s*\(/.test(reviewer), 'reviewer must not write state.json outside updateState');
  assert.ok(!/writeFileSync\([^)]*STATE_PATH/.test(reviewer));
  const server = fs.readFileSync(path.join(__dirname, '../services/agent-review/server.js'), 'utf8');
  const clear = server.slice(server.indexOf('async function clearReviewState'));
  assert.ok(/await updateJson\(REVIEW_STATE_PATH/.test(clear.slice(0, 400)), 'clearReviewState uses the lock');
  assert.ok(!/writeFile\([^)]*REVIEW_STATE_PATH/.test(server));
}

(async () => {
  try {
    await concurrentWritersLoseNothing();
    aStaleLockIsBroken();
    await aLiveLockIsWaitedFor();
    returningFalseSkipsTheWrite();
    bothServicesWriteThroughTheLock();
    console.log('test_review_state_lock: all passed');
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
})().catch((e) => { console.error(e); process.exit(1); });

// Code scanning (2026-09-27): a project name of "__proto__" in the URL found
// Object.prototype in the state file's record, and the merge endpoint then
// set a property on it. Lookups by a request value are own-property only.
{
    const src = fs.readFileSync(path.join(__dirname, '..', 'services', 'agent-review', 'server.js'), 'utf8');
    assert.match(src, /function own\(record, name\)[\s\S]*Object\.hasOwn\(record, name\)/);
    assert.doesNotMatch(src, /\)\[req\.params\.name\]/, 'a raw [req.params.name] lookup remains');
    assert.doesNotMatch(src, /readReviewState\(\)\)\[name\]/, 'a raw [name] lookup on the review state remains');
    console.log('own-property lookups by request name: ok');
}
