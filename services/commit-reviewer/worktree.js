// worktree.js -- the review checkout.
//
// A branch is reviewed in a detached worktree off the live repository,
// provisioned with what git does not carry: live's node_modules (linked, or
// installed fresh with --ignore-scripts when a manifest changed or live's
// install is stale), the other stacks' dependency trees bound read-only,
// the data the tests need bound read-only, review-only credentials, and
// generated code. This module builds that checkout, tears it down, and at
// startup sweeps the ones an interrupted review left behind. Which
// directories need their own node_modules is detected here too, from the
// tree as it is now.

const fs = require('fs');
const path = require('path');
const { AGENT_HOME, log, run, git, runAgentCode } = require('./exec');
const { nodeModulesSource } = require('./node-modules-source');

/**
 * A symlink target inside the worktree, written relative to the link. An
 * absolute one names the worktree by its HOST path, and the check container
 * mounts the worktree at /workspace: the link dangled there, and every app
 * that imports a workspace-internal package failed typecheck with "cannot
 * find module" in any review that borrowed dependencies (2026-09-29).
 */
function relativeLink(linkPath, target) {
    return path.relative(path.dirname(linkPath), target) || '.';
}

/**
 * Why `rel` cannot be written into the worktree, or null when it can.
 *
 * Everything the review puts INTO the checkout -- a review credential, a
 * link to live's generated code, a bind of live's data -- lands on a path
 * the branch under review may have committed something at. A committed
 * `.env.review -> <live>/.env` made the secret copy, which runs in this
 * process and not in the sandbox, write review credentials over live's
 * .env; a committed directory link did the same one level up, and a
 * `data -> /etc` would have had `mount --bind` land live's data on the host
 * (2026-09-29). So every component from the worktree down is lstat'ed: a
 * link anywhere is refused, and so is a final entry that already exists,
 * unless `existingDir` allows a real directory there (a mount goes over
 * the branch's own copy, as it always has).
 */
function committedInTheWay(worktreePath, rel, { existingDir = false } = {}) {
  if (typeof rel !== 'string' || path.isAbsolute(rel)) return `${rel} is not a relative path`;
  const parts = rel.split(/[\\/]+/).filter((p) => p && p !== '.');
  if (parts.includes('..')) return `${rel} leaves the worktree`;
  let cur = worktreePath;
  for (let i = 0; i < parts.length; i++) {
    cur = path.join(cur, parts[i]);
    let st;
    try { st = fs.lstatSync(cur); } catch { return null; }   // absent from here down: free to create
    const shown = path.relative(worktreePath, cur);
    if (st.isSymbolicLink()) return `${shown} is a symlink in the branch under review`;
    if (i === parts.length - 1) {
      if (existingDir && st.isDirectory()) return null;
      return `${shown} already exists in the branch under review`;
    }
    if (!st.isDirectory()) return `${shown} is not a directory in the branch under review`;
  }
  return null;
}

/** In the bundle the agent runs every check in a container it starts, with
 * the mounts it is asked for; this container holds no docker socket and no
 * mount capability. Read per call so a test can set it. */
function delegated() {
    return process.env.TEKTONIX_BUNDLE === '1' && Boolean(process.env.AGENT_SANDBOX_URL);
}

// A dependency install with no cache, over a bind mount, into a Windows
// folder scanned by Defender: five minutes was not enough (2026-09-29).
const INSTALL_TIMEOUT_MS = 900_000;

// Review-only credentials, one subtree per project mirroring each project's
// own relative secret paths. Never contains production values.
const REVIEW_SECRETS_ROOT = path.join(AGENT_HOME, 'services/commit-reviewer/review-secrets');

// Overridable, as REVIEW_STATE_DIR and USAGE_LOG are (reviewer.js), because
// agent/evals runs a second reviewer instance: the worktrees are a disk-space
// and stale-checkout concern rather than a correctness one, but they are the
// same kind of shared mutable state and belong on the same switch.
const WORKTREE_ROOT = process.env.REVIEW_WORKTREE_ROOT
    || path.join(AGENT_HOME, 'services/commit-reviewer/worktrees');

// Directories that need their own node_modules, read from the tree AS IT IS
// NOW rather than recorded once at onboarding.
//
// Onboarding already detected this, from the root manifest's `workspaces`.
// Two things beat it on 2026-09-22 and both are ordinary:
//
//   * a repo made of standalone packages -- backend/, frontend/, admin/, each
//     with its own package.json and lockfile and NO workspaces entry tying
//     them together -- which workspaces-based detection cannot see at all;
//   * a project that changed after it was onboarded. That one had no root
//     manifest when it was added; the root task-runner and the CI that
//     exercises all three apps arrived later. A snapshot taken at onboarding
//     cannot know about a layout that did not exist yet.
//
// With nothing configured, the review installed at the repo root only --
// which had no dependencies -- and every check in every app failed on a
// missing tool. See applyBaseline for how that then became a READY.
//
// So: explicit config always wins (an operator who deselected directories
// meant it, and `[]` is a real answer), and only an ABSENT key falls back to
// looking. The walk is shallow and skips everything that is output rather
// than source, so a poll tick costs a handful of stats, not a tree scan.
const NM_SKIP = new Set(['node_modules', '.git', 'dist', 'build', 'coverage', '.next',
  '.nuxt', '.turbo', '.cache', 'out', 'vendor', 'tmp']);
