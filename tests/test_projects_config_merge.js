'use strict';
/**
 * Guards the shared project-config merge (services/shared/projects-config.js).
 *
 * The behaviour that matters most: the three original projects' hand-tuned
 * entries must survive the merge untouched. They encode incidents that
 * generated config never could -- a suite that POSTed live trade orders, a
 * Prisma client needing regeneration before typecheck, a prerender step whose
 * absence 404'd a production catalog. A merge that lets projects.json
 * overwrite them would quietly undo all of it.
 *
 * Run: node tests/test_projects_config_merge.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const { loadProjects, readProjectsJson } = require('../services/shared/projects-config');

let passed = 0;
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}

function withProjectsFile(contents, fn) {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'projcfg-'));
    const file = path.join(dir, 'projects.json');
    fs.writeFileSync(file, typeof contents === 'string' ? contents : JSON.stringify(contents));
    try {
        return fn(file);
    } finally {
        fs.rmSync(dir, { recursive: true, force: true });
    }
}

console.log('projects-config merge');

test('projects.json entries become usable service configs', () => {
    withProjectsFile({
        projects: {
            newproj: {
                live: '/home/newproj',
                sandbox: '/home/agent-workspaces/newproj',
                review: { secretFiles: ['.env'], checks: [{ name: 'lint', dir: '.', cmd: 'npm', args: ['run', 'lint'] }] },
                deploy: { pm2Apps: ['newproj'], build: [{ dir: '.', cmd: 'npm', args: ['ci'] }] },
            },
        },
    }, (file) => {
        const review = loadProjects({}, { section: 'review', file });
        assert.deepStrictEqual(review.newproj.secretFiles, ['.env']);
        assert.strictEqual(review.newproj.checks[0].name, 'lint');
        assert.strictEqual(review.newproj.sandbox, '/home/agent-workspaces/newproj');

        const deploy = loadProjects({}, { section: 'deploy', file });
        assert.deepStrictEqual(deploy.newproj.pm2Apps, ['newproj']);
        // the deploy service calls the same directory "workspace"
        assert.strictEqual(deploy.newproj.workspace, '/home/agent-workspaces/newproj');
        // a section belongs to its own service only
        assert.strictEqual(deploy.newproj.secretFiles, undefined);
    });
});

test('built-in config wins over projects.json for the same project', () => {
    withProjectsFile({
        projects: {
            'large-project': {
                live: '/home/large-project',
                sandbox: '/ws/large-project',
                // a naive generated config that would run EVERYTHING
                review: { checks: [{ name: 'test', dir: '.', cmd: 'npm', args: ['test'] }] },
            },
        },
    }, (file) => {
        const builtins = {
            'large-project': {
                live: '/home/large-project',
                sandbox: '/ws/large-project',
                // the hand-tuned list that deliberately excludes live-hitting suites
                checks: [{ name: 'test', dir: '.', cmd: 'npm', args: ['run', 'test:review'] }],
                secretFiles: ['config/keys.json'],
            },
        };
        const merged = loadProjects(builtins, { section: 'review', file });
        assert.deepStrictEqual(merged['large-project'].checks[0].args, ['run', 'test:review'],
            'hand-tuned checks must not be replaced by generated ones');
        assert.deepStrictEqual(merged['large-project'].secretFiles, ['config/keys.json']);
    });
});

test('a built-in-only project still appears', () => {
    withProjectsFile({ projects: {} }, (file) => {
        const merged = loadProjects({ legacy: { live: '/l', sandbox: '/s' } }, { section: 'review', file });
        assert.ok(merged.legacy, 'built-in-only projects must not vanish');
    });
});

test('missing projects.json falls back to built-ins instead of throwing', () => {
    const merged = loadProjects({ a: { live: '/a' } },
        { section: 'review', file: '/nonexistent/definitely/projects.json' });
    assert.deepStrictEqual(Object.keys(merged), ['a']);
    assert.deepStrictEqual(readProjectsJson('/nonexistent/definitely/projects.json'), {});
});

test('malformed projects.json falls back instead of taking the service down', () => {
    withProjectsFile('{ this is not json', (file) => {
        const merged = loadProjects({ a: { live: '/a' } }, { section: 'review', file });
        assert.deepStrictEqual(Object.keys(merged), ['a']);
    });
});

test('config is re-read per call so runtime onboarding is picked up', () => {
    withProjectsFile({ projects: { one: { live: '/one', sandbox: '/s/one' } } }, (file) => {
        assert.deepStrictEqual(Object.keys(loadProjects({}, { section: 'review', file })), ['one']);
        const data = JSON.parse(fs.readFileSync(file, 'utf8'));
        data.projects.two = { live: '/two', sandbox: '/s/two' };
        fs.writeFileSync(file, JSON.stringify(data));
        assert.deepStrictEqual(Object.keys(loadProjects({}, { section: 'review', file })).sort(),
            ['one', 'two'], 'no restart should be needed to see a new project');
    });
});

console.log(`\n${passed} passed`);

test('a deploy restart list reaches the deploy service flat', () => {
    // projects.json nests it under `deploy`; the service reads `p.restart`.
    // The flattening is what connects the two, and a project on Docker
    // Compose gets nothing at all if it does not happen.
    withProjectsFile({
        projects: {
            composed: {
                live: '/srv/live/composed',
                sandbox: '/srv/ws/composed',
                deploy: {
                    restart: [{ kind: 'compose', cmd: 'docker', args: ['compose', 'up', '-d'], dir: '.' }],
                },
            },
        },
    }, (file) => {
        const merged = loadProjects({}, { section: 'deploy', file });
        assert.equal(merged.composed.restart[0].cmd, 'docker');
        assert.deepStrictEqual(merged.composed.restart[0].args, ['compose', 'up', '-d']);
    });
});

test('a project with no deploy block has no restart list to iterate', () => {
    withProjectsFile({
        projects: { bare: { live: '/srv/live/bare', sandbox: '/srv/ws/bare' } },
    }, (file) => {
        const merged = loadProjects({}, { section: 'deploy', file });
        assert.equal(merged.bare.restart, undefined, 'server.js guards with `|| []`');
    });
});
