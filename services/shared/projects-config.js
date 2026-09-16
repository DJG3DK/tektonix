'use strict';
/**
 * One source of truth for which projects exist, shared by the reviewer and
 * the deploy service.
 *
 * Before this, three files each carried their own project map: the agent's
 * projects.json (paths), commit-reviewer/reviewer.js (check commands, secret
 * files, mounts) and agent-review/server.js (build steps, pm2 apps). Adding a
 * project meant hand-editing all three, and a project added to one but not
 * the others silently half-worked -- the agent could build in it while the
 * reviewer ignored its commits.
 *
 * The merge rule is deliberately "built-in wins":
 *
 *   final = { ...fromProjectsJson, ...builtinOverride }
 *
 * The three original projects carry hand-tuned configs that encode real
 * incidents -- a test suite that POSTed live trade orders, a Prisma client
 * that had to be regenerated before a build could typecheck, a storefront
 * prerender step whose absence 404'd a production catalog. None of that is
 * derivable, so the built-in entries stay authoritative and are never
 * overwritten by generated config. Projects onboarded through the wizard
 * have no built-in entry, so they run entirely on projects.json.
 *
 * Read fresh on each call (cheap, small file), and both services call it
 * per poll tick / per request instead of binding the result at startup.
 * Until 2026-09-16 they bound it once, so a project created or given
 * checks at runtime was invisible until a pm2 restart: the reviewer never
 * polled the new repo, and the agent's wait_for_review timed out against
 * a verdict that could not arrive.
 */

const fs = require('fs');
const path = require('path');

const PROJECTS_JSON = process.env.AGENT_PROJECTS_JSON
    || path.join(__dirname, '..', '..', 'projects.json');

function readProjectsJson(file = PROJECTS_JSON) {
    try {
        const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
        return (parsed && parsed.projects) || {};
    } catch (err) {
        // A missing or malformed projects.json must not take down a running
        // reviewer -- it falls back to the built-in map and says so.
        if (err.code !== 'ENOENT') {
            console.error(`[projects-config] ${file} unreadable (${err.message}) — using built-ins only`);
        }
        return {};
    }
}

/**
 * @param {object} builtins  the service's own hand-tuned map (authoritative)
 * @param {object} [opts]
 * @param {'review'|'deploy'} opts.section  which sub-object of a projects.json
 *        entry carries this service's fields
 * @param {string} [opts.file]
 */
function loadProjects(builtins, { section, file } = {}) {
    const fromJson = readProjectsJson(file);
    const out = {};
    for (const [name, entry] of Object.entries(fromJson)) {
        const base = {
            live: entry.live,
            // projects.json calls the workspace "sandbox" (its original name);
            // the deploy service calls the same directory "workspace".
            sandbox: entry.sandbox,
            workspace: entry.sandbox,
        };
        const extra = (section && entry[section]) || {};
        out[name] = { ...base, ...extra };
    }
    // Built-ins win, and a built-in-only project (one deliberately not in
    // projects.json) still appears.
    for (const [name, cfg] of Object.entries(builtins || {})) {
        out[name] = { ...(out[name] || {}), ...cfg };
    }
    return out;
}

/**
 * The `projects` check both health routes report, kept here because the two
 * services must answer it the same way and neither one owns the rule.
 *
 * The rule is that **onboarding** is what a health route answers for. The
 * merged map always contains the built-in projects (see loadProjects above),
 * so "no projects configured" is a state it can never be in -- a fresh
 * install looks like three built-in names with nothing on disk behind any of
 * them. Judging health on the merged map made every fresh install answer 503
 * on its first day, which is the fastest way to teach an operator that the
 * health check is noise. A name in projects.json, by contrast, is this
 * deployment's own claim that the checkout exists, so a missing directory
 * there is a real fault: no commit can be reviewed, merged or deployed in a
 * directory that is not there.
 *
 * @param {object} projects  the merged map (built-ins + projects.json)
 * @param {object} [opts]
 * @param {string} [opts.file]    projects.json to read (tests override it)
 * @param {(p: string) => boolean} [opts.exists]  fs.existsSync, injectable
 */
function healthProjectsCheck(projects, { file, exists = fs.existsSync } = {}) {
    const onboarded = Object.keys(readProjectsJson(file));
    const missing = onboarded.filter((n) => projects[n] && !exists(projects[n].live));
    const dormant = Object.keys(projects).filter((n) => !onboarded.includes(n));
    return {
        ok: missing.length === 0,
        count: onboarded.length,
        detail: missing.length ? `live checkout missing for: ${missing.join(', ')}`
            : onboarded.length === 0 ? 'none onboarded yet (fresh install)'
            : dormant.length ? `not onboarded here: ${dormant.join(', ')}` : null,
    };
}

module.exports = { loadProjects, readProjectsJson, healthProjectsCheck, PROJECTS_JSON };
