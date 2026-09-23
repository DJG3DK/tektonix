'use strict';

/**
 * Parse CSV text into an array of rows, each an array of strings.
 * The first line is data, not a header. A trailing newline is ignored.
 */
function parseCSV(text) {
  return text
    .split('\n')
    .filter((line) => line.length > 0)
    .map((line) => line.split(','));
}

module.exports = { parseCSV };
