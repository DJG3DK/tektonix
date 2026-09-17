'use strict';
/**
 * A project entry with no `review` and no `deploy` block must not crash the
 * two services that read it.
 *
 * The incident behind it (2026-09-17): "create a project" makes an EMPTY git
 * repo -- a README and a .gitignore, no manifest, no .env, no pm2 app. So
 * detection proposes nothing, and provisioning.config_from_choices writes
 * `{live, sandbox}` with neither section. That is now the normal shape of a
 * brand-new project, and both services iterated those keys unguarded:
 *
 *   commit-reviewer/reviewer.js  `for (const rel of cfg.secretFiles)`
 *       -> TypeError inside setupWorktree, caught as "review failed with an
 *          internal error", NO verdict written, and the agent's
 *          wait_for_review polls until it times out and the task escalates.
 *
 *   agent-review/server.js       `for (const step of p.build)`
 *       -> 500 {stage: 'build'}, which verify_and_ship reads as a compile
 *          error the agent should fix -- so the agent goes round the work
 *          loop trying to fix a TypeError in the deploy service.
 *
 * The three live projects never hit either one, because
 * builtin-projects.local.js supplies those keys for all of them. Only a
 * dashboard-created project has no built-in entry, which is why this landed
 * with the feature that creates them.
 *
 * Run: node tests/test_bare_project_entry.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { loadProjects } = require('../services/shared/projects-config.js');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bare-entry-'));
const file = path.join(dir, 'projects.json');
// Exactly what config_from_choices writes for a project created from nothing:
// no `review` key, no `deploy` key. tests/test_project_create.py asserts this
// same shape from the Python side.
fs.writeFileSync(file, JSON.stringify({
    projects: { fresh: { live: '/home/fresh', sandbox: '/home/agent-workspaces/fresh' } },
}));

let passed = 0;
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}

const review = loadProjects({}, { section: 'review', file }).fresh;
const deploy = loadProjects({}, { section: 'deploy', file }).fresh;

test('a bare entry really does lack every optional key', () => {
    // If this ever fails the rest of the file is testing nothing: it would
    // mean loadProjects started defaulting the keys, and the guards below
    // would pass for the wrong reason.
    assert.ok(review.live && review.sandbox, 'live/sandbox still come through');
    for (const key of ['secretFiles', 'readOnlyMounts', 'nodeModulesDirs', 'dependencyDirs', 'checks']) {
        assert.strictEqual(review[key], undefined, `review.${key} should be absent`);
    }
    for (const key of ['build', 'pm2Apps']) {
        assert.strictEqual(deploy[key], undefined, `deploy.${key} should be absent`);
    }
});

test('every list the reviewer iterates survives being absent', () => {
    // The guard the reviewer applies, asserted directly: `|| []` on each.
    for (const key of ['secretFiles', 'readOnlyMounts', 'nodeModulesDirs', 'dependencyDirs', 'checks']) {
        assert.doesNotThrow(() => {
            for (const _ of review[key] || []) { /* the loop body is not the point */ }
        }, `review.${key}`);
    }
});

test('the reviewer source has no unguarded cfg.<list> loop left', () => {
    // Belt and braces: the runtime check above only covers keys this test
    // knows about. This one catches a NEW unguarded loop over cfg.
    const src = fs.readFileSync(path.join(__dirname, '..', 'services', 'commit-reviewer', 'reviewer.js'), 'utf8');
    const unguarded = [...src.matchAll(/for \(const \w+ of (cfg\.\w+)\)/g)].map((m) => m[1]);
    assert.deepStrictEqual(unguarded, [], `unguarded loops: ${unguarded.join(', ')}`);
});

test('the deploy service has no unguarded p.<list> loop left', () => {
    const src = fs.readFileSync(path.join(__dirname, '..', 'services', 'agent-review', 'server.js'), 'utf8');
    const unguarded = [...src.matchAll(/for \(const \w+ of (p\.\w+)\)/g)].map((m) => m[1]);
    assert.deepStrictEqual(unguarded, [], `unguarded loops: ${unguarded.join(', ')}`);
});

test('a bare entry deploys as a no-op instead of throwing', () => {
    const built = [];
    assert.doesNotThrow(() => {
        for (const step of deploy.build || []) built.push(step);
        for (const _ of deploy.pm2Apps || []) { /* nothing to restart */ }
    });
    assert.deepStrictEqual(built, [], 'nothing to build');
    assert.deepStrictEqual(deploy.pm2Apps || [], [], 'nothing to restart');
});

fs.rmSync(dir, { recursive: true, force: true });
console.log(`\n${passed} passed`);
