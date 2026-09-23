'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { uniqueTags } = require('../src/tags');

test('distinct tags in first-seen order', () => {
  assert.deepStrictEqual(uniqueTags([{ tags: ['a', 'b'] }, { tags: ['b', 'c'] }]), ['a', 'b', 'c']);
});
