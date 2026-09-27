// Running agent-authored code somewhere it cannot reach this machine.
//
// THE PROBLEM. reviewProject runs each project's configured checks -- `npm
// test` and friends -- against the agent's worktree, with execFile, on the
// host, as root. The agent's own shell is sandboxed, but its write/edit tools
// put files in that worktree, so a test file it wrote is arbitrary code
// executing as root outside any container. sealedEnv() stops secrets leaking
// INTO those commands; it does nothing about what they can do once running.
// That is a complete path from "model in a container" to "root on the box",
// and it is what this module closes.
//
// WHY THIS IS NOT A LEAP. The commands are not new to a container. The agent
// already runs the same cfg.checks list, in the same worktree, through
// agent/tools/sandbox.py's run_shell_sandboxed with --network none, and its
// run_checks tool describes them as "the exact same commands the outer
// verification gate will run". Every project that has completed a task has
// already proven its checks work in this image. The flags below deliberately
// mirror that call site rather than inventing a second hardening policy --
// two policies drift, and the weaker one is the one that matters.
//
// THE BUNDLE DELEGATES. In the compose bundle this service runs INSIDE a
// container that is not given /var/run/docker.sock (docker-compose.yml gives
// it to `agent` alone), and handing it the socket would give it host-root
// equivalent. It used to run checks in-process instead, on the theory that
// the container was containment enough. It was not: this container holds the
// review-control secret and can reach the merge endpoint, so a test file that
// read the secret and POSTed `force: true` merged its own branch past the
// gate. Now the bundle asks the agent -- which has the socket, and runs the
// same checks for its own run_checks tool -- to start the same hardened
// container (agent/review_sandbox.py, which re-validates every path). Without
// AGENT_SANDBOX_URL the bundle refuses, like any other host that cannot
// contain the code.
//
// FAIL CLOSED. If Docker or the image is missing on a host install, a check
// does not quietly run on the host instead: "fall back when the sandbox
// fails" is the path anyone attacking this would engineer. It is not a new
// fragility either -- the agent's own bash already requires Docker, so a box
// without it is not running tasks to review.
'use strict';

const { execFile } = require('child_process');
const fs = require('fs');
const path = require('path');

// Mirrors agent/tools/sandbox.py. Kept as literals with the source named
// rather than read from Python, because a reviewer that cannot parse the
// agent's config should still run with the documented limits.
// One copy of the stack->image map, shared with scripts/verify_stack_checks.py
// and agent/provisioning.py. See docker/stack-images.json for why.
let STACKS = { default: 'tektonix-sandbox:latest', stacks: {} };
try {
    STACKS = JSON.parse(fs.readFileSync(
        path.join(__dirname, '..', '..', 'docker', 'stack-images.json'), 'utf8'));
} catch {
    // A missing map is not fatal: every project without a declared stack runs
    // in the default image, which is the behaviour that existed before stacks
    // were selectable at all.
}

const IMAGE = process.env.AGENT_SANDBOX_IMAGE || STACKS.default || 'tektonix-sandbox:latest';

/**
 * The image and extra env a check runs with.
 *
 * A check may name its own `stack` -- set at onboarding from what
 * provisioning detected. Per CHECK, not per project, because a Go backend
 * with a React frontend is an ordinary repository and its two checks belong
 * in two different images.
 *
 * An unknown stack falls back to the default rather than failing: the map can
 * gain entries after a project's config was written, and refusing to run a
 * check because its label is new is worse than running it where it probably
 * works.
 */
function imageFor(stack) {
    const entry = stack && STACKS.stacks ? STACKS.stacks[stack] : null;
    return {
        image: (entry && entry.image) || IMAGE,
        env: (entry && entry.env) || {},
        known: Boolean(entry),
    };
}
const MEMORY = '2g';
const CPUS = '2';
const PIDS = '512';

const IN_CONTAINER = process.env.TEKTONIX_BUNDLE === '1';
const DELEGATE_URL = (process.env.AGENT_SANDBOX_URL || '').replace(/\/+$/, '');

function execp(cmd, args, opts = {}) {
    return new Promise((resolve) => {
        execFile(cmd, args, { maxBuffer: 20 * 1024 * 1024, ...opts }, (err, stdout, stderr) => {
            resolve({ ok: !err, code: err ? (err.code ?? 1) : 0, out: (stdout || '') + (stderr || '') });
        });
    });
}

let _probe = null;

