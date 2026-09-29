'use strict';
/**
 * Where a review borrows a project's installed node_modules from.
 *
 * Live's own install first, as always. In the bundle live is the operator's
 * checkout: cloned by the app it has no install at all, and cloned on Windows
 * it has one built for Windows (win32 esbuild and rollup binaries, .cmd
 * shims) that a Linux check container cannot run. The agent's own workspace
 * template (cfg.sandbox) holds a Linux install made for this project, so it
 * is the second candidate. 2026-09-29, the first Windows install.
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
 * The directory to borrow for `<rel>/node_modules`, or null when neither
 * live nor the template has a usable one. `reason` says which was taken.
 */
function nodeModulesSource(cfg, rel) {
    const candidates = [['live', cfg.live], ['template', cfg.sandbox]];
    for (const [which, root] of candidates) {
        if (!root) continue;
        const dir = path.join(root, rel, 'node_modules');
        if (!fs.existsSync(dir)) continue;
        if (foreignInstall(dir)) continue;
        return { dir, which };
    }
    return null;
}

module.exports = { nodeModulesSource, foreignInstall };
