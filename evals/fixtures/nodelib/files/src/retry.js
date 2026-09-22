'use strict';

/**
 * How long to wait before attempt `n` (1-based), in milliseconds.
 * Exponential with a ceiling, so a long outage does not back off forever.
 */
function backoffMs(attempt, { base = 100, ceiling = 5000 } = {}) {
  return Math.min(base * 2 ** (attempt - 1), ceiling);
}

/** Split a list into chunks of at most `size`. */
function chunk(items, size) {
  const out = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/** A query string from a plain object, with keys in a stable order. */
function toQuery(params) {
  return Object.keys(params)
    .sort()
    .map((k) => `${encodeURIComponent(k)}=${encodeURIComponent(params[k])}`)
    .join('&');
}

module.exports = { backoffMs, chunk, toQuery };