/**
 * Whether checks can be sandboxed here, cached for the process.
 *
 * Three answers: `sandbox` (a host install with docker and the image),
 * `delegated` (the bundle, where the agent starts the container), and
 * `unavailable`, which the caller must refuse on. There is no answer that
 * means "run it here": this process holds the secret that authorises merges.
 */
async function probe({ secret, fetchImpl = fetch } = {}) {
    if (_probe) return _probe;
    if (IN_CONTAINER) {
        if (!DELEGATE_URL) {
            _probe = { mode: 'unavailable',
                       reason: 'this service runs in a container that is not given the docker socket, and '
                             + 'AGENT_SANDBOX_URL is unset, so there is no agent to start the sandbox for it' };
            return _probe;
        }
        // Asked of the agent, the way a host install asks docker: whether the
        // image is there. Only a yes is remembered -- a no (the agent down, the
        // image still building after a failed boot build) is asked again next
        // review, and the agent builds the image when it is asked and it is
        // missing (2026-09-27: before this, a missing image failed every check
        // identically on the base commit and read as pre-existing).
        const answer = await delegatedProbe({ secret, fetchImpl });
        if (answer.mode === 'delegated') _probe = answer;
        return answer;
    }
    const d = await execp('docker', ['version', '--format', '{{.Server.Version}}']);
    if (!d.ok) {
        _probe = { mode: 'unavailable', reason: `docker is not usable here: ${d.out.trim().slice(0, 200)}` };
        return _probe;
    }
    const img = await execp('docker', ['image', 'inspect', IMAGE, '--format', '{{.Id}}']);
    if (!img.ok) {
        _probe = { mode: 'unavailable', reason: `the sandbox image ${IMAGE} is not built on this host` };
        return _probe;
    }
    _probe = { mode: 'sandbox', reason: `${IMAGE} on docker ${d.out.trim()}` };
    return _probe;
}

async function delegatedProbe({ secret, fetchImpl = fetch } = {}) {
    if (!secret) {
        return { mode: 'unavailable',
                 reason: 'REVIEW_CONTROL_SECRET is not configured, so the agent would refuse to run anything' };
    }
    let res;
    try {
        res = await fetchImpl(`${DELEGATE_URL}/api/internal/review-sandbox/probe`, {
            headers: { 'x-review-secret': secret }, signal: AbortSignal.timeout(30_000),
        });
    } catch (err) {
        return { mode: 'unavailable',
                 reason: `the agent's sandbox endpoint did not answer (${String(err && err.message || err).slice(0, 200)})` };
    }
    let data = null;
    try { data = await res.json(); } catch { /* reported below */ }
    if (!res.ok || !data || typeof data !== 'object') {
        const detail = data && (data.detail || data.error);
        return { mode: 'unavailable',
                 reason: `the agent refused the probe (HTTP ${res.status}${detail ? `: ${String(detail).slice(0, 200)}` : ''})` };
    }
    if (!data.ok) return { mode: 'unavailable', reason: String(data.reason || 'the agent cannot run a sandbox right now') };
    return { mode: 'delegated', image: data.image,
             reason: `checks run in ${data.image || 'the sandbox image'} started by the agent (${DELEGATE_URL})` };
}

function resetProbe() { _probe = null; }   // tests only

/**
 * The -v arguments a worktree needs for its checks to behave as they do on
 * the host.
 *
 * A plain `-v <worktree>:/workspace` is not enough, and getting this wrong is
 * how the change would have broken every project: the reviewer brings
 * dependencies in with `mount --bind` on the host, and a bind mount NESTED
 * inside a directory is not carried into a container by a plain bind of its
 * parent. node_modules would be an empty directory inside, and every `npm
 * test` would fail with "not found" while passing on the host.
 *
 * So each declared dependency and read-only mount is bound explicitly from
 * its source on the live checkout -- the same source the host bind uses --
 * read-only, because a check must never write into live's installed
 * dependencies or its data.
 *
 * The worktree's .git is a pointer FILE to <live>/.git/worktrees/<name>, so
 * git inside the container needs the live .git at the same absolute path, or
 * every `git` in a check dies with "not a git repository".
 */
function isInside(child, parent) {
    const rel = path.relative(path.resolve(parent), path.resolve(child));
    return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel));
}

