'use strict';
const test = require('node:test');
const assert = require('node:assert');
const path = require('path');
const { loadConfig } = require('../src/config');

test('loads a config with a callback', (t, done) => {
  loadConfig(path.join(__dirname, '..', 'config', 'app.json'), (err, cfg) => {
    assert.ifError(err);
    assert.strictEqual(cfg.port, 8080);
    done();
  });
});
