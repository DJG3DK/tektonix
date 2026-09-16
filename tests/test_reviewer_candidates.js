// Exactly one branch per project is the review unit. 2026-09-16: a manual
// `agent/230eed5b-pre-rebase-backup` branch sat unmerged beside the live
// task's branch, and detectNewCommit walked the candidate list past whichever
// branch the last round had reviewed -- so the two alternated all day (forty
// reviews of one project) and the merge gate saw the real task as "stale".
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { detectNewCommit, TASK_BRANCH_RE } = require('../services/commit-reviewer/reviewer.js');

const TASK_NEW = 'agent/7af2d437-7006-4645-85ad-bdd082d453a4';
const TASK_OLD = 'agent/230eed5b-99a0-4269-b1f3-b8c908a04744';
const BACKUP = 'agent/230eed5b-pre-rebase-backup';

const identity = {
  GIT_AUTHOR_NAME: 't', GIT_AUTHOR_EMAIL: 't@example.com',
  GIT_COMMITTER_NAME: 't', GIT_COMMITTER_EMAIL: 't@example.com',
};
Object.assign(process.env, identity); // reviewer.js's git() inherits process.env

function git(cwd, args, extraEnv = {}) {
  return execFileSync('git', args, { cwd, env: { ...process.env, ...extraEnv }, encoding: 'utf8' }).trim();
}
function commit(cwd, file, date) {
  fs.writeFileSync(path.join(cwd, file), `${file} ${date}\n`);
  git(cwd, ['add', file]);
  git(cwd, ['commit', '-q', '-m', file], { GIT_AUTHOR_DATE: date, GIT_COMMITTER_DATE: date });
  return git(cwd, ['rev-parse', 'HEAD']);
}

// live: main has A, B. TASK_OLD forks at A (abandoned, unmerged). TASK_NEW forks
// at B. BACKUP forks at A and carries the NEWEST committer date of all three,
// so it is first in for-each-ref order.
function fixture() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'reviewer-candidates-'));
  const live = path.join(root, 'live');
  fs.mkdirSync(live);
  git(live, ['init', '-q', '-b', 'main']);
  commit(live, 'a.txt', '2026-09-10T10:00:00Z');
  git(live, ['branch', TASK_OLD]);
  git(live, ['branch', BACKUP]);
  commit(live, 'b.txt', '2026-09-11T10:00:00Z');
  git(live, ['checkout', '-q', TASK_OLD]);
  commit(live, 'old.txt', '2026-09-12T10:00:00Z');
  git(live, ['checkout', '-q', BACKUP]);
  commit(live, 'backup.txt', '2026-09-16T10:00:00Z');
  git(live, ['checkout', '-q', 'main']);
  git(live, ['branch', TASK_NEW]);
  const sandbox = path.join(root, 'ws');
  git(live, ['worktree', 'add', '-q', sandbox, TASK_NEW]);
  const newTip = commit(sandbox, 'new.txt', '2026-09-14T10:00:00Z');
  return { root, cfg: { live, sandbox }, newTip };
}

test('TASK_BRANCH_RE admits agent/<task-id> and nothing else', () => {
  assert.ok(TASK_BRANCH_RE.test(TASK_NEW));
  assert.ok(!TASK_BRANCH_RE.test(BACKUP));
  assert.ok(!TASK_BRANCH_RE.test('agent/main'));
});

test('the workspace branch is the review unit, not the newest ref', async () => {
  const { root, cfg, newTip } = fixture();
  try {
    const unit = await detectNewCommit('p', cfg, undefined);
    assert.equal(unit?.branch, TASK_NEW);
    assert.equal(unit.sha, newTip);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('once reviewed at its tip, nothing else is picked up -- no ping-pong', async () => {
  const { root, cfg, newTip } = fixture();
  try {
    const prev = { branch: TASK_NEW, lastReviewedSha: newTip, verdict: 'READY' };
    assert.equal(await detectNewCommit('p', cfg, prev), null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('with the workspace off any task branch, the newest unmerged task branch wins and the backup is ignored', async () => {
  const { root, cfg } = fixture();
  try {
    git(cfg.sandbox, ['checkout', '-q', '--detach']);
    const unit = await detectNewCommit('p', cfg, undefined);
    assert.equal(unit?.branch, TASK_NEW, 'BACKUP has the newest date but is not a task branch');
    const prev = { branch: TASK_NEW, lastReviewedSha: unit.sha, verdict: 'READY' };
    assert.equal(await detectNewCommit('p', cfg, prev), null, 'TASK_OLD is never visited');
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('a dirty workspace on the task branch is a moving target', async () => {
  const { root, cfg } = fixture();
  try {
    fs.writeFileSync(path.join(cfg.sandbox, 'wip.txt'), 'x');
    assert.equal(await detectNewCommit('p', cfg, undefined), null);
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});
