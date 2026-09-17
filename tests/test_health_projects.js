'use strict';
/**
 * Guards the `projects` check both health routes report
 * (healthProjectsCheck in services/shared/projects-config.js).
 *
 * The incident behind it: the merged project map always carries the built-in
 * repos, so a fresh install -- nothing onboarded, nothing on disk -- looked
 * identical to three broken checkouts and every health route answered 503 on
 * day one. An operator who is told to ignore the health check on their first
 * afternoon will still be ignoring it on the day it means something.
 *
 * Run: node tests/test_health_projects.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { healthProjectsCheck } = require('../services/shared/projects-config');

let passed = 0;
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}

function withProjectsFile(contents, fn) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'healthproj-'));
    const file = path.join(dir, 'projects.json');
    fs.writeFileSync(file, typeof contents === 'string' ? contents : JSON.stringify(contents));
    try { return fn(file); } finally { fs.rmSync(dir, { recursive: true, force: true }); }
}

const BUILTINS = {
    'storefront': { live: '/home/storefront' },
    'webapp': { live: '/home/webapp' },
    'brochure': { live: '/home/brochure' },
};
const onDisk = (paths) => (p) => paths.includes(p);

console.log('health projects check');

test('a fresh install is healthy: built-ins exist, nothing is onboarded, nothing is on disk', () => {
    withProjectsFile({ projects: {} }, (file) => {
        const c = healthProjectsCheck(BUILTINS, { file, exists: onDisk([]) });
        assert.strictEqual(c.ok, true);
        assert.strictEqual(c.count, 0);
        assert.match(c.detail, /fresh install/);
    });
});

test('a missing projects.json is a fresh install too, not a fault', () => {
    const c = healthProjectsCheck(BUILTINS, {
        file: path.join(os.tmpdir(), 'no-such-projects-file.json'),
        exists: onDisk([]),
    });
    assert.strictEqual(c.ok, true);
    assert.strictEqual(c.count, 0);
});

test('an onboarded project with no checkout is a real fault', () => {
    withProjectsFile({ projects: { ghost: { live: '/home/ghost' } } }, (file) => {
        const c = healthProjectsCheck({ ...BUILTINS, ghost: { live: '/home/ghost' } },
            { file, exists: onDisk([]) });
        assert.strictEqual(c.ok, false);
        assert.match(c.detail, /live checkout missing for: ghost/);
    });
});

test('the fault names every missing project, not just the first', () => {
    withProjectsFile({ projects: { a: { live: '/a' }, b: { live: '/b' } } }, (file) => {
        const c = healthProjectsCheck({ a: { live: '/a' }, b: { live: '/b' } },
            { file, exists: onDisk([]) });
        assert.strictEqual(c.ok, false);
        assert.match(c.detail, /a/);
        assert.match(c.detail, /b/);
    });
});

test('an onboarded project that is on disk is healthy and counted', () => {
    withProjectsFile({ projects: { 'webapp': { live: '/home/webapp' } } }, (file) => {
        const c = healthProjectsCheck(BUILTINS, { file, exists: onDisk(['/home/webapp']) });
        assert.strictEqual(c.ok, true);
        assert.strictEqual(c.count, 1);
    });
});

test('built-ins this host never onboarded are named, but do not fail the check', () => {
    withProjectsFile({ projects: { 'webapp': { live: '/home/webapp' } } }, (file) => {
        const c = healthProjectsCheck(BUILTINS, { file, exists: onDisk(['/home/webapp']) });
        assert.strictEqual(c.ok, true);
        assert.match(c.detail, /not onboarded here/);
        assert.match(c.detail, /storefront/);
    });
});

test('a fully onboarded, fully present deployment reports no detail at all', () => {
    const all = { projects: Object.fromEntries(Object.entries(BUILTINS).map(([n, c]) => [n, { live: c.live }])) };
    withProjectsFile(all, (file) => {
        const c = healthProjectsCheck(BUILTINS, { file, exists: onDisk(Object.values(BUILTINS).map((v) => v.live)) });
        assert.strictEqual(c.ok, true);
        assert.strictEqual(c.count, 3);
        assert.strictEqual(c.detail, null);
    });
});

test('an onboarded name with no entry in the merged map cannot fail the check', () => {
    // loadProjects builds the map from the same file, so this only happens
    // mid-edit; it must not throw on projects[name].live.
    withProjectsFile({ projects: { stranger: { live: '/nowhere' } } }, (file) => {
        const c = healthProjectsCheck(BUILTINS, { file, exists: onDisk([]) });
        assert.strictEqual(c.ok, true);
    });
});

console.log(`\n${passed} passed`);
