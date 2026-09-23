'use strict';
const fs = require('fs');

/**
 * Read and parse a JSON config file, calling `callback(err, config)`.
 */
function loadConfig(path, callback) {
  fs.readFile(path, 'utf8', (err, text) => {
    if (err) return callback(err);
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      return callback(e);
    }
    callback(null, parsed);
  });
}

module.exports = { loadConfig };
