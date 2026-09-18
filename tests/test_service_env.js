// Where the Node services read REVIEW_CONTROL_SECRET from, and in what order.
// The router's .env used to be the only home, which made the model proxy a
// secrets bus; services/shared/.env is the home now, with the old path kept
// as a fallback so an upgrade that does not re-run the installer still boots.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { readServiceSecret, SHARED_ENV, LEGACY_ENV, ANCIENT_ENV } = require('../services/shared/service-env.js');

function home(files) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'svc-env-'));
  for (const [rel, body] of Object.entries(files)) {
    fs.mkdirSync(path.join(root, path.dirname(rel)), { recursive: true });
    fs.writeFileSync(path.join(root, rel), body);
  }
  return root;
}

test('the legacy fallback is the current router path, not the pre-rename one', () => {
  // Doctor warns about services/model-router/.env. If this constant still
  // said llm-router, an upgrade that never re-ran the installer would have
  // the secret where doctor looks and nowhere the Node services read.
  assert.equal(LEGACY_ENV, 'services/model-router/.env');
  assert.equal(ANCIENT_ENV, 'services/llm-router/.env');
});

test('the shared env file is the home', () => {
  const root = home({ [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=from-shared\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-shared');
});

test('the router env still works for a deployment installed before the split', () => {
  const root = home({ [LEGACY_ENV]: 'OPENROUTER_API_KEY=k\nREVIEW_CONTROL_SECRET=from-legacy\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-legacy');
});

test('the pre-rename llm-router path still works as a last resort', () => {
  const root = home({ [ANCIENT_ENV]: 'REVIEW_CONTROL_SECRET=from-ancient\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-ancient');
});

test('the current router leftover wins over the pre-rename path', () => {
  const root = home({
    [LEGACY_ENV]: 'REVIEW_CONTROL_SECRET=from-router\n',
    [ANCIENT_ENV]: 'REVIEW_CONTROL_SECRET=from-ancient\n',
  });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-router');
});

test('the shared file wins over the legacy one', () => {
  const root = home({
    [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=new\n',
    [LEGACY_ENV]: 'REVIEW_CONTROL_SECRET=old\n',
  });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'new');
});

test('the environment wins over every file', () => {
  const root = home({ [SHARED_ENV]: 'REVIEW_CONTROL_SECRET=file\n' });
  process.env.REVIEW_CONTROL_SECRET = 'from-env';
  try {
    assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'from-env');
  } finally {
    delete process.env.REVIEW_CONTROL_SECRET;
  }
});

test('a missing secret is null, so the caller can fail closed', () => {
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', home({})), null);
  assert.equal(readServiceSecret('NOPE', home({ [SHARED_ENV]: 'OTHER=1\n' })), null);
});

test('a value is not confused with a similarly named key', () => {
  const root = home({ [SHARED_ENV]: 'NOT_REVIEW_CONTROL_SECRET=wrong\nREVIEW_CONTROL_SECRET=right\n' });
  assert.equal(readServiceSecret('REVIEW_CONTROL_SECRET', root), 'right');
});

test('a <NAME>_FILE wins over the files on disk, and a missing one is not fatal', () => {
    // The bundle's reason for existing: the agent container generates this
    // secret on first run, and two other containers have to end up with the
    // same value without it being duplicated in compose or exposed in `ps`.
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'svcenv-'));
    const secretFile = path.join(dir, 'secret');
    fs.writeFileSync(secretFile, '  from-a-file\n');

    const home = path.join(dir, 'home');
    fs.mkdirSync(path.join(home, 'services', 'shared'), { recursive: true });
    fs.writeFileSync(path.join(home, 'services', 'shared', '.env'), 'X_SECRET=from-shared-env\n');

    process.env.X_SECRET_FILE = secretFile;
    delete process.env.X_SECRET;
    assert.equal(readServiceSecret('X_SECRET', home), 'from-a-file', 'the file wins, trimmed');

    process.env.X_SECRET_FILE = path.join(dir, 'does-not-exist');
    assert.equal(readServiceSecret('X_SECRET', home), 'from-shared-env',
        'an unreadable path is one more place that did not answer, not a crash');

    process.env.X_SECRET = 'from-the-environment';
    assert.equal(readServiceSecret('X_SECRET', home), 'from-the-environment',
        'the environment still wins over everything');

    delete process.env.X_SECRET;
    delete process.env.X_SECRET_FILE;
});
