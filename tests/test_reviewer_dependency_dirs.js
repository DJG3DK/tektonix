'use strict';
/**
 * The review checkout has to be able to RUN the checks it was given, without
 * being able to damage what it borrowed to run them.
 *
 * A git worktree carries what git carries. PHP's vendor/, Elixir's deps/ and
 * a bundled Ruby project's vendor/bundle are all gitignored, so a fresh
 * worktree has none of them and `vendor/bin/phpunit` exits 127 -- which the
 * reviewer reports as a failing check and an operator reads as a broken test
 * suite.
 *
 * The fix is a READ-ONLY bind, not a symlink. A symlink into the live tree is
 * writable, and the code running against it is by definition unreviewed: a
 * test fixture, a compile step or a package's own cache would have written
 * through to production's installed dependencies.
 *
 * Run: node tests/test_reviewer_dependency_dirs.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const {
  materializeDependencyDirs, installChangedDependencies,
} = require('../services/commit-reviewer/reviewer.js');

let passed = 0;
const skipped = [];

// Bind mounts need root. Locally that means the real-mount tests skip, which
// is fine -- but a suite that silently skips its only proof of the
// read-only guarantee is not proof of anything, and this one skipped on
// GitHub for exactly the same reason. CI runs this file under sudo with
// REQUIRE_MOUNT_TESTS=1, which turns a skip into a failure.
const MOUNTS_REQUIRED = process.env.REQUIRE_MOUNT_TESTS === '1';

function cannotMount(why) {
    if (MOUNTS_REQUIRED) {
        throw new Error(
            `REQUIRE_MOUNT_TESTS=1 but the mount did not happen (${why}). `
          + 'This test is the only proof that live\'s dependencies are borrowed read-only; '
          + 'run it as root or unset the variable, but do not let it pass silently.');
    }
    skipped.push(why);
}

// async, and AWAITING fn: `try { return fn() } finally { cleanup }` around an
// async function runs the cleanup the moment fn returns its promise -- which
// deleted the fixture out from under the test that was still using it.
async function test(name, fn) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'revdeps-'));
    const live = path.join(root, 'live');
    const worktree = path.join(root, 'worktree');
    fs.mkdirSync(live, { recursive: true });
    fs.mkdirSync(worktree, { recursive: true });
    try {
        return await fn({ live, worktree });
    } finally {
        // Unmount anything the test bound, so a failure cannot leave a mount
        // pointing into a directory that is about to be deleted.
        for (const rel of ['vendor', 'deps', 'vendor/bundle']) {
            try {
                require('child_process').execFileSync('umount', [path.join(worktree, rel)],
                    { stdio: 'ignore' });
            } catch { /* not mounted */ }
        }
        fs.rmSync(root, { recursive: true, force: true });
    }
}

function ok(name) {
    passed++;
    console.log(`  ok  ${name}`);
}

console.log('reviewer dependency dirs');

