'use strict';

/** Render one comment as a list item. */
function renderComment({ author, body }) {
  return `<li class="comment"><b>${author}</b>: ${body}</li>`;
}

/** Render a thread of comments. */
function renderThread(comments) {
  return `<ul class="thread">${comments.map(renderComment).join('')}</ul>`;
}

module.exports = { renderComment, renderThread };
