'use strict';
/**
 * The review services' git never runs a hook.
 *
 * The tree they run git in is agent-authored. A project that uses husky has
 * `core.hooksPath=.husky` in its own .git/config, and .husky/* is TRACKED --
 * so an agent commit that adds .husky/post-checkout had the reviewer run it,
 * as the reviewer's user, the moment `git worktree add` checked the commit
 * out for review. agent-review's `merge --ff-only` into live ran a
 * post-merge hook the same way.
 *
 * Run: node tests/test_review_services_git_hooks.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'review-hooks-'));
process.env.REVIEW_WORKTREE_ROOT = path.join(tmp, 'worktrees');
fs.mkdirSync(process.env.REVIEW_WORKTREE_ROOT);
const gitEnv = { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' };
process.env.GIT_CONFIG_GLOBAL = '/dev/null';
process.env.GIT_CONFIG_NOSYSTEM = '1';

const { setupWorktree, cleanupWorktree } = require('../services/commit-reviewer/reviewer.js');

const g = (cwd, ...args) => execFileSync('git', args, { cwd, env: gitEnv }).toString().trim();

function huskyRepo(marker) {
  const live = path.join(tmp, 'live');
  fs.mkdirSync(live);
  g(live, 'init', '-q', '-b', 'main');
  g(live, 'config', 'user.email', 'a@example.com');
  g(live, 'config', 'user.name', 'a');
  g(live, 'config', 'core.hooksPath', '.husky');
  fs.writeFileSync(path.join(live, 'README.md'), 'hi\n');
  g(live, 'add', '-A');
  g(live, 'commit', '-qm', 'base');
  const base = g(live, 'rev-parse', 'HEAD');
  // The agent's commit: a hook, tracked like any other file.
  fs.mkdirSync(path.join(live, '.husky'));
  for (const hook of ['post-checkout', 'post-merge']) {
    const p = path.join(live, '.husky', hook);
    fs.writeFileSync(p, `#!/bin/sh\necho ran >> "${marker}"\n`);
    fs.chmodSync(p, 0o755);
  }
  // Committed with hooks off, so the setup itself does not trip the marker.
  g(live, '-c', 'core.hooksPath=/dev/null', 'add', '-A');
  g(live, '-c', 'core.hooksPath=/dev/null', 'commit', '-qm', 'agent: add a hook');
  const sha = g(live, 'rev-parse', 'HEAD');
  return { live, base, sha };
}

async function main() {
  const marker = path.join(tmp, 'hook-ran');
  const { live, base, sha } = huskyRepo(marker);

  // Sanity: this repo does run the hook for a plain git.
  const probe = path.join(tmp, 'probe');
  g(live, 'worktree', 'add', '--detach', probe, sha);
  assert.ok(fs.existsSync(marker), 'the fixture hook must fire for an unguarded git, or this test proves nothing');
  fs.rmSync(marker);
  g(live, 'worktree', 'remove', '--force', probe);

  const cfg = { live, nodeModulesDirs: [] };
  const wt = await setupWorktree('proj', cfg, sha, base, { depsChangedOverride: false });
  const worktreePath = typeof wt === 'string' ? wt : wt.worktreePath;
  assert.ok(fs.existsSync(path.join(worktreePath, '.husky', 'post-checkout')), 'the commit was checked out');
  assert.ok(!fs.existsSync(marker), 'the reviewer ran the agent\'s post-checkout hook');
  await cleanupWorktree(cfg, worktreePath);
  assert.ok(!fs.existsSync(marker), 'worktree removal ran a hook');
  console.log('ok - the reviewer checks a commit out without running its hooks');

  const src = fs.readFileSync(path.join(__dirname, '..', 'services', 'agent-review', 'server.js'), 'utf8');
  assert.match(src, /const git = \(cwd, args\) => run\('git', \[\.\.\.GIT_SAFE, \.\.\.args\], cwd\);/);
  assert.match(src, /core\.hooksPath=\/dev\/null/);
  assert.doesNotMatch(src, /run\('git', (?!\[\.\.\.GIT_SAFE)/, 'a git call in agent-review bypasses GIT_SAFE');
  console.log('ok - agent-review runs every git call with hooks off');
}

main()
  .then(() => fs.rmSync(tmp, { recursive: true, force: true }))
  .catch((e) => { console.error(e); process.exit(1); });
