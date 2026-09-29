'use strict';
/**
 * A review never runs tools a task could have rewritten.
 *
 * Every task workspace is a HARDLINK copy of the agent's workspace template
 * (agent/workspaces.py), so a task command that rewrites a dependency file
 * in place -- `printf 'exit 0' > node_modules/.bin/eslint` -- changes the
 * template's copy too. For a day (2026-09-29) the reviewer borrowed that
 * template when live's install could not run on Linux, so a task could
 * choose the linter its own commit was then checked with. Now a live that
 * cannot lend means the review installs its own dependencies, which no
 * task shares.
 *
 * Run: node tests/test_reviewer_template_borrow.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'review-template-borrow-'));
process.env.AGENT_HOME = path.join(tmp, 'home');
process.env.REVIEW_WORKTREE_ROOT = path.join(tmp, 'worktrees');
// The bundle with no agent to delegate to: an install is REFUSED rather than
// run, which is exactly what tells this test the install path was taken.
process.env.TEKTONIX_BUNDLE = '1';
delete process.env.AGENT_SANDBOX_URL;
fs.mkdirSync(process.env.REVIEW_WORKTREE_ROOT, { recursive: true });
const gitEnv = { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' };
process.env.GIT_CONFIG_GLOBAL = '/dev/null';
process.env.GIT_CONFIG_NOSYSTEM = '1';

const { setupWorktree } = require('../services/commit-reviewer/worktree.js');
const g = (cwd, ...args) => execFileSync('git', args, { cwd, env: gitEnv }).toString().trim();

/** A live repo with a Windows install, and a template with a Linux one. */
function project() {
  const live = path.join(tmp, 'live');
  fs.mkdirSync(live);
  g(live, 'init', '-q', '-b', 'main');
  g(live, 'config', 'user.email', 'a@example.com');
  g(live, 'config', 'user.name', 'a');
  fs.writeFileSync(path.join(live, 'package.json'), JSON.stringify({ name: 'p', devDependencies: { eslint: '9' } }));
  fs.writeFileSync(path.join(live, 'package-lock.json'), '{}');
  fs.writeFileSync(path.join(live, '.gitignore'), 'node_modules/\n');
  g(live, 'add', '-A');
  g(live, 'commit', '-qm', 'base');
  const base = g(live, 'rev-parse', 'HEAD');
  fs.writeFileSync(path.join(live, 'index.js'), 'x\n');
  g(live, 'add', '-A');
  g(live, 'commit', '-qm', 'agent: a change that touches no manifest');
  const sha = g(live, 'rev-parse', 'HEAD');
  // Live's install is a Windows one: a .cmd shim in .bin.
  fs.mkdirSync(path.join(live, 'node_modules', '.bin'), { recursive: true });
  fs.writeFileSync(path.join(live, 'node_modules', '.bin', 'eslint.cmd'), '');
  // The template: a Linux install, hardlinked into every task workspace.
  const template = path.join(tmp, 'template');
  fs.mkdirSync(path.join(template, 'node_modules', '.bin'), { recursive: true });
  fs.writeFileSync(path.join(template, 'node_modules', '.bin', 'eslint'), '#!/bin/sh\nexit 0\n');
  return { live, template, base, sha };
}

async function main() {
  const { live, template, base, sha } = project();
  const cfg = { live, sandbox: template, nodeModulesDirs: ['.'] };

  // No manifest changed, and the baseline caller even says "do not install":
  // live still cannot lend, so the install path is taken. With no sandbox
  // to run it in, that path refuses, and the refusal is the proof.
  let refused = null;
  try {
    await setupWorktree('proj', cfg, sha, base, { depsChangedOverride: false });
  } catch (e) {
    refused = e;
  }
  assert.ok(refused, 'the worktree was provisioned without an install');
  assert.match(refused.message, /install/, refused.message);
  assert.match(refused.message, /SETUP: this check runs code the agent wrote/, 'the install went through runAgentCode');

  // And nothing in the worktree points at the template.
  const wt = fs.readdirSync(process.env.REVIEW_WORKTREE_ROOT).map((n) => path.join(process.env.REVIEW_WORKTREE_ROOT, n));
  for (const dir of wt) {
    const nm = path.join(dir, 'node_modules');
    if (!fs.existsSync(nm)) continue;
    assert.ok(!fs.lstatSync(nm).isSymbolicLink() || !fs.realpathSync(nm).startsWith(template),
      `the worktree's node_modules links into the template: ${nm}`);
  }
  console.log('ok - a live install that cannot run on Linux means a fresh install, never the template');

  // A live that CAN lend is still borrowed, with no install at all.
  fs.rmSync(path.join(live, 'node_modules', '.bin', 'eslint.cmd'));
  fs.writeFileSync(path.join(live, 'node_modules', '.bin', 'eslint'), '#!/bin/sh\nexit 0\n');
  const ok = await setupWorktree('proj', cfg, sha, base, { depsChangedOverride: false });
  // The root package has a name, so node_modules is populated entry by entry
  // (each entry a link into live's copy) rather than as one link.
  const linked = path.join(ok.worktreePath, 'node_modules', '.bin');
  assert.ok(fs.lstatSync(linked).isSymbolicLink(), 'live\'s install is borrowed by link');
  assert.equal(fs.realpathSync(linked), fs.realpathSync(path.join(live, 'node_modules', '.bin')));
  assert.deepEqual(ok.setupIssues, []);
  console.log('ok - a live install a Linux check can run is borrowed as before');
}

main()
  .then(() => fs.rmSync(tmp, { recursive: true, force: true }))
  .catch((e) => { console.error(e); process.exit(1); });
