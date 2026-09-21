#!/usr/bin/env node
'use strict';
/**
 * Can every configured check actually run in the sandbox?
 *
 * Since 2026-09-21 the reviewer runs each project's checks inside the agent's
 * sandbox container rather than on the host (SECURITY.md). That is strictly
 * better for a Node or Python project, because the image carries those
 * toolchains -- and it is a wall for anyone whose project is Go, Rust, Ruby,
 * Java, .NET, PHP or Elixir, all of which agent/provisioning.py happily
 * DETECTS and configures checks for.
 *
 * Without this, that user finds out at their first merge: every check comes
 * back as a setup error, on a commit that was probably fine. The point of
 * this script is that they find out when they add the project instead.
 *
 *   node scripts/check_sandbox_tools.js            all configured projects
 *   node scripts/check_sandbox_tools.js <project>  one of them
 *
 * Exit 0 when every check's command exists in the image, 1 otherwise, so it
 * can gate an onboarding step or a CI job. It probes for the COMMAND only --
 * whether the check passes is the reviewer's business, not this script's.
 */

const path = require('path');
const { execFile } = require('child_process');

const ROOT = path.join(__dirname, '..');
const sandbox = require(path.join(ROOT, 'services', 'commit-reviewer', 'sandbox'));
const { loadProjects } = require(path.join(ROOT, 'services', 'shared', 'projects-config'));

let BUILTIN = {};
try {
    BUILTIN = require(path.join(ROOT, 'services', 'commit-reviewer', 'builtin-projects.local'));
} catch { /* an installation with no local built-ins is the normal case */ }

function has(tool, image) {
    return new Promise((resolve) => {
        execFile('docker', [
            'run', '--rm', '--network', 'none', '--entrypoint', 'sh',
            image, '-c', `command -v ${JSON.stringify(tool)} >/dev/null`,
        ], { timeout: 120_000 }, (err) => resolve(!err));
    });
}

/** Whether the image is on this host at all, which is its own failure. */
function pulled(image) {
    return new Promise((resolve) => {
        execFile('docker', ['image', 'inspect', image, '--format', '{{.Id}}'],
                 { timeout: 60_000 }, (err) => resolve(!err));
    });
}

async function main() {
    const only = process.argv[2];
    const mode = await sandbox.probe();
    if (mode.mode !== 'sandbox') {
        // In the bundle checks do not run in a container at all, so there is
        // nothing here to verify; on a host that cannot contain them the
        // reviewer refuses anyway and says so itself.
        console.log(`nothing to check: ${mode.mode} -- ${mode.reason}`);
        return 0;
    }

    const projects = loadProjects(BUILTIN, { section: 'review' });
    const names = only ? [only] : Object.keys(projects);
    if (only && !projects[only]) {
        console.error(`unknown project ${only}; configured: ${Object.keys(projects).join(', ') || '(none)'}`);
        return 1;
    }

    // Keyed on (image, tool): the same command can be wanted in two images
    // -- a monorepo's Go backend and Node frontend both want `make` -- and
    // "is it there" has a different answer in each.
    const wanted = new Map();
    const add = (stack, cmd, who) => {
        const { image } = sandbox.imageFor(stack);
        const key = `${image}\u0000${cmd}`;
        if (!wanted.has(key)) wanted.set(key, { image, cmd, users: [] });
        wanted.get(key).users.push(who);
    };
    for (const name of names) {
        const p = projects[name];
        for (const c of p.checks || []) add(c.stack || p.stack, c.cmd, `${name}/${c.name}`);
        const b = p.build;
        const bc = Array.isArray(b) ? b[0] : b;
        if (bc && bc.cmd) add(bc.stack || p.stack, bc.cmd, `${name}/build`);
    }

    if (!wanted.size) {
        console.log('no checks configured on any project -- nothing runs, so nothing to verify');
        return 0;
    }

    const missing = [];
    const unpulled = new Set();
    const imagesSeen = new Set();
    for (const { image, cmd, users } of [...wanted.values()].sort(
            (a, b) => (a.image + a.cmd).localeCompare(b.image + b.cmd))) {
        if (!imagesSeen.has(image)) {
            imagesSeen.add(image);
            if (!await pulled(image)) unpulled.add(image);
        }
        if (unpulled.has(image)) {
            console.log(`  PULL  ${cmd.padEnd(12)} ${image}  (${users.join(', ')})`);
            continue;
        }
        const ok = await has(cmd, image);
        console.log(`  ${ok ? 'ok  ' : 'MISS'}  ${cmd.padEnd(12)} ${image}  (${users.join(', ')})`);
        if (!ok) missing.push({ cmd, image, users });
    }

    if (!missing.length && !unpulled.size) {
        console.log(`\nevery configured check can run in its image (${[...imagesSeen].join(', ')})`);
        return 0;
    }

    if (unpulled.size) {
        console.log(`\n${unpulled.size} image(s) not on this host yet:`);
        for (const i of unpulled) console.log(`  docker pull ${i}`);
        console.log('A check whose image is missing returns a SETUP error rather than running.');
    }
    for (const m of missing) {
        console.log(`\n${m.cmd} is not in ${m.image} — needed by ${m.users.join(', ')}`);
        console.log('  Either add it to that image, or correct the check\'s `stack` in projects.json.');
    }
    console.log('\nThe reviewer will not fall back to the host: that is the escalation the');
    console.log('sandbox exists to close (SECURITY.md).');
    return 1;
}

main().then((c) => process.exit(c)).catch((e) => {
    console.error(e && e.message ? e.message : e);
    process.exit(1);
});
