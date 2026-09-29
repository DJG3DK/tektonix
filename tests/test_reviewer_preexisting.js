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


test('every compared check failing on the base too is the environment, not a pre-existing red suite', () => {
    const { applyBaseline } = require('../services/commit-reviewer/reviewer.js');
    const all = [{ name: 'typecheck', ok: false, output: 'TS2307' }, { name: 'lint', ok: false, output: 'x' }, { name: 'secrets', ok: true }];
    applyBaseline(all, { typecheck: false, lint: false });
    assert.ok(all[0].infrastructure && all[1].infrastructure, 'both flagged as the harness');
    assert.ok(!all[0].preexisting && /every check fails on the base/.test(all[0].output));
    const some = [{ name: 'typecheck', ok: false, output: 'TS2307' }, { name: 'lint', ok: true }];
    applyBaseline(some, { typecheck: false, lint: true });
    assert.ok(some[0].preexisting && !some[0].infrastructure, 'one red check on both is genuinely pre-existing');
});


test('a single configured check failing on both commits is the environment, not pre-existing', () => {
    const { applyBaseline } = require('../services/commit-reviewer/reviewer.js');
    const only = [{ name: 'test', ok: false, output: 'vitest: not found' }];
    applyBaseline(only, { test: false });
    assert.ok(only[0].infrastructure && !only[0].preexisting, JSON.stringify(only));
    // One red check beside a passing one on both commits stays pre-existing.
    const beside = [{ name: 'audit', ok: false, output: 'x' }, { name: 'test', ok: true }];
    applyBaseline(beside, { audit: false, test: true });
    assert.ok(beside[0].preexisting && !beside[0].infrastructure);
});
