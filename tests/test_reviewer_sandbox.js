'use strict';
/**
 * Where agent-authored code is allowed to run.
 *
 * The reviewer executes each project's configured checks against the agent's
 * worktree. The agent's shell is sandboxed, but its write/edit tools put
 * files in that worktree -- so on a host install, where this service runs as
 * root, a test file the agent wrote was arbitrary code executing as root
 * outside any container. sealedEnv() stops secrets reaching those commands;
 * it does nothing about what they can do once running.
 *
 * These pin the three decisions that close it, because each one is the kind
 * that gets "simplified" later by someone who does not know what it was for:
 * fail closed rather than fall back, leave the bundle alone because it is
 * already contained, and mount what a check needs so containment does not
 * quietly turn every project red.
 *
 * Run: node tests/test_reviewer_sandbox.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sandbox = require('../services/commit-reviewer/sandbox');

let passed = 0;
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}

console.log('reviewer sandbox');

// --- what a check needs in order to behave as it does on the host ---------

function scratchProject() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'revsbx-'));
    const live = path.join(root, 'live');
    const wt = path.join(root, 'work');
    fs.mkdirSync(path.join(live, 'node_modules', '.bin'), { recursive: true });
    fs.mkdirSync(path.join(live, '.git'), { recursive: true });
    fs.mkdirSync(wt, { recursive: true });
    return { root, live, wt };
}

function mountsOf(args) {
    const out = [];
    args.forEach((a, i) => { if (a === '-v') out.push(args[i + 1]); });
    return out;
}

test('the worktree is the working directory', () => {
    const { live, wt } = scratchProject();
    assert.ok(mountsOf(sandbox.mountArgs({ live }, wt)).includes(`${wt}:/workspace`));
});

test('a symlinked node_modules is mounted so the link resolves', () => {
    // The one that actually bit. A symlink is carried into the container
    // faithfully and then dangles, so `npx eslint` dies with "not found"
    // inside while passing on the host.
    const { live, wt } = scratchProject();
    fs.symlinkSync(path.join(live, 'node_modules'), path.join(wt, 'node_modules'));
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(mounts.some(m => m === `${fs.realpathSync(path.join(live, 'node_modules'))}:${fs.realpathSync(path.join(live, 'node_modules'))}:ro`),
        `no mount for the link target: ${mounts.join(' , ')}`);
});

test('a symlink pointing outside the project is refused', () => {
    // The worktree is agent-writable, so the link target is agent-chosen:
    // node_modules -> /root would otherwise mount /root into the container.
    const { root, live, wt } = scratchProject();
    const elsewhere = path.join(root, 'elsewhere');
    fs.mkdirSync(elsewhere, { recursive: true });
    fs.symlinkSync(elsewhere, path.join(wt, 'node_modules'));
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(!mounts.some(m => m.startsWith(elsewhere)), `mounted ${elsewhere}`);
});

test('a real node_modules directory needs no mount of its own', () => {
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(wt, 'node_modules'), { recursive: true });
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.equal(mounts.filter(m => m.includes('node_modules')).length, 0,
        'a real directory is already inside the /workspace mount');
});

test('declared dependency and data dirs are bound read-only from live', () => {
    // A bind mount NESTED in the worktree is not carried into a container by
    // a plain bind of its parent, so these have to be named explicitly.
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(live, 'data'), { recursive: true });
    const mounts = mountsOf(sandbox.mountArgs(
        { live, dependencyDirs: ['node_modules'], readOnlyMounts: ['data'] }, wt));
    assert.ok(mounts.includes(`${path.join(live, 'node_modules')}:/workspace/node_modules:ro`));
    assert.ok(mounts.includes(`${path.join(live, 'data')}:/workspace/data:ro`));
});

test("the live .git is mounted at its own path, since a worktree's .git is a pointer", () => {
    const { live, wt } = scratchProject();
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${live}/.git/worktrees/work\n`);
    assert.ok(mountsOf(sandbox.mountArgs({ live }, wt))
        .includes(`${path.join(live, '.git')}:${path.join(live, '.git')}:ro`));
});

test('a pointer file naming someone else\'s .git is refused', () => {
    // Same reasoning as the symlink: the agent can rewrite this file.
    const { root, live, wt } = scratchProject();
    const other = path.join(root, 'other');
    fs.mkdirSync(path.join(other, '.git'), { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${other}/.git/worktrees/work\n`);
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(!mounts.some(m => m.startsWith(other)), `mounted ${other}`);
});

test('a directory that is not there is skipped rather than mounted empty', () => {
    const { live, wt } = scratchProject();
    const mounts = mountsOf(sandbox.mountArgs({ live, dependencyDirs: ['not-here'] }, wt));
    assert.ok(!mounts.some(m => m.includes('not-here')));
});

// --- the walk that finds those symlinks -----------------------------------

test('nested node_modules links are found, and the walk never descends into one', () => {
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(wt, 'apps', 'api'), { recursive: true });
    fs.symlinkSync(path.join(live, 'node_modules'), path.join(wt, 'apps', 'api', 'node_modules'));
    const found = sandbox.nodeModulesLinks(wt);
    assert.deepStrictEqual(found, [path.join('apps', 'api', 'node_modules')]);
});

test('an unreadable directory does not abort the walk', () => {
    const { wt } = scratchProject();
    assert.deepStrictEqual(sandbox.nodeModulesLinks(path.join(wt, 'nope')), []);
});

test('isInside is not a string prefix test', () => {
    // /home/proj-old must not count as inside /home/proj.
    assert.ok(sandbox.isInside('/home/proj/x', '/home/proj'));
    assert.ok(sandbox.isInside('/home/proj', '/home/proj'));
    assert.ok(!sandbox.isInside('/home/proj-old/x', '/home/proj'));
    assert.ok(!sandbox.isInside('/home/other', '/home/proj'));
});

console.log(`\n${passed} passed`);

// --- per-stack images -----------------------------------------------------
//
// The sandbox image carries Node and Python. agent/provisioning.py detects
// and configures checks for Go, Rust, Ruby, Elixir, Java, PHP and .NET, none
// of which are in it -- so containing checks without this would have turned
// every review red for anyone whose project is not JavaScript.

test('a stack with its own image gets it, with the env that toolchain needs', () => {
    const go = sandbox.imageFor('go');
    assert.ok(go.known);
    assert.match(go.image, /golang/);
    // Not decoration: with no network and a read-only HOME, go writes its
    // cache somewhere or it does not run at all.
    assert.ok(go.env.GOCACHE, 'go needs a writable cache directory');
});

test('no stack means the default image, which is what every project had before', () => {
    const d = sandbox.imageFor(undefined);
    assert.equal(d.known, false);
    assert.equal(d.image, sandbox.IMAGE);
    assert.deepStrictEqual(d.env, {});
});

test('an unknown stack falls back rather than failing', () => {
    // The map can gain entries after a project's config was written.
    // Refusing to run a check because its label is new is worse than running
    // it where it probably works.
    const u = sandbox.imageFor('fortran');
    assert.equal(u.known, false);
    assert.equal(u.image, sandbox.IMAGE);
});

test('every stack in the map names an image and an env', () => {
    for (const [name, entry] of Object.entries(sandbox.STACKS.stacks || {})) {
        assert.ok(entry.image, `${name} has no image`);
        assert.ok(entry.env && typeof entry.env === 'object', `${name} has no env`);
    }
});

test('the map is the same one the Python side reads', () => {
    // Three callers, one list: this file, agent/provisioning.py's stamp at
    // detection, and scripts/verify_stack_checks.py's CI proof. A second
    // inventory is how the review dashboard ended up labelling live models
    // from a list deleted months earlier.
    const onDisk = JSON.parse(fs.readFileSync(
        path.join(__dirname, '..', 'docker', 'stack-images.json'), 'utf8'));
    assert.deepStrictEqual(Object.keys(sandbox.STACKS.stacks).sort(),
                           Object.keys(onDisk.stacks).sort());
});

test('a missing toolchain is recognised from docker\'s own words', () => {
    assert.equal(sandbox.missingTool(
        'docker: Error response from daemon: ... exec: "go": executable file not found in $PATH'), 'go');
    // And not from a program that merely prints something similar --
    // mislabelling a real failure as a setup problem hides a genuine break.
    assert.equal(sandbox.missingTool('FAIL: expected "executable file not found in $PATH"'), null);
    assert.equal(sandbox.missingTool('2 tests failed'), null);
    assert.equal(sandbox.missingTool(''), null);
});
