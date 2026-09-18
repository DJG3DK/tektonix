// The review gate must blame the right thing.
//
// All four behaviours here come from one incident (2026-09-18). A commit that
// touched only backend files, and that the reviewer itself called clean, was
// rejected three times and escalated. Nothing was wrong with it. The gate had
// provisioned the branch's worktree one way and the base commit's another,
// compared the two, and concluded the difference was the commit's fault -- and
// the rejection it sent named the failing checks without ever saying why they
// failed, so the only way to find out was to re-run them by hand.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {
  packagesNeedingOwnInstall, baselineKey, classifyInfrastructureFailures, buildAgentMessage,
} = require('../services/commit-reviewer/reviewer.js');

function tmpTree(spec) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'gate-'));
  for (const [rel, kind] of Object.entries(spec)) {
    const full = path.join(root, rel);
    if (kind === 'dir') fs.mkdirSync(full, { recursive: true });
    else {
      fs.mkdirSync(path.dirname(full), { recursive: true });
      fs.writeFileSync(full, kind);
    }
  }
  return root;
}

// --- a root install does not reach a standalone package ---------------------

test('a package with its own manifest and no node_modules needs its own install', () => {
  const root = tmpTree({ 'package.json': '{}', 'frontend/package.json': '{}' });
  assert.deepEqual(packagesNeedingOwnInstall({ nodeModulesDirs: ['.', 'frontend'] }, root), ['frontend']);
});

test('a package the root install already covered is left alone', () => {
  // What a real workspaces root looks like after one install: the member has
  // node_modules already, so installing again would be waste, not a fix.
  const root = tmpTree({ 'package.json': '{}', 'frontend/package.json': '{}', 'frontend/node_modules': 'dir' });
  assert.deepEqual(packagesNeedingOwnInstall({ nodeModulesDirs: ['.', 'frontend'] }, root), []);
});

test('the repository root is never treated as a standalone package', () => {
  const root = tmpTree({ 'package.json': '{}' });
  assert.deepEqual(packagesNeedingOwnInstall({ nodeModulesDirs: ['.'] }, root), []);
});

test('a configured directory with no manifest is not installed into', () => {
  const root = tmpTree({ 'package.json': '{}', 'assets': 'dir' });
  assert.deepEqual(packagesNeedingOwnInstall({ nodeModulesDirs: ['.', 'assets'] }, root), []);
});

test('a project that configures no dependency directories is a no-op', () => {
  assert.deepEqual(packagesNeedingOwnInstall({}, tmpTree({ 'package.json': '{}' })), []);
});

// --- a baseline is only an answer about the environment it ran in -----------

test('the same base measured two ways is cached under two keys', () => {
  // The bug this prevents: the base commit was measured with its dependencies
  // inherited from the live checkout, the branch had to install its own, and
  // the two results were compared as though they described the same thing.
  assert.notEqual(baselineKey('abc123', true), baselineKey('abc123', false));
});

test('the same base measured the same way reuses one key', () => {
  assert.equal(baselineKey('abc123', true), baselineKey('abc123', true));
});

// --- a check that never ran is not a failing check --------------------------

test('a missing command is recorded as infrastructure, not as a code failure', () => {
  const results = [{ name: 'lint', ok: false, output: '\n> eslint .\n\nsh: 1: eslint: not found\n' }];
  classifyInfrastructureFailures(results);
  assert.equal(results[0].infrastructure, true);
});

test('the other shell wording for a missing command is caught too', () => {
  const results = [{ name: 'format', ok: false, output: 'prettier: command not found' }];
  classifyInfrastructureFailures(results);
  assert.equal(results[0].infrastructure, true);
});

test('a module the code under review cannot resolve stays the code\'s problem', () => {
  // The distinction that matters: the shell could not find the PROGRAM versus
  // the program could not find a MODULE. The second is usually a real defect
  // in the diff -- a bad import path, a dependency someone forgot to add --
  // and must not be excused as an environment fault.
  const results = [{ name: 'test', ok: false, output: "Error: Cannot find module './helpers/parse'" }];
  classifyInfrastructureFailures(results);
  assert.equal(results[0].infrastructure, undefined);
});

test('an ordinary assertion failure is not infrastructure', () => {
  const results = [{ name: 'test', ok: false, output: '1 failing\n  expected 3 to equal 4' }];
  classifyInfrastructureFailures(results);
  assert.equal(results[0].infrastructure, undefined);
});

test('a passing check is never reclassified, whatever its output quotes', () => {
  const results = [{ name: 'test', ok: true, output: 'ok - handles "command not found" gracefully' }];
  classifyInfrastructureFailures(results);
  assert.equal(results[0].infrastructure, undefined);
});

// --- the rejection has to say why -------------------------------------------

test('the rejection carries the actual failure output', () => {
  const msg = buildAgentMessage(
    { verdict: 'NEEDS_FIXES', summary: 'Tests fail.', findings: [] },
    [{ name: 'test', ok: false, output: 'AssertionError: expected 3 to equal 4' }],
  );
  assert.match(msg, /Failure output:/);
  assert.match(msg, /expected 3 to equal 4/);
});

test('a check that could not run is separated and marked as not the agent\'s to fix', () => {
  const msg = buildAgentMessage(
    { verdict: 'NEEDS_FIXES', summary: 'Could not run.', findings: [] },
    [{ name: 'lint', ok: false, infrastructure: true, output: 'sh: 1: eslint: not found' }],
  );
  assert.match(msg, /Checks that could NOT RUN: lint/);
  assert.match(msg, /do NOT try to fix it from inside this repository/);
  // Must not also appear as a normal failing check, or the instruction is
  // contradicted two lines later.
  assert.doesNotMatch(msg, /Failed checks: [^\n]*lint/);
});

test('real failures and unrunnable checks are told apart in the same message', () => {
  const msg = buildAgentMessage(
    { verdict: 'NEEDS_FIXES', summary: 'Mixed.', findings: [] },
    [
      { name: 'unit', ok: false, output: '1 failing' },
      { name: 'lint', ok: false, infrastructure: true, output: 'sh: 1: eslint: not found' },
    ],
  );
  assert.match(msg, /Failed checks: unit/);
  assert.match(msg, /Checks that could NOT RUN: lint/);
});