const NM_MAX_DEPTH = 2;
// Entries in a node_modules directory that tools WRITE to during a run. Never
// linked from live: see the entry-by-entry loop in setupWorktree. Dependency
// structure that looks similar -- .bin, .pnpm, .modules.yaml, lockfile state --
// is deliberately absent and still linked.
const NM_BUILD_CACHES = new Set(['.vite', '.vite-temp', '.cache', '.tmp', '.parcel-cache',
  '.turbo', '.next', '.nuxt', '.eslintcache', '.angular', '.svelte-kit']);

function declaresDependencies(pkgPath) {
  try {
    const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'));
    const n = (o) => (o && typeof o === 'object' ? Object.keys(o).length : 0);
    return n(pkg.dependencies) + n(pkg.devDependencies) > 0;
  } catch {
    return false;       // unreadable or not JSON: nothing to install from it
  }
}

// A workspace root declares nothing itself and still needs node_modules: it
// is where pnpm keeps the store every member links into, and where an npm or
// yarn workspace hoists shared dependencies. Found by cross-checking this
// detector against the hand-tuned configs on 2026-09-22 -- it agreed on three
// projects and dropped "." from the pnpm monorepo, which would have left every
// member's links pointing at nothing.
function isWorkspaceRoot(dir) {
  if (fs.existsSync(path.join(dir, 'pnpm-workspace.yaml'))) return true;
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'));
    return Boolean(pkg.workspaces);
  } catch {
    return false;
  }
}

function detectNodeModulesDirs(root) {
  if (!root || !fs.existsSync(root)) return [];
  const found = [];
  const walk = (rel, depth) => {
    const dir = path.join(root, rel);
    const manifest = path.join(dir, 'package.json');
    if (fs.existsSync(manifest)
        && (declaresDependencies(manifest) || (rel === '' && isWorkspaceRoot(dir)))) {
      found.push(rel || '.');
    }
    if (depth >= NM_MAX_DEPTH) return;
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (!e.isDirectory() || e.name.startsWith('.') || NM_SKIP.has(e.name)) continue;
      walk(rel ? path.join(rel, e.name) : e.name, depth + 1);
    }
  };
  walk('', 0);
  return found.sort((a, b) => (a === '.' ? -1 : b === '.' ? 1 : a.localeCompare(b)));
}

/**
 * Dependency trees the review checkout needs but git does not carry.
 *
 * PHP keeps its dependencies in `vendor/`, Elixir in `deps/`, Ruby (when
 * bundled with --path) in `vendor/bundle`. All are gitignored, so a fresh
 * worktree has none of them, and `vendor/bin/phpunit` exits 127 with nothing
 * useful to say -- which reads as a broken suite rather than as "nothing
 * installed it".
 *
 * Bound READ-ONLY from the live checkout, not symlinked.
 *
 * The first version of this symlinked, and a symlink into the live tree is
 * writable: a check that writes -- a test fixture, a compile step, a
 * package's own cache -- would have edited production's installed
 * dependencies from inside the one step whose premise is that this code has
 * not been vetted. That is the same hole a live data store already has a
 * read-only remount for, and Linux silently ignores `-o ro` on a plain bind,
 * so the explicit remount is what makes the flag take effect. A mount that
 * cannot be made read-only is unmounted rather than left writable.
 *
 * Nothing is ever INSTALLED here: an install would execute an untrusted
 * composer.json's scripts. When a branch changes its dependency manifest,
 * installChangedDependencies below handles it instead, with the same
 * no-scripts discipline the npm path already uses.
 *
 * Returns { mounted, issues } -- issues are reported as failed setup checks,
 * never thrown: a project that has not installed yet should fail the check
 * that needs it, with that check's own message, not the whole review.
 */
async function materializeDependencyDirs(
  cfg, worktreePath, { log = () => {}, skip = [], run: runCmd = run } = {},
) {
  const mounted = [];
  const issues = [];
  for (const rel of cfg.dependencyDirs || []) {
    if (skip.includes(rel)) continue;          // being installed fresh instead
    const src = path.join(cfg.live, rel);
    const dest = path.join(worktreePath, rel);
    if (!fs.existsSync(src)) {
      log(`${rel} is not present on the live checkout — checks that need it will fail`);
      continue;
    }
    if (fs.existsSync(dest)) continue;         // the branch brought its own
    if (delegated()) {
      // The agent mounts live's copy into the check container itself
      // (sandbox.js mountSpecs); a bind here would need a capability this
      // container does not have, and failed as a "check" the agent was
      // told to fix.
      mounted.push(rel);
      continue;
    }
    const inTheWay = committedInTheWay(worktreePath, rel);
    if (inTheWay) {
      issues.push({ name: `deps (${rel})`, ok: false,
                    output: `${inTheWay}; live's ${rel} was not bound over it.` });
      continue;
    }
    fs.mkdirSync(dest, { recursive: true });
    const m = await runCmd('mount', ['--bind', src, dest], '/');
    if (!m.ok) {
      issues.push({ name: `deps (${rel})`, ok: false, output: m.output.slice(-2000) });
      continue;
    }
    const ro = await runCmd('mount', ['-o', 'remount,ro,bind', dest], '/');
    if (!ro.ok) {
      await runCmd('umount', [dest], '/');
      issues.push({
        name: `deps (${rel})`, ok: false,
        output: `could not remount read-only; unmounted rather than exposing live's installed `
              + `dependencies writable to an unreviewed branch.\n${ro.output.slice(-1000)}`,
      });
      continue;
    }
    mounted.push(rel);
  }
  return { mounted, issues };
}

