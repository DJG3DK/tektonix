'use strict';
/**
 * The reviewer's project map is read fresh, never frozen at startup.
 *
 * The incident behind it (2026-09-16): reviewer.js bound
 * `const PROJECTS = loadProjects(...)` once at require time, so a project
 * created from the dashboard did not exist for the reviewer until pm2
 * restarted it -- the poll never looked at its branches, /check answered 404,
 * and the agent's wait_for_review timed out against a verdict that could not
 * arrive. loadProjects itself already re-read the file per call
 * (tests/test_projects_config_merge.js); the service just never called it
 * again. This test pins the service side.
 *
 * Run: node tests/test_reviewer_projects_reload.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

// The file's location is read once by services/shared/projects-config.js at
// require time, so it must be in the environment BEFORE the reviewer loads.
const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'reviewer-reload-'));
const file = path.join(dir, 'projects.json');
fs.writeFileSync(file, JSON.stringify({
    projects: { one: { live: '/one', sandbox: '/s/one', review: { checks: [{ name: 'lint' }] } } },
}));
process.env.AGENT_PROJECTS_JSON = file;

const reviewer = require('../services/commit-reviewer/reviewer.js');

let passed = 0;
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}

function writeProjects(mutate) {
    const data = JSON.parse(fs.readFileSync(file, 'utf8'));
    mutate(data.projects);
    fs.writeFileSync(file, JSON.stringify(data));
}

console.log('reviewer project map reload');

try {
    test('the map the service starts with comes from projects.json', () => {
        const projects = reviewer.currentProjects();
        assert.ok('one' in projects);
        assert.deepStrictEqual(projects.one.checks.map((c) => c.name), ['lint']);
    });

    test('a project added to projects.json is visible on the next call, without a restart', () => {
        writeProjects((p) => { p.two = { live: '/two', sandbox: '/s/two' }; });
        const projects = reviewer.currentProjects();
        assert.ok('two' in projects, 'the new project must appear with no restart');
        assert.strictEqual(projects.two.live, '/two');
    });

    test('checks written after the fact are picked up the same way', () => {
        // The post-merge autodetect (agent/project_checks.py) writes
        // review.checks into an entry that had none; the next poll must run
        // them.
        assert.strictEqual(reviewer.currentProjects().two.checks, undefined);
        writeProjects((p) => { p.two.review = { checks: [{ name: 'test' }, { name: 'typecheck' }] }; });
        assert.deepStrictEqual(reviewer.currentProjects().two.checks.map((c) => c.name), ['test', 'typecheck']);
    });

    test('a project removed from projects.json stops being polled', () => {
        writeProjects((p) => { delete p.one; });
        assert.ok(!('one' in reviewer.currentProjects()));
    });

    test('the legacy PROJECTS export answers fresh too, not a startup snapshot', () => {
        writeProjects((p) => { p.three = { live: '/three', sandbox: '/s/three' }; });
        assert.ok('three' in reviewer.PROJECTS);
    });
} finally {
    fs.rmSync(dir, { recursive: true, force: true });
}

console.log(`\n${passed} passed`);
