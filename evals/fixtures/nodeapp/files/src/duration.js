'use strict';

const UNITS = { ms: 1, s: 1000, m: 60_000, h: 3_600_000, d: 86_400_000 };

/**
 * Parse a duration like "1h30m", "45s" or "2d 4h" into milliseconds.
 * Units: ms, s, m, h, d. Whitespace between parts is allowed. Parts may come
 * in any order and repeat (they add up). Anything else -- an empty string, a
 * number without a unit, an unknown unit -- throws a TypeError.
 */
function parseDuration(text) {
  if (typeof text !== 'string' || text.trim() === '') throw new TypeError('empty duration');
  const compact = text.replace(/\s+/g, '');
  const re = /(\d+)(ms|s|m|h|d)/g;
  let total = 0;
  let consumed = 0;
  for (const match of compact.matchAll(re)) {
    if (match.index !== consumed) throw new TypeError(`bad duration: ${text}`);
    total += Number(match[1]) * UNITS[match[2]];
    consumed += match[0].length;
  }
  if (consumed !== compact.length) throw new TypeError(`bad duration: ${text}`);
  return total;
}

module.exports = { parseDuration };