/**
 * A branch that changed its dependency manifest must not be reviewed against
 * the dependencies live happens to have installed.
 *
 * The npm path has always done this (`depsChanged` -> a real install with
 * --ignore-scripts). PHP and Elixir had no equivalent, so a commit editing
 * composer.json ran its tests against live's vendor/ and passed or failed
 * for reasons that had nothing to do with the diff.
 *
 * Both installs here are the non-executing KIND, which is what made them
 * defensible on unvetted code before anything was contained:
 *   composer install --no-scripts --no-plugins   (the exact analogue of npm's
 *                                                 --ignore-scripts)
 *   mix deps.get                                 (fetches; compiles nothing)
 *
 * "Non-executing" is doing less work than it looks, though, which is why
 * these now go through runAgentCode like everything else: `mix deps.get`
 * evaluates mix.exs, and mix.exs is Elixir the agent could have written.
 * They run with network, because fetching is the entire point -- so on a
 * host install this is the one place agent-influenced code runs contained
 * AND online, and containment is the only thing standing between it and the
 * machine. In the compose bundle runAgentCode hands it to the agent, which
 * starts the same container: this process never runs it, since it holds the
 * secret that authorises a merge.
 *
 * Returns the directories that were installed fresh, so the caller knows not
 * to mount live's copy over them.
 */
async function installChangedDependencies(cfg, worktreePath, diffFiles, log = () => {}) {
  const installed = [];
  const issues = [];
  const declared = cfg.dependencyDirs || [];

  if (declared.includes('vendor') && /composer\.(json|lock)/.test(diffFiles)) {
    log('composer.json/lock changed — installing into the worktree instead of using live\'s vendor');
    const r = await runAgentCode(cfg, worktreePath, '.', 'composer',
      ['install', '--no-interaction', '--no-progress', '--no-scripts', '--no-plugins'],
      600_000, undefined, 'bridge');
    if (r.ok) installed.push('vendor');
    else issues.push({ name: 'composer install', ok: false, output: r.output.slice(-4000) });
  }

  if (declared.includes('deps') && /mix\.(exs|lock)/.test(diffFiles)) {
    log('mix.exs/lock changed — fetching this branch\'s dependencies');
    const r = await runAgentCode(cfg, worktreePath, '.', 'mix', ['deps.get'],
      600_000, undefined, 'bridge');
    if (r.ok) installed.push('deps');
    else issues.push({ name: 'mix deps.get', ok: false, output: r.output.slice(-4000) });
  }

  // Bundler is the exception, and it is the interesting one.
  //
  // `bundle install` builds native extensions, which is arbitrary code
  // execution at install time -- the same class of thing npm's
  // --ignore-scripts and composer's --no-scripts exist to prevent, and
  // bundler has no equivalent flag. So a branch that changes its Gemfile
  // does NOT get an install here.
  //
  // What it must not get either is live's bundle: those are the OLD gems,
  // and a green suite against them says nothing about the change. The
  // borrow is refused and the reason is recorded as a failed setup check, so
  // the verdict accounts for it instead of being quietly wrong.
  if (declared.includes('vendor/bundle') && /Gemfile(\.lock)?/.test(diffFiles)) {
    log('Gemfile/lock changed — refusing to borrow live\'s gems for a different Gemfile');
    issues.push({
      name: 'bundle',
      ok: false,
      output: 'This branch changes Gemfile/Gemfile.lock, so the live checkout\'s installed gems '
            + 'are the wrong ones to test against. They were not borrowed. `bundle install` is '
            + 'not run here either: it builds native extensions, which is code execution on an '
            + 'unreviewed branch (bundler has no --ignore-scripts). Install the new gems on the '
            + 'live checkout, or run this suite by hand before merging.',
    });
    // Named as installed so the read-only borrow is skipped for it.
    installed.push('vendor/bundle');
  }

  return { installed, issues };
}

// Which configured package directories a root install did NOT provision.
// A real workspaces root installs every member, so those already have their
// node_modules and are skipped; a directory that is its own npm project with
// no `workspaces` entry pointing at it gets nothing from the root install and
// needs its own. Exported so this is tested against real directories.
function packagesNeedingOwnInstall(cfg, worktreePath) {
  const out = [];
  for (const rel of cfg.nodeModulesDirs || []) {
    if (rel === '.' || rel === '') continue;
    const dir = path.join(worktreePath, rel);
    if (!fs.existsSync(path.join(dir, 'package.json'))) continue;
    if (fs.existsSync(path.join(dir, 'node_modules'))) continue;
    out.push(rel);
  }
  return out;
}

