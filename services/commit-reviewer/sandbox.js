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
// WHY THE BUNDLE IS EXEMPT. In the compose bundle this service runs INSIDE a
// container that is not given /var/run/docker.sock (docker-compose.yml gives
// it to `agent` alone). It cannot start a container, and handing it the
// socket so that it could would give that container host-root equivalent --
// strictly worse than the containment it already has. So on the bundle the
// caller keeps running checks in-process, and only a host install, where the
// reviewer really is root on the real machine, routes through here.
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
 * Three answers, not two: `bundle` is "already contained, do not try", which
 * a caller must not confuse with `unavailable` -- one is correct operation
 * and the other is a host install that has to refuse.
 */
async function probe() {
    if (_probe) return _probe;
    if (IN_CONTAINER) {
        _probe = { mode: 'bundle', reason: 'this service runs in a container that is not given the docker socket' };
        return _probe;
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

function mountArgs(cfg, worktreePath) {
    const args = ['-v', `${worktreePath}:/workspace`];
    const seen = new Set();

    for (const rel of [...(cfg.dependencyDirs || []), ...(cfg.readOnlyMounts || [])]) {
        const src = path.join(cfg.live, rel);
        if (seen.has(rel) || !fs.existsSync(src)) continue;
        seen.add(rel);
        args.push('-v', `${src}:${path.posix.join('/workspace', rel)}:ro`);
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
        args.push('-v', `${target}:${target}:ro`);
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
        args.push('-v', `${target}:${target}:ro`);
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
                    args.push('-v', `${mainGit}:${mainGit}:ro`);
                }
            }
        }
    } catch {
        // An unreadable pointer means git will not work inside; a check that
        // needs git fails loudly there rather than silently running on the host.
    }
    return args;
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
    const target = imageFor(stack);
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

module.exports = { probe, resetProbe, runSandboxed, dockerArgs, mountArgs, nodeModulesLinks, isInside,
                   missingTool, imageFor, STACKS, IMAGE, IN_CONTAINER };