async function main() {
  await test('vendor is bound from live and the check command resolves', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), '#!/bin/sh\n');

    const { mounted, issues } = await materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree);

    if (!mounted.length) {
        cannotMount('vendor bind mount (needs root)');
        return;
    }
    assert.deepEqual(mounted, ['vendor']);
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'bin', 'phpunit')),
        'the check command must resolve inside the worktree');
    ok('vendor is bound from live and the check command resolves');
  });

  await test('what is bound cannot be written through to live', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor', 'bin'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), 'original\n');

    const { mounted } = await materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);
    if (!mounted.length) {
        cannotMount('read-only remount (needs root)');
        return;
    }

    assert.throws(
        () => fs.writeFileSync(path.join(worktree, 'vendor', 'bin', 'phpunit'), 'rewritten\n'),
        /EROFS|EACCES|EPERM/,
        'an unreviewed branch must not be able to edit production\'s installed dependencies');
    assert.equal(fs.readFileSync(path.join(live, 'vendor', 'bin', 'phpunit'), 'utf8'), 'original\n');
    ok('what is bound cannot be written through to live');
  });

  await test('a directory the live checkout does not have is reported, not fatal',
    async ({ live, worktree }) => {
      const said = [];
      const { mounted, issues } = await materializeDependencyDirs(
          { live, dependencyDirs: ['vendor'] }, worktree, { log: (m) => said.push(m) });
      assert.deepEqual(mounted, []);
      assert.deepEqual(issues, []);
      assert.match(said.join('\n'), /not present on the live checkout/);
      ok('a directory the live checkout does not have is reported, not fatal');
    });

  await test('a branch that brought its own copy keeps it', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'from-live'), '');
    fs.mkdirSync(path.join(worktree, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(worktree, 'vendor', 'from-branch'), '');

    const { mounted } = await materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, worktree);

    assert.deepEqual(mounted, [], 'the branch wins');
    assert.ok(fs.existsSync(path.join(worktree, 'vendor', 'from-branch')));
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor', 'from-live')));
    ok('a branch that brought its own copy keeps it');
  });

  await test('a project with no dependency dirs is untouched', async ({ live, worktree }) => {
    assert.deepEqual((await materializeDependencyDirs({ live }, worktree)).mounted, []);
    assert.deepEqual((await materializeDependencyDirs({ live, dependencyDirs: [] }, worktree)).mounted, []);
    assert.deepEqual(fs.readdirSync(worktree), []);
    ok('a project with no dependency dirs is untouched');
  });

  // ---- the mount sequence, without needing root -----------------------
  //
  // The tests above prove the guarantee by writing through a real mount and
  // requiring EROFS. These prove the reviewer ASKS for the right thing, on
  // any machine: a bind, then a read-only remount, and an unmount rather
  // than a writable mount left behind if the remount fails.

  await test('a bind is always followed by a read-only remount', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    const calls = [];
    const fakeRun = async (cmd, args) => { calls.push([cmd, ...args.slice(0, 2)]); return { ok: true, output: '' }; };

    const { mounted } = await materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree, { run: fakeRun });

    assert.deepEqual(mounted, ['vendor']);
    assert.equal(calls[0][0], 'mount');
    assert.equal(calls[0][1], '--bind');
    assert.deepEqual(calls[1].slice(0, 2), ['mount', '-o'], 'the remount must follow the bind');
    assert.match(calls[1][2], /remount,ro,bind/);
    ok('a bind is always followed by a read-only remount');
  });

  await test('a failed read-only remount unmounts rather than leaving it writable',
    async ({ live, worktree }) => {
      fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
      const calls = [];
      const fakeRun = async (cmd, args) => {
        calls.push(cmd);
        const remounting = cmd === 'mount' && args[0] === '-o';
        return remounting ? { ok: false, output: 'remount refused' } : { ok: true, output: '' };
      };

      const { mounted, issues } = await materializeDependencyDirs(
          { live, dependencyDirs: ['vendor'] }, worktree, { run: fakeRun });

      assert.deepEqual(mounted, [], 'nothing may be reported as mounted');
      assert.equal(issues.length, 1);
      assert.match(issues[0].output, /unmounted rather than exposing/);
      assert.equal(calls.at(-1), 'umount', 'the writable bind must be undone');
      ok('a failed read-only remount unmounts rather than leaving it writable');
    });

  await test('a failed bind is reported and nothing further is attempted',
    async ({ live, worktree }) => {
      fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
      const calls = [];
      const fakeRun = async (cmd) => { calls.push(cmd); return { ok: false, output: 'no permission' }; };

      const { mounted, issues } = await materializeDependencyDirs(
          { live, dependencyDirs: ['vendor'] }, worktree, { run: fakeRun });

      assert.deepEqual(mounted, []);
      assert.equal(issues.length, 1);
      assert.deepEqual(calls, ['mount'], 'no remount, no unmount of something never mounted');
      ok('a failed bind is reported and nothing further is attempted');
    });

  // ---- installChangedDependencies -------------------------------------
  //
  // A branch that edits its manifest must not be judged against live's
  // installed dependencies. The npm path has always reinstalled on a
  // lockfile change; these two had no equivalent.

  await test('an unchanged manifest installs nothing', async ({ live, worktree }) => {
    const { installed, issues } = await installChangedDependencies(
        { live, dependencyDirs: ['vendor', 'deps'] }, worktree, 'src/App.php\nREADME.md\n');
    assert.deepEqual(installed, []);
    assert.deepEqual(issues, []);
    ok('an unchanged manifest installs nothing');
  });

  await test('a changed composer.json is installed rather than borrowed',
    async ({ live, worktree }) => {
      const said = [];
      const { installed, issues } = await installChangedDependencies(
          { live, dependencyDirs: ['vendor'] }, worktree, 'composer.json\nsrc/App.php\n',
          (m) => said.push(m));
      // composer is not installed on this host, so the install fails -- and
      // failing loudly as a setup issue is the correct outcome. What matters
      // is that it was ATTEMPTED and that vendor is not then borrowed.
      assert.ok(installed.length === 1 || issues.length === 1,
          'a changed manifest must either install or report why it could not');
      assert.match(said.join('\n'), /composer\.json\/lock changed/);
      ok('a changed composer.json is installed rather than borrowed');
    });

  await test('a changed mix.exs triggers deps.get', async ({ live, worktree }) => {
    const said = [];
    const { installed, issues } = await installChangedDependencies(
        { live, dependencyDirs: ['deps'] }, worktree, 'mix.lock\n', (m) => said.push(m));
    assert.ok(installed.length === 1 || issues.length === 1);
    assert.match(said.join('\n'), /mix\.exs\/lock changed/);
    ok('a changed mix.exs triggers deps.get');
  });

  await test('a stack that declares no such directory is never installed for',
    async ({ live, worktree }) => {
      const { installed, issues } = await installChangedDependencies(
          { live, dependencyDirs: [] }, worktree, 'composer.json\nmix.lock\n');
      assert.deepEqual(installed, []);
      assert.deepEqual(issues, []);
      ok('a stack that declares no such directory is never installed for');
    });

  await test('a changed Gemfile refuses the borrow instead of testing old gems',
    async ({ live, worktree }) => {
      // bundle install builds native extensions -- code execution on an
      // unreviewed branch, with no --ignore-scripts to disable it. So the
      // branch gets neither an install nor live's gems, and the reason is
      // recorded rather than the suite passing against the wrong ones.
      const { installed, issues } = await installChangedDependencies(
          { live, dependencyDirs: ['vendor/bundle'] }, worktree, 'Gemfile.lock\nlib/a.rb\n');
      assert.deepEqual(installed, ['vendor/bundle'], 'marked so the borrow is skipped');
      assert.equal(issues.length, 1);
      assert.match(issues[0].output, /native extensions/);
      assert.match(issues[0].output, /not borrowed/);
      ok('a changed Gemfile refuses the borrow instead of testing old gems');
    });

  await test('an unchanged Gemfile borrows the bundle as usual', async ({ live, worktree }) => {
    const { installed, issues } = await installChangedDependencies(
        { live, dependencyDirs: ['vendor/bundle'] }, worktree, 'lib/a.rb\n');
    assert.deepEqual(installed, []);
    assert.deepEqual(issues, []);
    ok('an unchanged Gemfile borrows the bundle as usual');
  });

  await test('what was installed fresh is not then mounted over', async ({ live, worktree }) => {
    fs.mkdirSync(path.join(live, 'vendor'), { recursive: true });
    fs.writeFileSync(path.join(live, 'vendor', 'from-live'), '');
    const { mounted } = await materializeDependencyDirs(
        { live, dependencyDirs: ['vendor'] }, worktree, { skip: ['vendor'] });
    assert.deepEqual(mounted, []);
    assert.ok(!fs.existsSync(path.join(worktree, 'vendor')),
        "the branch's own install must not be shadowed by live's");
    ok('what was installed fresh is not then mounted over');
  });

  console.log(`\n${passed} passed`);
  for (const s of skipped) console.log(`  SKIPPED (needs root): ${s}`);
  if (skipped.length) {
    console.log('  -> run as root, or in CI where REQUIRE_MOUNT_TESTS=1 makes a skip a failure');
  }
}

