'use strict';
const test = require('node:test');
const assert = require('node:assert');
const { renderComment, renderThread } = require('../src/comments');

test('renders a comment', () => {
  assert.strictEqual(renderComment({ author: 'ann', body: 'hello' }),
    '<li class="comment"><b>ann</b>: hello</li>');
});

test('renders a thread', () => {
  assert.match(renderThread([{ author: 'a', body: 'b' }]), /^<ul class="thread"><li/);
});
