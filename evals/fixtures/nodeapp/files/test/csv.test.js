'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { parseCSV } = require('../src/csv');

test('parses simple rows', () => {
  assert.deepStrictEqual(parseCSV('a,b\n1,2\n'), [['a', 'b'], ['1', '2']]);
});
