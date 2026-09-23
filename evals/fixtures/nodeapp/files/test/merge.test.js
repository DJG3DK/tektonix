'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { deepMerge } = require('../src/merge');

test('merges nested objects', () => {
  assert.deepStrictEqual(deepMerge({ a: { x: 1 }, b: 1 }, { a: { y: 2 }, c: 3 }),
    { a: { x: 1, y: 2 }, b: 1, c: 3 });
});

test('replaces arrays', () => {
  assert.deepStrictEqual(deepMerge({ a: [1, 2] }, { a: [3] }), { a: [3] });
});
