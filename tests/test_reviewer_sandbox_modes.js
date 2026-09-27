'use strict';
/**
 * The reviewer decides where agent-authored code runs, and there are three
 * answers -- none of which is "here".
 *
 *   sandbox      a host install: contain it
 *   delegated    the bundle: the agent, which has the docker socket, starts
 *                the same hardened container
 *   unavailable  anything that cannot contain it: REFUSE
 *
 * The bundle used to have a fourth, `bundle`, which ran checks in this
 * process on the theory that the container was containment enough. It was
 * not: this container holds the review-control secret and can reach the
 * merge endpoint, so a test file could merge its own branch with
 * `force: true`. The tests below pin that the in-process path is gone.
 *
 * Run: node tests/test_reviewer_sandbox_modes.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

let passed = 0;
const pending = [];
function test(name, fn) {
    pending.push((async () => {
        await fn();
        passed++;
        console.log(`  ok  ${name}`);
    })().catch((err) => {
        console.error(`  FAIL ${name}\n${err.stack}`);
        process.exitCode = 1;
    }));
}

console.log('reviewer sandbox modes');

function freshSandbox(env, delegateUrl) {
    for (const k of Object.keys(require.cache)) {
        if (k.includes('commit-reviewer/sandbox')) delete require.cache[k];
    }
    const saved = { b: process.env.TEKTONIX_BUNDLE, u: process.env.AGENT_SANDBOX_URL };
    if (env === undefined) delete process.env.TEKTONIX_BUNDLE;
    else process.env.TEKTONIX_BUNDLE = env;
    if (delegateUrl === undefined) delete process.env.AGENT_SANDBOX_URL;
    else process.env.AGENT_SANDBOX_URL = delegateUrl;
    const s = require('../services/commit-reviewer/sandbox');
    for (const [k, v] of [['TEKTONIX_BUNDLE', saved.b], ['AGENT_SANDBOX_URL', saved.u]]) {
        if (v === undefined) delete process.env[k];
        else process.env[k] = v;
    }
    return s;
}

// The whole service, not one file: the assertions below are about call
// sites, and a call site in any of the reviewer's modules counts. sandbox.js
// is the runner itself, and builtin-projects.local.js is the operator's.
const REVIEWER_DIR = path.join(__dirname, '..', 'services', 'commit-reviewer');
const REVIEWER_SRC = fs.readdirSync(REVIEWER_DIR)
    .filter((f) => f.endsWith('.js') && f !== 'sandbox.js' && !f.startsWith('builtin-projects'))
    .sort()
    .map((f) => fs.readFileSync(path.join(REVIEWER_DIR, f), 'utf8'))
    .join('\n');

test('inside the bundle with an agent to ask, checks are delegated once the agent says the image is there', async () => {
    const s = freshSandbox('1', 'http://agent:8100/');
    assert.equal(s.IN_CONTAINER, true);
    const asked = [];
    const agent = (data, status = 200) => async (url, init) => {
        asked.push({ url, init });
        return { ok: status < 300, status, json: async () => data };
    };
    const p = await s.probe({ secret: 's3cret', fetchImpl: agent({ ok: true, mode: 'delegated', image: 'tektonix-sandbox:latest', docker: '27.1' }) });
    assert.equal(p.mode, 'delegated');
    assert.ok(/tektonix-sandbox:latest/.test(p.reason), p.reason);
    assert.equal(asked[0].url, 'http://agent:8100/api/internal/review-sandbox/probe');
    assert.equal(asked[0].init.headers['x-review-secret'], 's3cret');
    assert.equal(s.DELEGATE_URL, 'http://agent:8100');
    // A yes is remembered: the next check does not ask again.
    const again = await s.probe({ secret: 's3cret', fetchImpl: agent({ ok: false, reason: 'gone' }) });
    assert.equal(again.mode, 'delegated');
    assert.equal(asked.length, 1);
});

test('inside the bundle, a missing image or a silent agent is asked again next time, never remembered', async () => {
    const s = freshSandbox('1', 'http://agent:8100');
    const answers = [
        { ok: false, reason: 'the sandbox image tektonix-sandbox:latest is not built; the agent is building it now, so try again in a few minutes' },
        { ok: true, mode: 'delegated', image: 'tektonix-sandbox:latest' },
    ];
    let calls = 0;
    const fetchImpl = async () => ({ ok: true, status: 200, json: async () => answers[calls++] });
    const first = await s.probe({ secret: 's3cret', fetchImpl });
    assert.equal(first.mode, 'unavailable');
    assert.ok(/building it now/.test(first.reason), first.reason);
    const second = await s.probe({ secret: 's3cret', fetchImpl });
    assert.equal(second.mode, 'delegated', 'the image was built in between; the second ask sees it');
    assert.equal(calls, 2);

    const t = freshSandbox('1', 'http://agent:8100');
    const down = await t.probe({ secret: 's3cret', fetchImpl: async () => { throw new Error('ECONNREFUSED'); } });
    assert.equal(down.mode, 'unavailable');
    assert.ok(/did not answer/.test(down.reason), down.reason);
    const refused = await t.probe({ secret: 's3cret', fetchImpl: async () => ({ ok: false, status: 401, json: async () => ({ detail: 'invalid or missing X-Review-Secret' }) }) });
    assert.equal(refused.mode, 'unavailable');
    assert.ok(/HTTP 401/.test(refused.reason), refused.reason);
    const noSecret = await t.probe({ secret: '', fetchImpl: async () => { throw new Error('must not be called'); } });
    assert.equal(noSecret.mode, 'unavailable');
    assert.ok(/REVIEW_CONTROL_SECRET/.test(noSecret.reason), noSecret.reason);
});

test('inside the bundle with no agent to ask, it refuses rather than running in-process', async () => {
    const s = freshSandbox('1', undefined);
    const p = await s.probe();
    assert.equal(p.mode, 'unavailable');
    // The reason has to name WHY, because the next person to read it is
    // deciding whether to "fix" it by mounting the socket.
    assert.ok(/docker socket/.test(p.reason) && /AGENT_SANDBOX_URL/.test(p.reason), p.reason);
});

test('outside the bundle it looks for a real docker and a built image', async () => {
    const s = freshSandbox(undefined);
    assert.equal(s.IN_CONTAINER, false);
    const p = await s.probe();
    assert.ok(['sandbox', 'unavailable'].includes(p.mode), p.mode);
    if (p.mode === 'unavailable') assert.ok(p.reason.length > 0);
});

test('the image is overridable, so a bundle build can pin its own', () => {
    const saved = process.env.AGENT_SANDBOX_IMAGE;
    process.env.AGENT_SANDBOX_IMAGE = 'someone-elses:tag';
    const s = freshSandbox(undefined);
    assert.equal(s.IMAGE, 'someone-elses:tag');
    if (saved === undefined) delete process.env.AGENT_SANDBOX_IMAGE;
    else process.env.AGENT_SANDBOX_IMAGE = saved;
    freshSandbox(undefined);
});

function fixtureWorktree() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'rvw-'));
    const live = path.join(root, 'live');
    const wt = path.join(root, 'worktrees', 'shop-0123456789ab');
    fs.mkdirSync(path.join(live, '.git', 'worktrees', 'shop-0123456789ab'), { recursive: true });
    fs.mkdirSync(path.join(live, 'node_modules'), { recursive: true });
    fs.mkdirSync(path.join(live, 'data'), { recursive: true });
    fs.mkdirSync(wt, { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${path.join(live, '.git', 'worktrees', 'shop-0123456789ab')}\n`);
    fs.symlinkSync(path.join(live, 'node_modules'), path.join(wt, 'node_modules'));
    return { root, live: fs.realpathSync(live), wt };
}

test('the delegated request carries the check, the mounts as data, and none of this container\'s env', () => {
    const s = freshSandbox('1', 'http://agent:8100');
    const { live, wt } = fixtureWorktree();
    const body = s.delegatedRequest({ live, readOnlyMounts: ['data'] }, wt, 'frontend', 'npm', ['test'],
        120_000, { PATH: '/usr/bin', HOME: '/root', FOO: 'bar' }, 'bridge', 'go');
    assert.equal(body.project, 'shop');
    assert.equal(body.worktree, wt);
    assert.equal(body.relDir, 'frontend');
    assert.equal(body.cmd, 'npm');
    assert.deepEqual(body.args, ['test']);
    assert.equal(body.network, 'bridge');
    assert.equal(body.stack, 'go');
    assert.equal(body.timeoutMs, 120_000);
    assert.equal(body.env.FOO, 'bar');
    assert.equal(body.env.CI, 'true');
    assert.ok(!('PATH' in body.env) && !('HOME' in body.env), JSON.stringify(body.env));
    const dsts = body.mounts.map((m) => m.dst).sort();
    assert.deepEqual(dsts, [path.join(live, '.git'), path.join(live, 'node_modules'), '/workspace/data'].sort());
});

test('anything but "bridge" is sent as no network at all', () => {
    const s = freshSandbox('1', 'http://agent:8100');
    const { live, wt } = fixtureWorktree();
    for (const n of [undefined, 'host', 'none', 'container:x']) {
        assert.equal(s.delegatedRequest({ live }, wt, '.', 'npm', [], 1, {}, n).network, 'none');
    }
});

test('a delegated run authenticates, and a refusal or an outage is infrastructure, never a pass', async () => {
    const s = freshSandbox('1', 'http://agent:8100');
    const { live, wt } = fixtureWorktree();
    const seen = [];
    const answer = (status, data) => async (url, init) => {
        seen.push({ url, init });
        return { ok: status < 300, status, json: async () => data };
    };

    const ok = await s.runDelegated({ live }, wt, '.', 'npm', ['test'], 5_000, {}, 'none', null,
        { secret: 's3cret', fetchImpl: answer(200, { ok: true, code: 0, output: 'all green', image: 'img' }) });
    assert.deepEqual(ok, { ok: true, code: 0, output: 'all green' });
    assert.equal(seen[0].url, 'http://agent:8100/api/internal/review-sandbox/run');
    assert.equal(seen[0].init.headers['x-review-secret'], 's3cret');

    const failing = await s.runDelegated({ live }, wt, '.', 'npm', ['test'], 5_000, {}, 'none', null,
        { secret: 's3cret', fetchImpl: answer(200, { ok: false, code: 1, output: '1 failing' }) });
    assert.equal(failing.ok, false);
    assert.ok(!failing.infrastructure, 'a real failing check is the code\'s problem');

    const missing = await s.runDelegated({ live }, wt, '.', 'go', ['test'], 5_000, {}, 'none', null,
        { secret: 's3cret', fetchImpl: answer(200, {
            ok: false, code: 127, image: 'tektonix-sandbox:latest',
            output: 'exec: "go": executable file not found in $PATH' }) });
    assert.equal(missing.missingTool, 'go');

    for (const [label, opts] of [
        ['refused', { secret: 's3cret', fetchImpl: answer(400, { detail: 'refused: mount outside live' }) }],
        ['down', { secret: 's3cret', fetchImpl: async () => { throw new Error('ECONNREFUSED'); } }],
        ['no secret', { secret: '', fetchImpl: answer(200, { ok: true, code: 0, output: '' }) }],
    ]) {
        const r = await s.runDelegated({ live }, wt, '.', 'npm', ['test'], 5_000, {}, 'none', null, opts);
        assert.equal(r.ok, false, label);
        assert.equal(r.infrastructure, true, label);
        assert.ok(/^SETUP: /.test(r.output), `${label}: ${r.output}`);
    }
});

test('a check that could not be RUN is flagged as infrastructure, not as failing', () => {
    // The distinction decides whose problem it is. Everything downstream
    // reads `.infrastructure`; without it, "the sandbox image has no go" is
    // handed to the agent as a failing check, and the agent spends rounds
    // debugging an environment it cannot see -- the exact loop the host-side
    // MISSING_TOOL_RE was written to stop. That regex matches a SHELL saying
    // "not found" and never matches our own SETUP message, which is why this
    // is carried structurally instead of matched again.
    assert.ok(/infrastructure: true/.test(REVIEWER_SRC), 'the runner never flags a setup failure');
    assert.ok(/r\.infrastructure \|\| r\.missingTool/.test(REVIEWER_SRC),
        'the check loop must carry the runner\'s own verdict rather than re-deriving it');
    assert.ok(!/REFUSED:/.test(REVIEWER_SRC),
        'the refusal says SETUP:, like the missing-toolchain message, so both read the same way');
});

test('the refusal text tells an operator what to do and that nothing ran', () => {
    assert.ok(REVIEWER_SRC.includes('SETUP: this check runs code the agent wrote'), 'no refusal path');
    assert.ok(/It was not run on the host/.test(REVIEWER_SRC), 'the refusal must say nothing ran');
    assert.ok(/docker\/agent-sandbox/.test(REVIEWER_SRC), 'the refusal must name the fix');
});

test('nothing executes agent code through runSealed outside runDatabaseCheck', () => {
    // The whole point: one place decides containment, and it never answers
    // "this process". A call site that goes straight to runSealed is the
    // regression this catches -- including the in-process bundle branch
    // runAgentCode used to have.
    //
    // runDatabaseCheck is the one documented exception on a HOST install
    // (SECURITY.md, "The database checks, which stay on the host"), cut out
    // before counting, with its own calls pinned.
    const dbStart = REVIEWER_SRC.indexOf('async function runDatabaseCheck(');
    const dbEnd = REVIEWER_SRC.indexOf('\n}\n', dbStart);
    assert.ok(dbStart > 0 && dbEnd > dbStart, 'runDatabaseCheck not found');
    const dbCheck = REVIEWER_SRC.slice(dbStart, dbEnd);
    const rest = REVIEWER_SRC.slice(0, dbStart) + REVIEWER_SRC.slice(dbEnd);
    const calls = (rest.match(/[^\w]runSealed\(/g) || []).length;
    assert.equal(calls, 1,
        `runSealed appears ${calls} times outside runDatabaseCheck (expect only its definition); `
        + 'agent-authored code must go through runAgentCode');
    assert.ok(!/mode\.mode === 'bundle'/.test(REVIEWER_SRC), 'the in-process bundle branch is back');
    assert.equal((dbCheck.match(/[^\w]runSealed\(/g) || []).length, 5);
    const code = dbCheck.replace(/\/\/.*$/gm, '');
    assert.ok(!/[^\w]run\(/.test(code), 'runDatabaseCheck calls run(), which inherits process.env');
    // ...and in the bundle it refuses before any of them: there, "outside
    // the sandbox" is the container holding the merge secret.
    const refuse = dbCheck.indexOf('sandbox.IN_CONTAINER');
    assert.ok(refuse > 0 && refuse < dbCheck.indexOf('runSealed('),
        'runDatabaseCheck must refuse in the bundle before it runs anything');
});

test('a file pulled into the review prompt cannot be a way out of the worktree', () => {
    const { root, wt } = fixtureWorktree();
    const secret = path.join(root, 'secret.json');
    fs.writeFileSync(secret, 'the-merge-secret');
    fs.writeFileSync(path.join(wt, 'ok.ts'), 'export const x = 1;');
    fs.symlinkSync(secret, path.join(wt, 'config.json'));
    process.env.REVIEW_CONTROL_SECRET = process.env.REVIEW_CONTROL_SECRET || 'unused';
    const reviewer = require('../services/commit-reviewer/reviewer');
    assert.equal(reviewer.readWorktreeFile(wt, 'ok.ts', 100), 'export const x = 1;');
    assert.equal(reviewer.readWorktreeFile(wt, 'config.json', 100), null, 'followed a symlink out');
    assert.equal(reviewer.readWorktreeFile(wt, '../../secret.json', 100), null, 'followed ../');
    const out = reviewer.gatherReferencedFiles(wt, 'touches ../../secret.json and ./config.json', '');
    assert.ok(!out.includes('the-merge-secret'), out);
});

Promise.all(pending).then(() => {
    console.log(`\n${passed} passed`);
});

test('in the bundle the database checks are delegated whole, and the rows come back in the host shape', async () => {
    const s = freshSandbox('1', 'http://agent:8100');
    const { live, wt } = fixtureWorktree();
    const seen = [];
    const answer = (status, data) => async (url, init) => {
        seen.push({ url, init });
        return { ok: status < 300, status, json: async () => data };
    };
    const rows = await s.runDelegatedDatabaseCheck({ live, name: 'shop', dependencyDirs: ['node_modules'] }, wt, null,
        { secret: 's3cret', fetchImpl: answer(200, { results: [
            { name: 'db-drift', ok: true, code: 0, output: 'no drift' },
            { name: 'db-seed', ok: false, code: 1, output: 'seed failed' },
        ] }) });
    assert.deepEqual(rows, [{ name: 'db-drift', ok: true, output: 'no drift' }, { name: 'db-seed', ok: false, output: 'seed failed' }]);
    assert.equal(seen[0].url, 'http://agent:8100/api/internal/review-sandbox/db-check');
    assert.equal(seen[0].init.headers['x-review-secret'], 's3cret');
    const body = JSON.parse(seen[0].init.body);
    assert.equal(body.project, 'shop');
    assert.equal(body.worktree, wt);
    assert.ok(Array.isArray(body.mounts), 'the dependency mounts travel with the request');
    assert.ok(!('cmd' in body) && !('env' in body), 'the commands and the DSN are the agent\'s to decide');

    for (const [label, opts] of [
        ['bundle without checks services', { secret: 's3cret', fetchImpl: answer(503, { detail: 'database checks are not enabled on this deployment' }) }],
        ['agent down', { secret: 's3cret', fetchImpl: async () => { throw new Error('ECONNREFUSED'); } }],
        ['no secret', { secret: '', fetchImpl: answer(200, { results: [] }) }],
    ]) {
        const r = await s.runDelegatedDatabaseCheck({ live, name: 'shop' }, wt, null, opts);
        assert.equal(r.length, 1, label);
        assert.equal(r[0].name, 'db-setup', label);
        assert.equal(r[0].infrastructure, true, label);
        assert.ok(/^SETUP: /.test(r[0].output), `${label}: ${r[0].output}`);
    }
});

test('the checks module hands the database checks to the agent in the bundle and refuses without one', async () => {
    const src = fs.readFileSync(path.join(REVIEWER_DIR, 'checks.js'), 'utf8');
    assert.ok(/sandbox\.runDelegatedDatabaseCheck\(cfg, worktreePath/.test(src), 'the bundle branch delegates');
    assert.ok(/AGENT_SANDBOX_URL is unset, so there is no agent to start the checks container/.test(src), 'and refuses without an agent');
    assert.ok(!/runSealed\(dc\.driftCmd[\s\S]*IN_CONTAINER/.test(src.split('async function runDatabaseCheck')[1].split('IN_CONTAINER')[0]),
        'nothing agent-authored runs in this process before the bundle check');
});
