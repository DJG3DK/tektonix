'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { createRouter } = require('../src/router');

test('matches an exact path', () => {
  const home = () => 'home';
  const r = createRouter().add('GET', '/', home);
  assert.strictEqual(r.match('GET', '/').handler, home);
  assert.strictEqual(r.match('POST', '/'), null);
});