main().catch((e) => { console.error(e); process.exit(1); });


test('node_modules comes from live or from nowhere: a Windows install is passed over, the agent\'s template is never borrowed', () => {
    // The template was the second candidate for a day (2026-09-29). Every
    // task workspace hardlinks it, so a task could rewrite the linter the
    // review then ran; now a live that cannot lend means a fresh install.
    const { nodeModulesSource, foreignInstall, unborrowable } = require('../services/commit-reviewer/node-modules-source.js');
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'nm-src-'));
    try {
        const live = path.join(root, 'live'); const tpl = path.join(root, 'tpl');
        fs.mkdirSync(path.join(tpl, 'node_modules', '@esbuild', 'linux-x64'), { recursive: true });
        assert.equal(nodeModulesSource({ live, sandbox: tpl }, '.'), null, 'live has no install: nothing to borrow');
        assert.deepEqual(unborrowable({ live, sandbox: tpl, nodeModulesDirs: ['.', 'web'] }), ['.', 'web']);
        fs.mkdirSync(path.join(live, 'node_modules', '.bin'), { recursive: true });
        fs.writeFileSync(path.join(live, 'node_modules', '.bin', 'vitest.cmd'), '');
        assert.equal(foreignInstall(path.join(live, 'node_modules')), true);
        assert.equal(nodeModulesSource({ live, sandbox: tpl }, '.'), null, 'a Windows install is not usable, and the template is not an answer');
        fs.rmSync(path.join(live, 'node_modules', '.bin', 'vitest.cmd'));
        fs.mkdirSync(path.join(live, 'node_modules', '@rollup', 'rollup-linux-x64-gnu'), { recursive: true });
        assert.equal(nodeModulesSource({ live, sandbox: tpl }, '.')?.which, 'live', 'a Linux install in live is borrowed');
        assert.deepEqual(unborrowable({ live, sandbox: tpl, nodeModulesDirs: ['.'] }), []);
        assert.equal(nodeModulesSource({ live: path.join(root, 'none') }, '.'), null);
    } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

