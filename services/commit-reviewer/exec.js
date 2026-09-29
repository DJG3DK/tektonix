// exec.js -- how the reviewer runs anything.
//
// The base layer under reviewer.js, worktree.js, checks.js and prompt.js:
// the installation root, the control secret, logging, the two process
// runners (run inherits this process's environment; runSealed does not), git
// with hooks off, and runAgentCode -- the ONE path through which anything
// the agent could have written is executed. Nothing here knows about a
// review; it is what every part of one runs on.

const path = require('path');
const { execFile } = require('child_process');
const sandbox = require('./sandbox');

// Installation root, derived from this file's location so the same source
// works from any checkout path (AGENT_HOME overrides). Declared here, with
// the requires, because consts below reference it -- defining it lower hit
// the temporal dead zone and crash-looped the service on boot.
const AGENT_HOME = process.env.AGENT_HOME || path.join(__dirname, '..', '..');

// audit C-4: the control port (4101) was unauthenticated on the same "localhost
// is the boundary" assumption that url_guard already disproved -- browse_page
// reached it live. Require the shared secret on the one mutating endpoint.
// services/shared/.env, not the router's: the model proxy's config should not
// carry the secret that authorises merge and deploy. The legacy path stays a
// fallback for deployments installed before the split -- see
// services/shared/service-env.js.
const REVIEW_CONTROL_SECRET = require('../shared/service-env')
  .readServiceSecret('REVIEW_CONTROL_SECRET', AGENT_HOME);

function log(msg) {
  console.log(`[${new Date().toISOString()}] ${msg}`);
}

// Inherits this process's environment: git, mount and gitleaks only.
// Anything that runs agent-authored code goes through runAgentCode.
function run(cmd, args, cwd, timeoutMs = 300_000) {
  return new Promise((resolve) => {
    const opts = { cwd, maxBuffer: 20 * 1024 * 1024, timeout: timeoutMs };
    execFile(cmd, args, opts, (err, stdout, stderr) => {
      resolve({ ok: !err, output: (stdout || '') + (stderr || ''), code: err ? (err.code ?? 1) : 0 });
    });
  });
}

// audit C-2 (reviewer side): the check/build commands run agent-authored code
// -- npm scripts and the test-writer's own test files. Since 2026-09-21
// checks, the build, package-manager installs, schema generation and the
// build assertions all go through runAgentCode: a sandbox container on a
// host install, one the agent starts for us in the bundle.
//
// sealedEnv is applied inside it as well, and is not redundant: containment
// stops the code reaching the machine, the sealed environment stops it
// reading the reviewer's own variables on the way past.
//
// On a host install db:drift / db:seed / test:e2e still run on the host,
// because they talk to Postgres and Redis on loopback and inside a container
// "localhost" is the container. The bundle gives them their own
// checks-postgres and checks-redis and runs them in the sandbox like every
// other check (agent/review_sandbox.py, since 2026-09-27). SECURITY.md has
// the reasoning.
function sealedEnv(extra) {
  return {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    LANG: process.env.LANG || 'C.UTF-8',
    CI: 'true',
    DEBIAN_FRONTEND: 'noninteractive',
    ...(extra || {}),
  };
}

// Like run(), but with a sealed env and no process.env merge. For commands that
// execute agent-authored code (checks, build, db checks).
function runSealed(cmd, args, cwd, timeoutMs = 300_000, extraEnv) {
  return new Promise((resolve) => {
    const opts = { cwd, maxBuffer: 20 * 1024 * 1024, timeout: timeoutMs, env: sealedEnv(extraEnv) };
    execFile(cmd, args, opts, (err, stdout, stderr) => {
      resolve({ ok: !err, output: (stdout || '') + (stderr || ''), code: err ? (err.code ?? 1) : 0 });
    });
  });
}
// On every git call, with hooks and fsmonitor off: the tree git runs in is
// agent-authored. The flags are GIT_SAFE in services/shared/review-records.js,
// the one copy agent-review's git runs with too.
const { GIT_SAFE } = require('../shared/review-records');
const git = (cwd, args) => run('git', [...GIT_SAFE, ...args], cwd);

// Where agent-authored code runs. Everything below that executes something
// the agent could have written -- checks, the build -- goes through this
// rather than calling runSealed directly, so there is ONE answer to "is this
// contained" instead of one per call site. That answer has three outcomes,
// by deployment: a host install runs it in a sandbox container; the compose
// bundle asks the agent to start the same container (sandbox.js, "THE BUNDLE
// DELEGATES"); anything else refuses. Never this process: it holds the
// secret that authorises a merge. The one exception is runDatabaseCheck's
// three commands on a host install -- SECURITY.md, "The database checks:
// contained in the bundle, on the host still not" -- which the bundle hands
// to the agent whole (sandbox.js runDelegatedDatabaseCheck).
//
// sealedEnv is still applied inside the container: it stops secrets reaching
// the command, which containment does not do on its own.
async function runAgentCode(cfg, worktreePath, relDir, cmd, args, timeoutMs, extraEnv, network, stack) {
  const mode = await sandbox.probe({ secret: REVIEW_CONTROL_SECRET });
  if (mode.mode === 'sandbox') {
    return sandbox.runSandboxed(cfg, worktreePath, relDir, cmd, args, timeoutMs,
                                sealedEnv(extraEnv), network, stack);
  }
  if (mode.mode === 'delegated') {
    return sandbox.runDelegated(cfg, worktreePath, relDir, cmd, args, timeoutMs,
                                extraEnv, network, stack, { secret: REVIEW_CONTROL_SECRET });
  }
  // `unavailable`, and any answer this function does not know, refuse the
  // same way. Flagged as infrastructure at the source: everything downstream
  // that decides whose problem a failure is reads `.infrastructure`, and
  // deriving it by matching the message text again would be a second place
  // for the two to disagree, which costs an agent several rounds debugging
  // an environment it cannot see.
  return { ...(await unavailable(mode)), infrastructure: true };
}

// Fail closed. Running on the host instead would be the escalation this
// exists to close, and "fall back when the sandbox is unavailable" is the
// path anyone attacking it would engineer. Not a new fragility: the agent's
// own bash already requires Docker, so a box without it is not producing
// work to review.
async function unavailable(mode) {
  return {
    ok: false,
    code: 1,
    output: `SETUP: this check runs code the agent wrote and cannot be contained here -- ${mode.reason}. `
          + `Build the sandbox image (docker/agent-sandbox), or in the bundle set AGENT_SANDBOX_URL. `
          + `It was not run on the host, and nothing about the code under review is known either way.`,
  };
}

module.exports = {
  AGENT_HOME, REVIEW_CONTROL_SECRET, log, run, sealedEnv, runSealed, GIT_SAFE, git, runAgentCode,
};
