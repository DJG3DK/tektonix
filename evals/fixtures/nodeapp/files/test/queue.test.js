'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createQueue } = require('../src/queue');

test('runs every job and returns its result', async () => {
  const q = createQueue(2);
  const results = await Promise.all([1, 2, 3].map((n) => q.push(async () => n * 10)));
  assert.deepStrictEqual(results, [10, 20, 30]);
});