/** Relative paths of symlinked node_modules directories, up to three deep. */
function nodeModulesLinks(root, depth = 0, rel = '') {
    if (depth > 3) return [];
    const out = [];
    let entries;
    try {
        entries = fs.readdirSync(path.join(root, rel), { withFileTypes: true });
    } catch {
        return out;
    }
    for (const e of entries) {
        const here = path.join(rel, e.name);
        if (e.name === 'node_modules') {
            if (e.isSymbolicLink()) out.push(here);
            continue;   // never descend into one
        }
        if (e.isDirectory() && !e.name.startsWith('.')) {
            out.push(...nodeModulesLinks(root, depth + 1, here));
        }
    }
    return out;
}

/** The -v arguments, for a container this process starts itself. */
function mountArgs(cfg, worktreePath) {
    const args = ['-v', `${worktreePath}:/workspace`];
    for (const m of mountSpecs(cfg, worktreePath)) args.push('-v', `${m.src}:${m.dst}:ro`);
    return args;
}

/**
 * Every read-only mount beyond the worktree itself, as {src, dst}. The
 * delegated path sends these to the agent as data rather than as -v strings,
 * so nothing has to split a path on ':' to get them back.
 */
function mountSpecs(cfg, worktreePath) {
    const specs = [];
    const seen = new Set();

    for (const rel of [...(cfg.dependencyDirs || []), ...(cfg.readOnlyMounts || [])]) {
        const src = path.join(cfg.live, rel);
        if (seen.has(rel) || !fs.existsSync(src)) continue;
        seen.add(rel);
        specs.push({ src, dst: path.posix.join('/workspace', rel) });
    }

    // The other half of the same problem, and the one that actually bit:
    // some worktrees SYMLINK node_modules to the live checkout's copy rather
    // than holding their own. A symlink is carried into the container
    // faithfully and then dangles, so `npx eslint` dies with "not found"
    // inside while passing on the host. Mount each such target read-only at
    // its own absolute path so the link resolves -- the same fix, and the
    // same reasoning, as _node_modules_mounts in agent/tools/sandbox.py.
    //
    // Read-only because a check must never write into live's installed
    // dependencies. Bounded to three levels: deep enough for a monorepo's
    // per-package node_modules, shallow enough not to walk a whole tree.
    for (const rel of nodeModulesLinks(worktreePath)) {
        // A DANGLING link throws here rather than resolving, and an agent that
        // deleted a directory its symlink pointed at is an ordinary state for
        // a worktree to be in. Unguarded, that ENOENT came out of mountArgs
        // and failed the entire review rather than skipping one mount.
        let target;
        try {
            target = fs.realpathSync(path.join(worktreePath, rel));
        } catch {
            continue;
        }
        // Agent-writable worktree, so the link target is agent-controlled:
        // only the project's own live checkout is an acceptable destination.
        if (!isInside(target, cfg.live) || seen.has(target)) continue;
        seen.add(target);
        specs.push({ src: target, dst: target });
    }

    // ...and the layout the loop above cannot see. When a project's package
    // directories carry named package.json files, setupWorktree builds a REAL
    // node_modules/ directory and symlinks each ENTRY inside it into live's
    // copy (so a workspace-internal package can be redirected to the
    // worktree's own build). node_modules itself is then not a link, the loop
    // above finds nothing, and every entry dangles inside the container.
    //
    // Found 2026-09-22 on a project of three standalone apps: once the review
    // service finally knew which directories needed node_modules, the checks
    // STILL failed with `oxlint: not found`, because frontend/node_modules/.bin
    // pointed at /home/<project>/frontend/node_modules/.bin -- a path the
    // container had never been given. Mounting each configured directory's
    // live node_modules read-only at its own absolute path makes every such
    // entry resolve, and it is the same mount the symlink layout already gets.
    for (const rel of cfg.nodeModulesDirs || []) {
        const liveNm = path.join(cfg.live, rel, 'node_modules');
        let target;
        try {
            target = fs.realpathSync(liveNm);
        } catch {
            continue;                       // not installed in live: nothing to mount
        }
        if (!isInside(target, cfg.live) || seen.has(target)) continue;
        seen.add(target);
        specs.push({ src: target, dst: target });
    }

    const dotgit = path.join(worktreePath, '.git');
    try {
        if (fs.statSync(dotgit).isFile()) {
            const gitdir = fs.readFileSync(dotgit, 'utf8').replace(/^gitdir:/, '').trim();
            const marker = `${path.sep}worktrees${path.sep}`;
            if (gitdir.includes(marker)) {
                const mainGit = gitdir.slice(0, gitdir.indexOf(marker));
                // The pointer file lives inside a directory the agent can
                // write, so the path in it is agent-controlled: `gitdir:
                // /root/x` would otherwise mount /root into the container.
                // Only the live checkout's own .git is accepted.
                const expected = path.join(cfg.live, '.git');
                if (path.resolve(mainGit) === path.resolve(expected) && fs.existsSync(mainGit)) {
                    specs.push({ src: mainGit, dst: mainGit });
                }
            }
        }
    } catch {
        // An unreadable pointer means git will not work inside; a check that
        // needs git fails loudly there rather than silently running on the host.
    }
    return specs;
}

