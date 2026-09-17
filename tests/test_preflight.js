// The deploy service checks a project's declared preflight URLs before any
// build step runs, and reports a failure as its own stage (2026-09-09: the
// storefront prerender failed against a dead API and the gate handed the
// agent a "compile error" to fix).
const test = require('node:test');
const assert = require('node:assert/strict');
const { runPreflight, formatPreflightError } = require('../services/agent-review/preflight.js');

const okFetch = async () => ({ ok: true, status: 200 });

test('no preflight configured passes', async () => {
  assert.deepEqual(await runPreflight(undefined, { fetchImpl: okFetch }), []);
  assert.deepEqual(await runPreflight([], { fetchImpl: okFetch }), []);
});

test('a 2xx answer passes, bare strings and objects both accepted', async () => {
  const seen = [];
  const fetchImpl = async (url) => { seen.push(url); return { ok: true, status: 204 }; };
  const failures = await runPreflight(['http://a/health', { url: 'http://b/ready', why: 'x' }], { fetchImpl });
  assert.deepEqual(failures, []);
  assert.deepEqual(seen, ['http://a/health', 'http://b/ready']);
});

test('a non-2xx answer and an unreachable host are both reported, with the why', async () => {
  const fetchImpl = async (url) => {
    if (url.includes('down')) { const e = new TypeError('fetch failed'); e.cause = { code: 'ECONNREFUSED' }; throw e; }
    return { ok: false, status: 503 };
  };
  const failures = await runPreflight([
    { url: 'http://api/down', why: 'prerender reads the catalog' },
    { url: 'http://api/busy' },
  ], { fetchImpl });
  assert.equal(failures.length, 2);
  assert.match(failures[0], /http:\/\/api\/down unreachable \(ECONNREFUSED\) -- prerender reads the catalog/);
  assert.match(failures[1], /http:\/\/api\/busy answered HTTP 503/);
});

test('every check is reported, not just the first failure', async () => {
  const fetchImpl = async () => ({ ok: false, status: 500 });
  const failures = await runPreflight(['http://a', 'http://b', 'http://c'], { fetchImpl });
  assert.equal(failures.length, 3);
});

test('the formatted error says nothing was touched and lists each failure', () => {
  const msg = formatPreflightError(['http://a unreachable (ECONNREFUSED)']);
  assert.match(msg, /no build step was run/);
  assert.match(msg, /  - http:\/\/a unreachable/);
});

test('a hanging dependency times out instead of blocking the deploy forever', async () => {
  const fetchImpl = (url, { signal }) => new Promise((_, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason));
  });
  const failures = await runPreflight(['http://hang'], { fetchImpl, timeoutMs: 20 });
  assert.equal(failures.length, 1);
  assert.match(failures[0], /unreachable \(TimeoutError\)/);
});