// Is what live has INSTALLED what live's lockfile says? The symlink path
// below borrows live's node_modules on the assumption that it is -- and a
// merge that bumps a lockfile does not reinstall anything by itself. Found
// 2026-09-23: a project's Dependabot fixes (multer 1 -> 2, nodemailer 6 -> 9
// and 30 more) had merged but never been installed in the checkout, so every
// review ran the suite against the old packages, 11 version tests failed on
// the branch AND on the base, and were waved through as "pre-existing" -- two
// five-minute test runs per review, and a real regression in those tests
// would have been waved through the same way. Returns a description of each
// mismatch; empty means live's install can be borrowed.
function liveInstallIsStale(cfg) {
  const { installMismatch } = require('../shared/deps-state');
  const out = [];
  for (const rel of new Set(['.', ...(cfg.nodeModulesDirs || [])])) {
    const dir = path.join(cfg.live, rel);
    // Nothing installed means nothing to borrow; the symlink path copes.
    if (!fs.existsSync(path.join(dir, 'node_modules'))) continue;
    const why = installMismatch(dir);
    if (why) out.push(`${rel}: ${why}`);
  }
  return out;
}

async function setupWorktree(project, cfg, sha, base, { depsChangedOverride = null } = {}) {
  const worktreePath = path.join(WORKTREE_ROOT, `${project}-${sha.slice(0, 12)}`);
  // Self-heal before the rmSync: a previous attempt that died between setup
  // and cleanup (or whose cleanup umounts failed -- run() is best-effort and
  // "target is busy" right after a check suite is real) leaves LIVE bind
  // mounts inside the stale directory, and rmSync then dies on the read-only
  // read-only mount with EROFS before any review can start. Seen live
  // 2026-08-27: three crashed reviews left worktrees/a large project-1a1fcd8194c7
  // with data/fixtures still ro-mounted, and every subsequent attempt failed
  // instantly with "Read-only file system". Sweep /proc/self/mounts for
  // anything under this path and unmount deepest-first, so setup succeeds no
  // matter how its predecessor died.
  try {
    const mounts = fs.readFileSync('/proc/self/mounts', 'utf8')
      .split('\n')
      .map((l) => l.split(' ')[1])
      .filter((m) => m && m.startsWith(worktreePath + '/') || m === worktreePath)
      .sort((a, b) => b.length - a.length);
    for (const m of mounts) {
      log(`  unmounting stale mount from a previous attempt: ${m}`);
      await run('umount', [m], '/');
    }
  } catch (err) {
    log(`  stale-mount sweep failed (continuing): ${err.message}`);
  }
  fs.rmSync(worktreePath, { recursive: true, force: true });
  await git(cfg.live, ['worktree', 'prune']);
  const add = await git(cfg.live, ['worktree', 'add', '--detach', worktreePath, sha]);
  if (!add.ok) throw new Error(`worktree add failed: ${add.output.slice(0, 500)}`);

  // Every nodeModulesDirs loop below tolerates the key being absent: a Go,
  // Rust or Ruby project has no such directories, and neither does a Node
  // project whose operator deselected them in the onboarding wizard.
  // node_modules aren't part of the git tree — symlink from live rather
  // than reinstall, UNLESS this commit touched a lockfile/package.json, in
  // which case a symlinked node_modules could be silently wrong. A single
  // workspace-root install covers every package's node_modules correctly
  // (that's what pnpm/npm workspaces are for) — do it once, not once per
  // nodeModulesDirs entry.
  // Same fixed range as the review itself, so 'did this commit touch the
  // prisma schema?' is answered against the branch's own work rather than
  // against whatever live happens to contain now.
  const diffFiles = (await git(cfg.live, ['diff', '--name-only', base || 'HEAD', sha])).output;
  // `depsChangedOverride` exists for the baseline run (checks.js): a base worktree
  // is built with sha === base, so its own diff is EMPTY and it would always
  // take the symlink path while the branch took the install path. Comparing
  // the two then compares provisioning, not code -- see markPreexistingFailures.
  const staleLive = depsChangedOverride === null ? liveInstallIsStale(cfg) : [];
  if (staleLive.length) {
    log(`[${project}] live's installed dependencies do not match its own lockfile (${staleLive.slice(0, 3).join('; ')}${staleLive.length > 3 ? '; ...' : ''}) -- installing fresh for this review instead of borrowing them`);
  }
  const depsChanged = depsChangedOverride === null
    ? /package\.json|pnpm-lock\.yaml|package-lock\.json/.test(diffFiles) || staleLive.length > 0
    : depsChangedOverride;

  const setupIssues = [];
  if (depsChanged) {
    // audit C-2 path 3: --ignore-scripts on every install below. The reviewer
    // installs an UNTRUSTED, agent-authored package.json as the service user;
    // without this, a malicious `postinstall`/`preinstall` hook executes
    // automatically during review, with network. A package that genuinely
    // needs a native build (bcrypt) or a codegen step (prisma) has that step
    // run explicitly elsewhere, or surfaces as a test failure the reviewer
    // reports -- both preferable to arbitrary code execution on install.
    log(`[${project}] dependency files changed — running a real install at the workspace root instead of symlinking`);
    const pm = fs.existsSync(path.join(cfg.live, 'pnpm-lock.yaml')) ? 'pnpm' : 'npm';
    if (pm === 'pnpm') {
      // Try frozen first — this is the exact check GitHub CI does
      // (`pnpm install --frozen-lockfile`) and the exact one missing here
      // until now: a commit that adds/removes a dependency in package.json
      // without regenerating pnpm-lock.yaml used to pass silently, because
      // --prefer-offline just resolves and tolerates the mismatch in
      // memory rather than failing on it. Safe to run frozen here (unlike
      // as a standalone check command) because this branch always installs
      // into a real, isolated worktree node_modules, never the symlinked
      // one used below — nothing here can write through to live's.
      const frozen = await runAgentCode(cfg, worktreePath, '.', pm,
        ['install', '--frozen-lockfile', '--ignore-scripts'], INSTALL_TIMEOUT_MS, undefined, 'bridge');
      if (frozen.ok) {
        // fall through, node_modules already installed
      } else if (/ERR_PNPM_OUTDATED_LOCKFILE/.test(frozen.output)) {
        setupIssues.push({ name: 'lockfile-consistency', ok: false, output: frozen.output.slice(-4000) });
        const lenient = await runAgentCode(cfg, worktreePath, '.', pm,
          ['install', '--prefer-offline', '--ignore-scripts'], INSTALL_TIMEOUT_MS, undefined, 'bridge');
        if (!lenient.ok) throw new Error(`pnpm install failed even non-frozen: ${lenient.output.slice(0, 1000)}`);
      } else {
        throw new Error(`pnpm install --frozen-lockfile failed: ${frozen.output.slice(0, 1000)}`);
      }
    } else {
      const install = await runAgentCode(cfg, worktreePath, '.', pm,
        ['install', '--prefer-offline', '--ignore-scripts'], INSTALL_TIMEOUT_MS, undefined, 'bridge');
      if (!install.ok) throw new Error(`${pm} install failed: ${install.output.slice(0, 1000)}`);
    }
    // A root install only covers the whole repo when the root manifest really
    // declares workspaces. A repo can just as easily carry a second,
    // standalone package (frontend/) with its own package.json and lockfile
    // and no `workspaces` entry, and then a root install never touches it:
    // every check configured with `dir: 'frontend'` resolves against the
    // backend-only root node_modules and fails on missing tooling rather than
    // on the code under review. Seen 2026-09-18: a backend-only diff that
    // happened to touch package.json flipped this branch on, the three
    // frontend checks failed with "eslint-plugin-react-hooks / prettier /
    // vitest not found", and the branch could not be merged no matter what
    // the agent did -- the verdict was NEEDS_FIXES while the review summary
    // said the diff itself was clean.
    //
    // The `else` branch below already treats every nodeModulesDirs entry as
    // its own root; this makes the depsChanged branch agree with it. Skipping
    // an entry that already has node_modules keeps a genuine workspace root
    // (where the root install DID cover everything) to exactly one install.
    // --ignore-scripts for the same reason as above: a postinstall hook in an
    // agent-authored manifest must not execute here.
    for (const rel of packagesNeedingOwnInstall(cfg, worktreePath)) {
      const dir = path.join(worktreePath, rel);
      log(`[${project}] ${rel}/ is a standalone package the root install did not cover — installing it`);
      const sub = await runAgentCode(cfg, worktreePath, path.relative(worktreePath, dir) || '.', pm,
        ['install', '--prefer-offline', '--ignore-scripts'], INSTALL_TIMEOUT_MS, undefined, 'bridge');
      if (!sub.ok) {
        setupIssues.push({ name: `install (${rel})`, ok: false, output: sub.output.slice(-4000) });
      }
    }
  } else {
    // Workspace-internal packages (anything in nodeModulesDirs that has its
    // own package.json — e.g. packages/shared-types) must resolve to THIS
    // worktree's own freshly-checked-out copy, never live's. A single
    // symlink for the whole node_modules directory gets this silently
    // wrong: pnpm's own internal symlink for such a package (e.g.
    // node_modules/@scope/pkg -> ../../packages/pkg) is relative, so once
    // the enclosing node_modules directory is (via our symlink) physically
    // live's, that relative hop resolves against live's real directory
    // tree too — landing back on live's copy of the package regardless of
    // what this worktree's own build produced. Seen live (2026-08-18): a
    // a monorepo project commit added a new shared-types export; the worktree's
    // typecheck kept resolving that shared-types package straight through to
    // live's copy (which never received the export, since this hadn't
    // merged yet) and reported "still doesn't resolve" for 4 review rounds
    // in a row — not a real bug in the reviewed commit, a symlink chain
    // bypassing the worktree's own fresh build entirely. Confirmed via
    // `fs.realpathSync` on the worktree's own node_modules entry.
    const internalPackages = new Map(); // package.json name -> worktree path
    for (const rel of cfg.nodeModulesDirs || []) {
      const pkgJsonPath = path.join(worktreePath, rel, 'package.json');
      if (!fs.existsSync(pkgJsonPath)) continue;
      try {
        const name = JSON.parse(fs.readFileSync(pkgJsonPath, 'utf8')).name;
        if (name) internalPackages.set(name, path.join(worktreePath, rel));
      } catch {
        // Not valid JSON or unreadable — treat as having no internal name,
        // same as if package.json didn't exist.
      }
    }

    for (const rel of cfg.nodeModulesDirs || []) {
      const source = nodeModulesSource(cfg, rel);
      const targetDir = path.join(worktreePath, rel);
      const targetNodeModules = path.join(targetDir, 'node_modules');
      if (!source) {
        log(`${rel === '.' ? '' : rel + '/'}node_modules: neither live nor the agent's workspace has a usable install -- checks that need it will fail`);
        continue;
      }
      const liveNodeModules = source.dir;
      if (source.which !== 'live') log(`${rel === '.' ? '' : rel + '/'}node_modules borrowed from the agent's workspace: live has none a Linux check can run`);
      fs.mkdirSync(targetDir, { recursive: true });
      if (cfg.bindMountNodeModules && !delegated()) {
        fs.mkdirSync(targetNodeModules, { recursive: true });
        const mount = await run('mount', ['--bind', liveNodeModules, targetNodeModules], '/');
        if (!mount.ok) throw new Error(`bind mount failed for ${rel}: ${mount.output.slice(0, 500)}`);
        // audit C-2 path 3: remount READ-ONLY. The reviewer only reads deps;
        // a writable bind mount of LIVE's node_modules let a test/build script in
        // the untrusted worktree write through to the running production app.
        const roMount = await run('mount', ['-o', 'remount,ro,bind', targetNodeModules], '/');
        if (!roMount.ok) {
          // Unmount, never warn-and-continue. A warning left live's
          // node_modules bind-mounted WRITABLE into an unreviewed worktree --
          // the exact hole the vendor/ mount was fixed for, in the stack that
          // had it first. Without the mount the checks fail on missing
          // dependencies, which is a loud, correct failure; with it, an
          // unvetted build script writes into the running app's modules.
          await run('umount', [targetNodeModules], '/');
          setupIssues.push({
            name: `node_modules (${rel})`, ok: false,
            output: `could not remount read-only; unmounted rather than exposing live's `
                  + `node_modules writable to an unreviewed branch.\n${roMount.output.slice(-1000)}`,
          });
        }
      } else if (internalPackages.size > 0) {
        // Populate entry-by-entry instead of one directory symlink, so each
        // workspace-internal package can be individually redirected to the
        // worktree's own copy; everything else (third-party deps) is just
        // as cheap to link this way — still a symlink, not a copy.
        fs.mkdirSync(targetNodeModules, { recursive: true });
        for (const entry of fs.readdirSync(liveNodeModules)) {
          // Build caches are per-run scratch, not dependencies. Linking one
          // points the worktree's copy at LIVE's, which the check container
          // sees read-only -- so the first tool to write its cache dies with
          // EROFS. Seen 2026-09-22: `vite build` failed on every commit because
          // a manual build on the live checkout had left node_modules/.vite-temp
          // behind, the worktree linked it, and Vite could not write its config
          // bundle. Skipped here, the tool simply creates a fresh one in the
          // worktree, where it belongs.
          if (NM_BUILD_CACHES.has(entry)) continue;
          if (entry.startsWith('@')) {
            const scopeDir = path.join(liveNodeModules, entry);
            let scopedEntries;
            try {
              scopedEntries = fs.readdirSync(scopeDir);
            } catch {
              continue; // not actually a directory (unexpected, but not fatal)
            }
            fs.mkdirSync(path.join(targetNodeModules, entry), { recursive: true });
            for (const scopedEntry of scopedEntries) {
              const fullName = `${entry}/${scopedEntry}`;
              const dest = path.join(targetNodeModules, fullName);
              const override = internalPackages.get(fullName);
              fs.symlinkSync(override ? relativeLink(dest, override) : path.join(scopeDir, scopedEntry), dest);
            }
          } else {
            const dest = path.join(targetNodeModules, entry);
            const override = internalPackages.get(entry);
            fs.symlinkSync(override ? relativeLink(dest, override) : path.join(liveNodeModules, entry), dest);
          }
        }
      } else {
        fs.symlinkSync(liveNodeModules, targetNodeModules);
      }
    }
  }

  // The same problem for stacks that keep dependencies inside the project
  // (PHP's vendor/, Elixir's deps/, a --path bundle): a branch that changed
  // its manifest gets a no-scripts install of its own, everything else
  // borrows live's copy bound read-only -- see the two functions. Go, Rust,
  // Maven, Gradle and NuGet use a user-wide cache and need nothing here.
  const talk = (m) => log(`[${project}] ${m}`);
  const fresh = await installChangedDependencies(cfg, worktreePath, diffFiles, talk);
  setupIssues.push(...fresh.issues);
  const deps = await materializeDependencyDirs(cfg, worktreePath, { log: talk, skip: fresh.installed });
  setupIssues.push(...deps.issues);

  // Read-only inputs the tests need but git doesn't carry. data/fixtures is
  // gitignored (348M of live market data), so a worktree has none -- and the
  // suites that need it quietly self-skip rather than fail. Measured: 31 of 49
  // suites skipped cases that way, including all 18 grid suites (test:grid
  // alone skipped 23 cases, test:grid-pump-protect 5). Those checks reported
  // green while asserting nothing.
  //
  // Bound read-only, not symlinked or copied: the source is the live trading
  // project's own data store, and a test that decided to write must not be able
  // to reach it. A plain `-o ro` bind is silently ignored by Linux, so the
  // explicit remount is what actually makes the flag take effect.
  for (const rel of cfg.readOnlyMounts || []) {
    const src = path.join(cfg.live, rel);
    const dest = path.join(worktreePath, rel);
    if (!fs.existsSync(src)) {
      setupIssues.push({ name: `mount (${rel})`, ok: false, output: `${src} does not exist on the live checkout.` });
      continue;
    }
    if (delegated()) continue;                 // mounted into the check container by the agent
    const inTheWay = committedInTheWay(worktreePath, rel, { existingDir: true });
    if (inTheWay) {
      setupIssues.push({ name: `mount (${rel})`, ok: false,
                         output: `${inTheWay}; live's ${rel} was not bound over it.` });
      continue;
    }
    fs.mkdirSync(dest, { recursive: true });
    const m = await run('mount', ['--bind', src, dest], '/');
    if (!m.ok) {
      setupIssues.push({ name: `mount (${rel})`, ok: false, output: m.output.slice(-2000) });
      continue;
    }
    const ro = await run('mount', ['-o', 'remount,ro,bind', dest], '/');
    if (!ro.ok) {
      await run('umount', [dest], '/');
      setupIssues.push({ name: `mount (${rel})`, ok: false, output: `could not remount read-only; unmounted rather than exposing live data writable.\n${ro.output.slice(-1000)}` });
    }
  }

  // Credentials come from REVIEW_SECRETS_ROOT, never from the live checkout.
  // They used to be copied out of cfg.live, so a review -- the step whose
  // job is to run code nobody has vetted -- ran it holding production
  // exchange keys, the live JWT secret, real mail and notification
  // credentials, a payment-provider secret and the production DATABASE_URL.
  // The review set is structurally identical but non-functional: dummies
  // that decrypt/parse correctly, mail at an unroutable host, side-effecting
  // flags off, DATABASE_URL aimed at the test database.
  //
  // Fail closed: a missing review secret is a failed setup check (same path
  // as a regenerate failure), never a fallback to live.
  //
  // `|| []` like every sibling loop above: a project with no `review` block
  // is the NORMAL shape -- a dashboard-created project starts as `{live,
  // sandbox}` and nothing else. Iterating the missing key threw inside
  // setupWorktree, no verdict was written, and the agent's wait_for_review
  // timed out.
  for (const rel of cfg.secretFiles || []) {
    const src = path.join(REVIEW_SECRETS_ROOT, project, rel);
    const dest = path.join(worktreePath, rel);
    if (!fs.existsSync(src)) {
      log(`[${project}] missing review secret ${rel} — recording a failed check rather than falling back to live`);
      setupIssues.push({
        name: `review-secret (${rel})`,
        ok: false,
        output: `Expected a review-only credential at ${src} but it does not exist.\n` +
                `Checks that need ${rel} will fail until it is created. Live credentials ` +
                `are deliberately NOT used as a fallback.`,
      });
      continue;
    }
    // Never through a link the branch committed, and never over a file it
    // committed: see committedInTheWay. COPYFILE_EXCL closes the last gap,
    // an entry that appears between the check and the copy.
    const inTheWay = committedInTheWay(worktreePath, rel);
    if (inTheWay) {
      log(`[${project}] not copying review secret ${rel}: ${inTheWay}`);
      setupIssues.push({
        name: `review-secret (${rel})`,
        ok: false,
        output: `${inTheWay}. The review credential was not written there: a committed link or file ` +
                `at a secret's path is how a branch redirects the copy at something outside its checkout.`,
      });
      continue;
    }
    fs.mkdirSync(path.dirname(dest), { recursive: true });
    try {
      fs.copyFileSync(src, dest, fs.constants.COPYFILE_EXCL);
    } catch (err) {
      setupIssues.push({ name: `review-secret (${rel})`, ok: false,
                         output: `could not write the review credential to ${rel}: ${err.message}` });
    }
  }

  // Generated code (e.g. Prisma's client) that lives outside both git and
  // node_modules — same symlink-unless-the-source-changed logic.
  //
  // A regenerate failure here (e.g. `prisma generate` rejecting an invalid
  // schema) usually means the COMMIT being reviewed introduced the bug, not
  // that the reviewer's own environment is broken. Originally this threw,
  // which aborted reviewProject() entirely via its outer catch — silently,
  // with nothing posted to the agent, and every subsequent poll re-failing
  // the exact same way until someone noticed the reviewer had gone quiet.
  // Recorded as a failed setup check instead, so it flows through the same
  // checkResults -> Sonnet review -> agent-notification path as a lint/test
  // failure, and the review actually completes and reports it.
  for (const g of cfg.generated || []) {
    const schemaChanged = diffFiles.includes(g.schemaFile);
    const targetDir = path.join(worktreePath, g.dir);
    if (schemaChanged) {
      log(`[${project}] ${g.schemaFile} changed — regenerating ${g.dir} instead of symlinking`);
      const gen = await runAgentCode(cfg, worktreePath, g.regenerate.dir,
        g.regenerate.cmd, g.regenerate.args, 120_000, undefined, g.regenerate.network);
      if (!gen.ok) {
        log(`[${project}] regenerate failed for ${g.dir} — recording as a failed check instead of aborting`);
        setupIssues.push({ name: `generate (${g.dir})`, ok: false, output: gen.output.slice(-4000) });
      }
    } else {
      const liveGenerated = path.join(cfg.live, g.dir);
      if (fs.existsSync(liveGenerated)) {
        // A branch that committed g.dir, or a link at it, used to throw
        // EEXIST here: closed, but as a harness failure nobody could read.
        const inTheWay = committedInTheWay(worktreePath, g.dir);
        if (inTheWay) {
          setupIssues.push({ name: `generate (${g.dir})`, ok: false,
                             output: `${inTheWay}; live's generated ${g.dir} was not linked over it.` });
          continue;
        }
        fs.mkdirSync(path.dirname(targetDir), { recursive: true });
        fs.symlinkSync(liveGenerated, targetDir);
      }
    }
  }

  return { worktreePath, setupIssues, depsChanged };
}

