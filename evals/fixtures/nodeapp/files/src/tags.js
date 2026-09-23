'use strict';

/** Every distinct tag across the posts, in first-seen order. */
function uniqueTags(posts) {
  const out = [];
  for (const post of posts) {
    for (const tag of post.tags) {
      if (!out.includes(tag)) out.push(tag);
    }
  }
  return out;
}

module.exports = { uniqueTags };
