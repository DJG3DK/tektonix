// A check that fails on the branch AND on the base commit is not the diff's
// fault: it must not force NEEDS_FIXES, and the agent must be told not to
// chase it (2026-09-09: pnpm audit's 14 high vulns on main looped a storefront
// task at the gate).
const test = require('node:test');
const assert = require('node:assert/strict');
const { applyBaseline, buildAgentMessage } = require('../services/commit-reviewer/reviewer.js');

test('applyBaseline marks only failures that also fail on base', () => {
  const results = [
    { name: 'lint', ok: true },
    { name: 'audit', ok: false, output: '14 high' },
    { name: 'unit-tests', ok: false, output: '1 failing' },
  ];
  applyBaseline(results, { audit: false, 'unit-tests': true });
  assert.equal(results[1].preexisting, true, 'audit fails on base too');
  assert.equal(results[2].preexisting, undefined, 'unit-tests pass on base, so this failure is the diff\'s');
  assert.equal(results[0].preexisting, undefined);
});

test('applyBaseline with no baseline leaves everything blocking', () => {
  const results = [{ name: 'audit', ok: false }];
  applyBaseline(results, undefined);
  assert.equal(results[0].preexisting, undefined);
});

test('the agent message separates pre-existing failures and says not to fix them', () => {
  const review = { verdict: 'NEEDS_FIXES', summary: 'One real problem.', findings: [{ severity: 'blocking', file: 'a.ts', issue: 'null deref' }] };
  const msg = buildAgentMessage(review, [
    { name: 'audit', ok: false, preexisting: true },
    { name: 'unit-tests', ok: false },
  ]);
  assert.match(msg, /Failed checks: unit-tests/);
  assert.doesNotMatch(msg, /Failed checks: [^\n]*audit/);
  assert.match(msg, /Pre-existing failing checks[^\n]*audit/);
  assert.match(msg, /NOT counted against this change/);
});

test('mechanical failure is derived from non-pre-existing checks only', () => {
  const checks = [{ name: 'audit', ok: false, preexisting: true }, { name: 'lint', ok: true }];
  const mechanicalFailed = checks.some((c) => !c.ok && !c.preexisting);
  assert.equal(mechanicalFailed, false);
});
