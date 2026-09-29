'use strict';
// The reviewer's queue is visible in state.json (2026-09-29): the agent's
// gate keeps waiting for a branch the file says is queued, the way it does
// for one the file says is in progress.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'reviewer-queue-'));
process.env.REVIEW_STATE_DIR = dir;
const { queueReview, runNextQueued, pendingReviews } = require('../services/commit-reviewer/reviewer.js');
const state = () => JSON.parse(fs.readFileSync(path.join(dir, 'state.json'), 'utf8'));

test('a queued branch is written to state.json in the order asked, with its position', () => {
  assert.equal(queueReview('shop', 'agent/first'), 1);
  assert.equal(queueReview('shop', 'agent/second'), 2);
  assert.equal(queueReview('shop', 'agent/first'), 1, 'asked twice is queued once');
  assert.deepEqual(state().shop.queued, ['agent/first', 'agent/second']);
});

test('draining the queue removes the branch from the file, and an empty queue leaves no key', () => {
  // An unknown project: the next request is taken off the queue and no
  // review starts, which is all this needs.
  runNextQueued('shop', 'sk-none');
  assert.deepEqual(state().shop.queued, ['agent/second']);
  runNextQueued('shop', 'sk-none');
  assert.equal(state().shop.queued, undefined);
  assert.equal(pendingReviews.has('shop'), false);
});
