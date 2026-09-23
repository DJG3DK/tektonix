'use strict';

function isPlainObject(v) {
  return v !== null && typeof v === 'object' && !Array.isArray(v);
}

/**
 * Merge `source` into `target`, recursively, and return `target`. Nested
 * objects merge; arrays and everything else are replaced.
 */
function deepMerge(target, source) {
  for (const key of Object.keys(source)) {
    const value = source[key];
    if (isPlainObject(value)) {
      if (!isPlainObject(target[key])) target[key] = {};
      deepMerge(target[key], value);
    } else {
      target[key] = value;
    }
  }
  return target;
}

module.exports = { deepMerge };
