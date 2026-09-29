'use strict';
/**
 * Where a review borrows a project's installed node_modules from: live's
 * own install, or nowhere.
 *
 * In the bundle live is the operator's checkout: cloned by the app it has
 * no install at all, and cloned on Windows it has one built for Windows
 * (win32 esbuild and rollup binaries, .cmd shims) that a Linux check
 * container cannot run. For one day (2026-09-29) the agent's own workspace
 * template (cfg.sandbox) was the second candidate. It is not one any more:
 * every task workspace is a HARDLINK copy of that template
 * (agent/workspaces.py), so a task command that rewrites a file in
 * node_modules in place -- `printf 'exit 0' > node_modules/.bin/eslint`
 * -- changes the tool the review would then run, and the read-only mount
 * does not help because the write happened before the review. When live's
 * install cannot be borrowed the review installs its own (worktree.js,
 * the same --ignore-scripts install a changed lockfile gets), which no
 * task shares.
 */
const fs = require('fs');
const path = require('path');

/** True when an install could not have been made by a Linux node. */
function foreignInstall(dir) {
    try {
        const bin = path.join(dir, '.bin');
        if (fs.existsSync(bin) && fs.readdirSync(bin).some((f) => /\.(cmd|ps1)$/i.test(f))) return true;
        for (const [scope, prefix] of [['@esbuild', 'linux-'], ['@rollup', 'rollup-linux-']]) {
            const scoped = path.join(dir, scope);
            if (!fs.existsSync(scoped)) continue;
            const entries = fs.readdirSync(scoped);
            if (entries.length && !entries.some((e) => e.startsWith(prefix))) return true;
        }
    } catch {
        return false;
    }
    return false;
}

/**
 * The directory to borrow for `<rel>/node_modules`, or null when live has
 * no usable one. `which` is always 'live'; kept in the shape callers read.
 */
function nodeModulesSource(cfg, rel) {
    if (!cfg.live) return null;
    const dir = path.join(cfg.live, rel, 'node_modules');
    if (!fs.existsSync(dir) || foreignInstall(dir)) return null;
    return { dir, which: 'live' };
}

/**
 * The configured package directories whose node_modules live cannot lend:
 * none installed, or an install a Linux container cannot run. Any of them
 * means the review installs its own dependencies rather than borrowing.
 */
function unborrowable(cfg) {
    return (cfg.nodeModulesDirs || []).filter((rel) => !nodeModulesSource(cfg, rel));
}

module.exports = { nodeModulesSource, foreignInstall, unborrowable };