async function cleanupWorktree(cfg, worktreePath) {
  // Bind mounts must be unmounted before the directory tree under them can
  // be removed — `worktree remove` alone would otherwise fail (or, worse,
  // silently leave a stale mount pointing at a deleted path) if this were
  // skipped.
  if (cfg.bindMountNodeModules) {
    for (const rel of cfg.nodeModulesDirs || []) {
      const mounted = path.join(worktreePath, rel, 'node_modules');
      await run('umount', [mounted], '/');
    }
  }
  // Same reasoning for the read-only data mounts: a stale mount left behind
  // would point into a live data store from a deleted directory.
  for (const rel of cfg.readOnlyMounts || []) {
    await run('umount', [path.join(worktreePath, rel)], '/');
  }
  // Same reasoning for the dependency binds: a stale mount left behind points
  // into live's installed dependencies from a directory that is about to be
  // deleted, and `worktree remove` would fail on it.
  for (const rel of cfg.dependencyDirs || []) {
    await run('umount', [path.join(worktreePath, rel)], '/');
  }
  await git(cfg.live, ['worktree', 'remove', worktreePath, '--force']);
}

// Review worktrees left by a review that never reached its cleanup -- a
// restart mid-review is the usual way. setupWorktree heals one only when the
// SAME commit is reviewed again, so the rest stayed forever, some still
// holding a read-only mount of live data (one found 2026-09-23). At startup no
// review is running, so everything here is left over. Unmounted deepest-first,
// and never deleted while anything is still mounted in it: a delete through a
// bind mount deletes what the mount shows.
//
// `projects` is the project map (name -> cfg with `live`), passed in: the map
// is reviewer.js's, which defaults it to currentProjects().
async function sweepLeftoverWorktrees(root = WORKTREE_ROOT, projects = {}) {
  let names = [];
  try { names = fs.readdirSync(root); } catch { return []; }
  const swept = [];
  for (const name of names) {
    const dir = path.join(root, name);
    let mounts = [];
    try {
      mounts = fs.readFileSync('/proc/self/mounts', 'utf8').split('\n')
        .map((l) => l.split(' ')[1]).filter((m) => m && (m === dir || m.startsWith(dir + '/')))
        .sort((a, b) => b.length - a.length);
    } catch { /* no /proc: nothing we can see is mounted */ }
    for (const m of mounts) await run('umount', [m], '/');
    const still = fs.readFileSync('/proc/self/mounts', 'utf8').split('\n')
      .map((l) => l.split(' ')[1]).filter((m) => m && (m === dir || m.startsWith(dir + '/')));
    if (still.length) {
      log(`  leaving ${dir}: still mounted at ${still.join(', ')}`);
      continue;
    }
    const owner = Object.entries(projects).find(([p]) => name.startsWith(`${p}-`));
    if (owner && owner[1].live) {
      await git(owner[1].live, ['worktree', 'remove', '--force', dir]);
    }
    fs.rmSync(dir, { recursive: true, force: true });
    if (owner && owner[1].live) await git(owner[1].live, ['worktree', 'prune']);
    swept.push(name);
  }
  if (swept.length) log(`removed ${swept.length} review worktree(s) left by earlier runs: ${swept.join(', ')}`);
  return swept;
}

module.exports = {
  delegated, INSTALL_TIMEOUT_MS, relativeLink, committedInTheWay,
  REVIEW_SECRETS_ROOT, WORKTREE_ROOT, NM_BUILD_CACHES, detectNodeModulesDirs,
  materializeDependencyDirs, installChangedDependencies, packagesNeedingOwnInstall,
  liveInstallIsStale, setupWorktree, cleanupWorktree, sweepLeftoverWorktrees,
};
