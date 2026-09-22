'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { backoffMs, chunk, toQuery } = require('../src/retry');

test('backoff grows exponentially', () => {
  assert.equal(backoffMs(1), 100);
  assert.equal(backoffMs(2), 200);
  assert.equal(backoffMs(3), 400);
});

test('backoff respects the ceiling', () => {
  assert.equal(backoffMs(20, { ceiling: 5000 }), 5000);
});

test('chunk splits evenly and keeps the remainder', () => {
  assert.deepEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);
});

test('toQuery sorts keys and encodes values', () => {
  assert.equal(toQuery({ b: '2', a: 'x y' }), 'a=x%20y&b=2');
});