/**
 * Run one check command inside the sandbox.
 *
 * argv, never a shell: the reviewer's own runSealed uses execFile for the
 * same reason, and passing `cmd args...` to `bash -c` here would ADD an
 * injection surface the host path does not have.
 *
 * `--network none` by default, matching the agent's own check runner:
 * dependencies are installed by the time checks run, and nothing here may
 * install (see reviewProject's note on why an install would execute
 * untrusted code).
 *
 * A check may opt out with `network: "bridge"` in projects.json, and one
 * real check needs it -- `pnpm audit` queries an advisory database and fails
 * with no egress. That opt-in is deliberately in the project CONFIG, which
 * the agent cannot write: it lives outside the worktree, so a model cannot
 * grant its own code network access by editing a file. What the operator IS
 * agreeing to, per check, is that this one command runs agent-authored code
 * WITH egress -- which is why it is per check and not a global switch.
 */
/**
 * The exact argv handed to docker. Pure, and exported, so the hardening can
 * be ASSERTED rather than grepped for: --cap-drop ALL and --network none are
 * the difference between containment and a container, and a source grep
 * passes for a flag that has been moved into a branch that never runs.
 */
function dockerArgs(cfg, worktreePath, relDir, cmd, args, extraEnv, network, stack) {
    // A check's own stack wins; then the project's own image
    // (projects.json `sandbox_image`), then the project's stack, then the
    // default -- the same order agent/tools/sandbox.py uses, so the agent
    // and its reviewer run a project's code in the same environment.
    const target = imageFor(stack || (cfg && cfg.stack));
    if (!stack && cfg && cfg.sandboxImage) target.image = cfg.sandboxImage;
    const envArgs = [];
    for (const [k, v] of Object.entries({
        CI: 'true', DEBIAN_FRONTEND: 'noninteractive', LANG: 'C.UTF-8',
        // The toolchain's own needs first, so a check may still override
        // them: with no network and a read-only HOME, Go wants a GOCACHE it
        // can write and Maven a local repository, or nothing runs at all.
        ...target.env, ...(extraEnv || {}),
    })) {
        envArgs.push('-e', `${k}=${v}`);
    }

    const docker = [
        'run', '--rm',
        ...mountArgs(cfg, worktreePath),
        '--network', network === 'bridge' ? 'bridge' : 'none',
        '-w', path.posix.join('/workspace', relDir || '.'),
        '--memory', MEMORY,
        '--cpus', CPUS,
        '--pids-limit', PIDS,
        '--cap-drop', 'ALL',
        '--security-opt', 'no-new-privileges',
        // --entrypoint, not `IMAGE cmd args`. This image is built on the
        // Node base, which ships ENTRYPOINT ["docker-entrypoint.sh"]; that
        // script hands anything it does not recognise to `node`, so a check
        // whose tool is missing came back as a Node module stack trace
        // instead of "not found". Setting the entrypoint bypasses it and
        // gives docker's own, unambiguous:
        //   exec: "go": executable file not found in $PATH
        // which is what lets a missing toolchain be reported as a setup
        // problem rather than as a failing check.
        '--entrypoint', cmd,
        ...envArgs,
        target.image,
        ...(args || []),
    ];

    return { docker, image: target.image };
}


async function runSandboxed(cfg, worktreePath, relDir, cmd, args, timeoutMs, extraEnv, network, stack) {
    const { docker, image } = dockerArgs(cfg, worktreePath, relDir, cmd, args, extraEnv, network, stack);
    const r = await execp('docker', docker, { timeout: timeoutMs || 300_000 });
    return classify(r, image);
}

/**
 * The body POSTed to the agent for one check. Pure and exported, so what the
 * bundle sends can be asserted without an agent to send it to.
 *
 * The env is the check's own and the fixed CI variables -- never PATH or
 * HOME: those are THIS container's, and a toolchain image (Go puts its
 * binaries in /usr/local/go/bin) needs its own.
 */
