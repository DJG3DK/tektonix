'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { renderBadge, STATES } = require('../src/badge');

test('renders the label for each known state', () => {
  for (const [state, label] of Object.entries(STATES)) {
    const html = renderBadge(state);
    assert.match(html, new RegExp(`badge--${state}`));
    assert.match(html, new RegExp(label));
  }
});

test('an unknown state falls back to ok', () => {
  assert.match(renderBadge('nonsense'), /badge--ok/);
});

test('a custom label is escaped', () => {
  assert.match(renderBadge('ok', { label: '<script>' }), /&lt;script&gt;/);
});
