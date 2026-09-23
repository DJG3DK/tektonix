// Does a directory's INSTALLED node_modules match its own lockfile?
//
// A merge that bumps a lockfile installs nothing by itself. Found 2026-09-23:
// Dependabot fixes had merged on two projects -- sharp, nanoid, next,
// multer, nodemailer among them -- and were never installed, one of them on a
// site served from this machine. Its deploy ran `npm run build` against the
// old packages, and the reviewer borrowed the same stale install and waved the
// resulting version-test failures through as pre-existing. The reviewer asks
// this before borrowing an install; the deploy asks it before building.
//
// Returns a one-line description of the mismatch, or null when the install
// matches (or there is nothing to compare). Never throws.
const fs = require('fs');
const path = require('path');

function installMismatch(dir) {
  const nm = path.join(dir, 'node_modules');
  const npmLock = path.join(dir, 'package-lock.json');
  const pnpmLock = path.join(dir, 'pnpm-lock.yaml');
  try {
    if (fs.existsSync(pnpmLock)) {
      // pnpm keeps a copy of the lockfile it installed from.
      const installed = path.join(nm, '.pnpm', 'lock.yaml');
      if (!fs.existsSync(nm)) return 'not installed';
      if (fs.existsSync(installed) && fs.readFileSync(installed, 'utf8') !== fs.readFileSync(pnpmLock, 'utf8')) {
        return 'installed from a different pnpm-lock.yaml';
      }
      return null;
    }
    if (!fs.existsSync(npmLock)) return null;
    if (!fs.existsSync(nm)) return 'not installed';
    const pkgs = JSON.parse(fs.readFileSync(npmLock, 'utf8')).packages || {};
    let wrong = 0;
    const examples = [];
    for (const [key, meta] of Object.entries(pkgs)) {
      if (!key.startsWith('node_modules/') || !meta || !meta.version) continue;
      let have = null;
      try { have = JSON.parse(fs.readFileSync(path.join(dir, key, 'package.json'), 'utf8')).version; } catch { /* absent */ }
      // An optional package that is absent is a platform binary for another OS.
      if (have === meta.version || (have === null && meta.optional)) continue;
      wrong += 1;
      if (examples.length < 2) examples.push(`${key.slice('node_modules/'.length)} ${have || 'missing'} != ${meta.version}`);
    }
    return wrong ? `${wrong} package(s) not at the locked version, e.g. ${examples.join(', ')}` : null;
  } catch (err) {
    return `could not compare (${err.message})`;
  }
}

// The command that installs exactly what the lockfile says, or null.
function frozenInstall(dir) {
  if (fs.existsSync(path.join(dir, 'pnpm-lock.yaml'))) return { cmd: 'pnpm', args: ['install', '--frozen-lockfile'] };
  if (fs.existsSync(path.join(dir, 'package-lock.json'))) return { cmd: 'npm', args: ['ci'] };
  if (fs.existsSync(path.join(dir, 'yarn.lock'))) return { cmd: 'yarn', args: ['install', '--frozen-lockfile'] };
  return null;
}

module.exports = { installMismatch, frozenInstall };
