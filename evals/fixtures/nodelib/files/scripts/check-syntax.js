// A lint that needs nothing installed: parse every source file and report the
// ones that do not. Enough to catch a broken edit, and it can never fail
// because a registry was down.
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..', 'src');
let bad = 0;
for (const name of fs.readdirSync(root).filter((f) => f.endsWith('.js'))) {
  const file = path.join(root, name);
  try {
    new vm.Script(fs.readFileSync(file, 'utf8'), { filename: file });
  } catch (err) {
    console.error(`${file}: ${err.message}`);
    bad += 1;
  }
}
if (bad) {
  console.error(`${bad} file(s) failed to parse`);
  process.exit(1);
}
console.log('syntax ok');
