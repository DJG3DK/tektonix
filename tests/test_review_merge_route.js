'use strict';
// The merge route in services/agent-review/server.js merges and pushes the
// SHA it verified, and a verdict that cannot be cleared is logged, not
// thrown (2026-09-29 audit, R1 and R6). The route is an express handler
// inside a service that listens on load, so these pins read the source.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const SRC = fs.readFileSync(path.join(__dirname, '../services/agent-review/server.js'), 'utf8');
const merge = SRC.slice(SRC.indexOf("const tipSha = (await git(p.live, ['rev-parse', agentRef]))"), SRC.indexOf('res.json({ ok: true, output, push, mergedFrom })'));

test('the merge fast-forwards to the verified SHA, never to the ref', () => {
    assert.ok(merge.includes("['merge', '--ff-only', tipSha]"), 'merges tipSha');
    assert.ok(!merge.includes("['merge', '--ff-only', agentRef]"), 'the ref could have moved since the check');
});

test('the push sends the reviewed SHA onto the base branch with a lease', () => {
    assert.ok(merge.includes("'--force-with-lease', 'origin', `${tipSha}:refs/heads/${branch}`"), merge.slice(-600));
});

test('a verdict that cannot be cleared is logged with a logger that exists', () => {
    const catchBlock = merge.slice(merge.indexOf('await clearReviewState('), merge.indexOf('// Push to the real GitHub remote'));
    assert.ok(catchBlock.includes('console.log('), 'console.log, not a bare log()');
    assert.ok(!/[^.\w]log\(/.test(catchBlock), 'no bare log() call: there is none in module scope');
});