function delegatedRequest(cfg, worktreePath, relDir, cmd, args, timeoutMs, extraEnv, network, stack) {
    const env = { LANG: 'C.UTF-8', CI: 'true', DEBIAN_FRONTEND: 'noninteractive', ...(extraEnv || {}) };
    delete env.PATH;
    delete env.HOME;
    const base = path.basename(worktreePath);
    const m = /^(.+)-[0-9a-f]{7,40}$/.exec(base);
    return {
        project: cfg.name || (m ? m[1] : base),
        worktree: worktreePath,
        relDir: relDir || '.',
        cmd,
        args: args || [],
        env,
        network: network === 'bridge' ? 'bridge' : 'none',
        stack: stack || null,
        mounts: mountSpecs(cfg, worktreePath),
        timeoutMs: timeoutMs || 300_000,
    };
}

/**
 * Run one check in a container the AGENT starts. See the header: this is
 * how the bundle contains agent-authored code without this service holding
 * the docker socket or running the code itself.
 */
async function runDelegated(cfg, worktreePath, relDir, cmd, args, timeoutMs, extraEnv, network, stack,
                            { secret, fetchImpl = fetch } = {}) {
    const body = delegatedRequest(cfg, worktreePath, relDir, cmd, args, timeoutMs, extraEnv, network, stack);
    const setup = (why) => ({
        ok: false, code: 1, infrastructure: true,
        output: `SETUP: this check runs code the agent wrote and could not be sandboxed -- ${why}. `
              + 'It was not run here, and nothing about the code under review is known either way.',
    });
    if (!secret) return setup('REVIEW_CONTROL_SECRET is not configured, so the agent would refuse the request');
    let res;
    try {
        res = await fetchImpl(`${DELEGATE_URL}/api/internal/review-sandbox/run`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-review-secret': secret },
            body: JSON.stringify(body),
            signal: AbortSignal.timeout(body.timeoutMs + 60_000),
        });
    } catch (err) {
        return setup(`the agent's sandbox endpoint did not answer (${String(err && err.message || err).slice(0, 200)})`);
    }
    let data = null;
    try { data = await res.json(); } catch { /* reported below */ }
    if (!res.ok || !data || typeof data !== 'object') {
        const detail = data && (data.detail || data.error);
        return setup(`the agent refused it (HTTP ${res.status}${detail ? `: ${String(detail).slice(0, 300)}` : ''})`);
    }
    if (data.infrastructure) return { ok: false, code: data.code ?? 1, infrastructure: true, output: String(data.output || '') };
    return classify({ ok: Boolean(data.ok), code: data.code ?? (data.ok ? 0 : 1), out: String(data.output || '') },
                    data.image || imageFor(stack || cfg.stack).image);
}

function classify(r, image) {
    const missing = missingTool(r.out);
    if (missing) {
        // A toolchain the image does not carry is a SETUP problem, not a
        // failing check, and the difference decides who fixes it. Left as a
        // raw docker error it reads as "the agent broke the build", the
        // agent is asked to debug an environment it cannot see, and it tries
        // -- which is how a correct commit gets rejected round after round
        // (the same reasoning as MISSING_TOOL_RE in reviewer.js).
        return {
            ok: false,
            code: r.code,
            missingTool: missing,
            output: `SETUP: this check needs \`${missing}\`, which the sandbox image `
                  + `(${image}) does not have. The image carries Node and Python; a project on `
                  + `another toolchain needs it added to docker/agent-sandbox/Dockerfile, or this `
                  + `check removed from the project's config. Nothing about the code under review `
                  + `is known either way -- the check did not run.`,
        };
    }
    return { ok: r.ok, output: r.out, code: r.code };
}

// Docker's own words when the image has no such binary. Deliberately narrow:
// it must not match a program that merely PRINTS something similar, because
// mislabelling a real failure as a setup problem hides a genuine break.
const NOT_IN_IMAGE_RE = /exec: "([^"]+)": executable file not found in \$PATH/;

function missingTool(output) {
    const m = NOT_IN_IMAGE_RE.exec(output || '');
    return m ? m[1] : null;
}

module.exports = { probe, delegatedProbe, resetProbe, runSandboxed, runDelegated, delegatedRequest, dockerArgs, mountArgs,
                   mountSpecs, nodeModulesLinks, isInside, missingTool, imageFor, STACKS, IMAGE, IN_CONTAINER,
                   DELEGATE_URL };