test('in the bundle the reviewer does not bind-mount in its own container: the agent mounts into the check container', async () => {
    const { materializeDependencyDirs, delegated } = require('../services/commit-reviewer/worktree.js');
    const saved = [process.env.TEKTONIX_BUNDLE, process.env.AGENT_SANDBOX_URL];
    process.env.TEKTONIX_BUNDLE = '1'; process.env.AGENT_SANDBOX_URL = 'http://agent:8100';
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'deleg-'));
    try {
        assert.equal(delegated(), true);
        const live = path.join(root, 'live'); const wt = path.join(root, 'wt');
        fs.mkdirSync(path.join(live, 'vendor'), { recursive: true }); fs.mkdirSync(wt);
        const out = await materializeDependencyDirs({ live, dependencyDirs: ['vendor'] }, wt, {});
        assert.deepEqual(out.issues, [], 'no mount was attempted, so no permission error');
        assert.deepEqual(out.mounted, ['vendor']);
    } finally {
        fs.rmSync(root, { recursive: true, force: true });
        if (saved[0] === undefined) delete process.env.TEKTONIX_BUNDLE; else process.env.TEKTONIX_BUNDLE = saved[0];
        if (saved[1] === undefined) delete process.env.AGENT_SANDBOX_URL; else process.env.AGENT_SANDBOX_URL = saved[1];
    }
});


test('a workspace-internal package is linked relative to the worktree, so it resolves at /workspace too', () => {
    const { relativeLink } = require('../services/commit-reviewer/worktree.js');
    const wt = '/home/agent/worktrees/proj-abc';
    const link = relativeLink(`${wt}/apps/admin/node_modules/@scope/shared`, `${wt}/packages/shared`);
    assert.equal(link, '../../../../packages/shared');
    assert.equal(require('path').resolve('/workspace/apps/admin/node_modules/@scope', link), '/workspace/packages/shared');
});
