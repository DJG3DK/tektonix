'use strict';
/**
 * What the review puts INTO its checkout never follows a link the branch
 * committed.
 *
 * The secret copy runs in the reviewer's own process, not in the sandbox.
 * With a committed `.env.review -> <live>/.env`, copyFileSync followed the
 * link and wrote review credentials over live's .env; a committed directory
 * link did the same one level up (2026-09-29). The generated-code link and
 * the bind mounts land on committed paths the same way. Every one of them
 * is refused with a failed setup check that names the link, and the file
 * outside the worktree is untouched.
 *
 * Run: node tests/test_review_services_secret_copy.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFileSync } = require('child_process');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'review-secret-copy-'));
// The reviewer reads its review-only credentials from under AGENT_HOME, and
// runs outside the bundle here so the host paths (mounts, links) are taken.
process.env.AGENT_HOME = path.join(tmp, 'home');
process.env.REVIEW_WORKTREE_ROOT = path.join(tmp, 'worktrees');
delete process.env.TEKTONIX_BUNDLE;
delete process.env.AGENT_SANDBOX_URL;
fs.mkdirSync(process.env.REVIEW_WORKTREE_ROOT, { recursive: true });
const gitEnv = { ...process.env, GIT_CONFIG_GLOBAL: '/dev/null', GIT_CONFIG_NOSYSTEM: '1' };
process.env.GIT_CONFIG_GLOBAL = '/dev/null';
process.env.GIT_CONFIG_NOSYSTEM = '1';

const worktree = require('../services/commit-reviewer/worktree.js');
const { setupWorktree, cleanupWorktree, committedInTheWay, REVIEW_SECRETS_ROOT } = worktree;

const g = (cwd, ...args) => execFileSync('git', args, { cwd, env: gitEnv }).toString().trim();

/** A live repo whose agent branch plants links where the review writes. */
function repoWithLinks(outside) {
  const live = path.join(tmp, 'live');
  fs.mkdirSync(live);
  g(live, 'init', '-q', '-b', 'main');
  g(live, 'config', 'user.email', 'a@example.com');
  g(live, 'config', 'user.name', 'a');
  fs.writeFileSync(path.join(live, 'README.md'), 'hi\n');
  fs.writeFileSync(path.join(live, '.gitignore'), 'data/\ngenerated-link/\n');
  g(live, 'add', '-A');
  g(live, 'commit', '-qm', 'base');
  const base = g(live, 'rev-parse', 'HEAD');
  // The agent's branch: a link at the secret's path, a linked parent
  // directory for a second secret, a linked data directory, and a link
  // where generated code goes. Committed with --force since two of the
  // names are ignored on live, which is what makes them the review's to
  // provide.
  g(live, 'checkout', '-qb', 'agent/links');
  fs.symlinkSync(path.join(outside, 'live.env'), path.join(live, '.env.review'));
  fs.symlinkSync(outside, path.join(live, 'config'));
  fs.symlinkSync('/etc', path.join(live, 'data'));
  fs.symlinkSync(outside, path.join(live, 'generated-link'));
  g(live, 'add', '-A', '--force');
  g(live, 'commit', '-qm', 'agent: links everywhere');
  const sha = g(live, 'rev-parse', 'HEAD');
  g(live, 'checkout', '-q', 'main');
  // Live's own copies of what the review borrows: the data the tests read
  // and the generated client.
  fs.mkdirSync(path.join(live, 'data'));
  fs.mkdirSync(path.join(live, 'generated-link'));
  fs.writeFileSync(path.join(live, 'generated-link', 'client.js'), '// live generated\n');
  return { live, base, sha };
}

async function main() {
  const outside = path.join(tmp, 'outside');
  fs.mkdirSync(outside);
  fs.writeFileSync(path.join(outside, 'live.env'), 'LIVE_SECRET=production\n');
  fs.writeFileSync(path.join(outside, '.env.review'), 'LIVE_SECRET=production-nested\n');
  const { live, base, sha } = repoWithLinks(outside);

  for (const rel of ['.env.review', 'config/.env.review']) {
    const src = path.join(REVIEW_SECRETS_ROOT, 'proj', rel);
    fs.mkdirSync(path.dirname(src), { recursive: true });
    fs.writeFileSync(src, 'REVIEW_ONLY=1\n');
  }

  const cfg = {
    live, nodeModulesDirs: [],
    secretFiles: ['.env.review', 'config/.env.review'],
    readOnlyMounts: ['data'],
    generated: [{ dir: 'generated-link', schemaFile: 'schema.prisma', regenerate: { dir: '.', cmd: 'true', args: [] } }],
  };

  const wt = await setupWorktree('proj', cfg, sha, base, { depsChangedOverride: false });
  try {
    const issues = wt.setupIssues;
    const named = (name) => issues.find((i) => i.name === name);

    // The file outside the worktree is exactly what it was.
    assert.equal(fs.readFileSync(path.join(outside, 'live.env'), 'utf8'), 'LIVE_SECRET=production\n',
      'the review credential was written over the file the link pointed at');
    assert.equal(fs.readFileSync(path.join(outside, '.env.review'), 'utf8'), 'LIVE_SECRET=production-nested\n',
      'the review credential was written through the linked parent directory');
    assert.ok(!fs.existsSync(path.join(outside, 'REVIEW_ONLY')));

    // And each refusal is a failed check that says why.
    for (const rel of ['.env.review', 'config/.env.review']) {
      const issue = named(`review-secret (${rel})`);
      assert.ok(issue && issue.ok === false, `no failing review-secret (${rel}) check: ${JSON.stringify(issues)}`);
      assert.match(issue.output, /symlink/, issue.output);
    }
    const mount = named('mount (data)');
    assert.ok(mount && mount.ok === false, `no failing mount (data) check: ${JSON.stringify(issues)}`);
    assert.match(mount.output, /symlink/, 'the mount was attempted rather than refused: ' + mount.output);
    const gen = named('generate (generated-link)');
    assert.ok(gen && gen.ok === false, `no failing generate check: ${JSON.stringify(issues)}`);
    assert.match(gen.output, /symlink/, gen.output);
    console.log('ok - a committed link at a secret, mount or generated path is refused, and the target is untouched');
  } finally {
    await cleanupWorktree(cfg, wt.worktreePath);
  }

  // A worktree with nothing in the way gets its secret, and a second copy
  // does not overwrite what the first wrote.
  const clean = fs.mkdtempSync(path.join(tmp, 'clean-'));
  assert.equal(committedInTheWay(clean, 'sub/dir/.env.review'), null);
  fs.mkdirSync(path.join(clean, 'sub'));
  fs.writeFileSync(path.join(clean, 'sub', 'file'), '');
  assert.match(committedInTheWay(clean, 'sub/file/.env.review'), /not a directory/);
  assert.match(committedInTheWay(clean, 'sub/file'), /already exists/);
  assert.equal(committedInTheWay(clean, 'sub', { existingDir: true }), null, 'a mount may go over the branch\'s own directory');
  assert.match(committedInTheWay(clean, '../elsewhere'), /leaves the worktree/);
  assert.match(committedInTheWay(clean, '/abs'), /not a relative path/);
  console.log('ok - committedInTheWay accepts only real directories on the way and an absent final entry');
}

main()
  .then(() => fs.rmSync(tmp, { recursive: true, force: true }))
  .catch((e) => { console.error(e); process.exit(1); });
