// The agent's answers to review rounds, read back out of the branch's commit
// log, oldest first; and the model's leaked tool-call markup stripped from
// its strings (2026-09-25).
const test = require('node:test');
const assert = require('node:assert/strict');
const { extractAgentResponses, stripLeakedMarkup, REVIEW_RESPONSE_MARKER } = require('../services/commit-reviewer/reviewer.js');

const LOG = [
  'c3c3c3c fix the parser',
  '(shipped via deepagents-based agent)',
  '',
  `${REVIEW_RESPONSE_MARKER} 2:`,
  'Round 2 again: the test you asked for passes -- pytest -k quote -> 1 passed.',
  'b2b2b2b fix the parser',
  '(shipped via deepagents-based agent)',
  '',
  `${REVIEW_RESPONSE_MARKER} 1:`,
  'The finding is wrong: the removed line is inside the CONTINUE branch.',
  'Probe output: it\'s a test',
  'a1a1a1a fix the parser',
  '(shipped via deepagents-based agent)',
].join('\n');

test('every answer in the log comes back, oldest round first', () => {
  const got = extractAgentResponses(LOG);
  assert.equal(got.length, 2);
  assert.equal(got[0].round, 2, 'the log is newest-first; the extractor keeps log order');
  assert.match(got[0].text, /pytest -k quote/);
  assert.equal(got[1].round, 1);
  assert.match(got[1].text, /CONTINUE branch/);
  assert.match(got[1].text, /Probe output/);
  assert.doesNotMatch(got[0].text, /b2b2b2b/, 'a block ends where the next commit or answer starts');
});

test('a log without answers yields none', () => {
  assert.deepEqual(extractAgentResponses('a1a1a1a fix\n(shipped via deepagents-based agent)\n'), []);
  assert.deepEqual(extractAgentResponses(''), []);
});

test('leaked tool-call framing is stripped from a summary', () => {
  assert.equal(stripLeakedMarkup('the unescape step is deleted.</summary>\n</invoke>\n'), 'the unescape step is deleted.');
  assert.equal(stripLeakedMarkup('plain text'), 'plain text');
});
