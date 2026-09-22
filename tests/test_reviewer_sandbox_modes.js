'use strict';
/**
 * The reviewer decides where agent-authored code runs, and there are three
 * answers -- not two.
 *
 *   sandbox      a host install: contain it
 *   bundle       already inside a container with no docker socket: leave it
 *   unavailable  a host install that cannot contain it: REFUSE
 *
 * The third is the one worth a test. "Fall back to the host when the sandbox
 * is unavailable" is the path anyone attacking this would engineer, and it is
 * also the change a future maintainer makes to stop a red build. The refusal
 * has to be load-bearing and it has to say why.
 *
 * Run: node tests/test_reviewer_sandbox_modes.js
 */

const assert = require('assert');

let passed = 0;
function test(name, fn) { fn(); passed++; console.log(`  ok  ${name}`); }

console.log('reviewer sandbox modes');

function freshSandbox(env) {
    for (const k of Object.keys(require.cache)) {
        if (k.includes('commit-reviewer/sandbox')) delete require.cache[k];
    }
    const saved = process.env.TEKTONIX_BUNDLE;
    if (env === undefined) delete process.env.TEKTONIX_BUNDLE;
    else process.env.TEKTONIX_BUNDLE = env;
    const s = require('../services/commit-reviewer/sandbox');
    if (saved === undefined) delete process.env.TEKTONIX_BUNDLE;
    else process.env.TEKTONIX_BUNDLE = saved;
    return s;
}

test('inside the bundle it reports bundle, without asking docker anything', async () => {
    const s = freshSandbox('1');
    assert.equal(s.IN_CONTAINER, true);
    const p = await s.probe();
    assert.equal(p.mode, 'bundle');
    // The reason has to name WHY, because the next person to read it is
    // deciding whether to "fix" it by mounting the socket.
    assert.ok(/docker socket/.test(p.reason), p.reason);
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

test('a check that could not be RUN is flagged as infrastructure, not as failing', () => {
    // The distinction decides whose problem it is. Everything downstream
    // reads `.infrastructure`; without it, "the sandbox image has no go" is
    // handed to the agent as a failing check, and the agent spends rounds
    // debugging an environment it cannot see -- the exact loop the host-side
    // MISSING_TOOL_RE was written to stop. That regex matches a SHELL saying
    // "not found" and never matches our own SETUP message, which is why this
    // is carried structurally instead of matched again.
    const src = require('fs').readFileSync(
        require('path').join(__dirname, '..', 'services', 'commit-reviewer', 'reviewer.js'), 'utf8');
    assert.ok(/infrastructure: true/.test(src), 'the runner never flags a setup failure');
    assert.ok(/r\.infrastructure \|\| r\.missingTool/.test(src),
        'the check loop must carry the runner\'s own verdict rather than re-deriving it');
    assert.ok(!/REFUSED:/.test(src),
        'the refusal says SETUP:, like the missing-toolchain message, so both read the same way');
});

test('the refusal text tells an operator what to do and that nothing ran', () => {
    // Asserted against the reviewer's source rather than by driving a whole
    // review: what matters is that the refusal path exists, says it did not
    // run on the host, and names the fix.
    const src = require('fs').readFileSync(
        require('path').join(__dirname, '..', 'services', 'commit-reviewer', 'reviewer.js'), 'utf8');
    assert.ok(src.includes('SETUP: this check runs code the agent wrote'), 'no refusal path');
    assert.ok(/It was not run on the host/.test(src), 'the refusal must say nothing ran');
    assert.ok(/docker\/agent-sandbox/.test(src), 'the refusal must name the fix');
});

test('nothing executes agent code through runSealed any more', () => {
    // The whole point: one place decides containment. A new call site that
    // goes straight to runSealed is the regression this catches.
    const src = require('fs').readFileSync(
        require('path').join(__dirname, '..', 'services', 'commit-reviewer', 'reviewer.js'), 'utf8');
    // Its definition, and exactly one call: the bundle branch inside
    // runAgentCode. Any other call site is agent-authored code running
    // wherever this process happens to be, which is the hole being closed.
    const calls = (src.match(/[^\w]runSealed\(/g) || []).length;
    assert.equal(calls, 2,
        `runSealed appears ${calls} times (expect its definition + one call); `
        + 'agent-authored code must go through runAgentCode');
});

(async () => {
    console.log(`\n${passed} passed`);
})();
