'use strict';
/**
 * Review/merge server for the coding agent's workspace.
 *
 * Purely narrow, safe git operations against the 3 live repos:
 *   - fetch/log/diff  → read-only, never touch the live working tree
 *   - merge            → --ff-only ONLY. Never rewrites/discards a commit;
 *                        refuses outright if live has diverged or has
 *                        uncommitted changes that would be clobbered — git's
 *                        own safety net, not a hand-rolled one.
 *   - restart          → explicit, separate action; merging code never
 *                        implicitly restarts the live process.
 *
 * Localhost-only (127.0.0.1) — reached exclusively via nginx's /_review/
 * proxy in front of this service, which is itself behind the same
 * auth_request login gate as everything else on that domain. No separate
 * auth layer needed here.
 */

const express = require('express');
const rateLimit = require('express-rate-limit');
const { execFile } = require('child_process');
const path = require('path');
// Installation root, derived from this file's location so the same source
// works from any checkout path (AGENT_HOME overrides). Declared here, with
// the requires, because consts below reference it -- defining it lower hit
// the temporal dead zone and crash-looped the service on boot.
const AGENT_HOME = process.env.AGENT_HOME || path.join(__dirname, '..', '..');

const fs = require('fs');

// `build` steps run (in order, relative to `live`) BEFORE the pm2 restarts
// below — a bare `pm2 restart` re-runs whatever's already compiled on disk,
// so skipping this for a project with a build step deploys stale code while
// looking like it succeeded. Checked each project directly rather than
// assume: a trading-bot project runs straight from source (no build step, `main: src/index.js`),
// a Next.js project is Next.js (`next build` then `next start` serves .next/),
// a monorepo project' API is NestJS (`nest build`) and admin/storefront are Vite
// static bundles nginx serves directly — not pm2 apps at all, so they need
// a build with no matching restart.
// Deployment-specific overrides, loaded from an OPTIONAL gitignored file
// so a public checkout ships no one's infrastructure. See
// builtin-projects.local.js.example. Anything defined there wins over
// projects.json (see services/shared/projects-config.js).
let BUILTIN_PROJECTS = {};
try {
    BUILTIN_PROJECTS = require('./builtin-projects.local');
} catch (err) {
    if (err.code !== 'MODULE_NOT_FOUND') throw err;
}

// See services/shared/projects-config.js: projects.json supplies onboarded
// projects (deploy section), these built-ins stay authoritative.
const { loadProjects, healthProjectsCheck } = require('../shared/projects-config');
const { runPreflight, formatPreflightError } = require('./preflight');

const PROJECTS = loadProjects(BUILTIN_PROJECTS, { section: 'deploy' });

// Mirrors config.yaml's model_list — the raw backend model strings the router's
// `return_raw_model_name` puts in response.model / routing_decision.routed_model
// (provider prefix like "openrouter/" is stripped by the router before this point).
// All 4 tiers are real destinations (2026-08-15) — session_affinity is off,
// so every message is classified fresh instead of inheriting turn 1's tier.
const ROUTING_LOG = path.join(AGENT_HOME, 'services/model-router/logs/routing.jsonl');
const ROUTER_MODELS = [
    { backend: 'amazon/nova-micro-v1',            label: 'Nova Micro',            tier: 'SIMPLE' },
    { backend: 'openai/gpt-4o-mini',              label: 'GPT-4o mini',           tier: 'SIMPLE' },
    { backend: 'amazon/nova-lite-v1',             label: 'Nova Lite',             tier: 'SIMPLE' },
    { backend: 'z-ai/glm-5.2',                    label: 'GLM-5.2',               tier: 'MEDIUM' },
    { backend: 'qwen/qwen3.7-max',                label: 'Qwen3.7 Max',           tier: 'MEDIUM' },
    { backend: 'anthropic/claude-haiku-4.5',      label: 'Claude Haiku 4.5',      tier: 'MEDIUM' },
    { backend: 'deepseek/deepseek-v4-pro',        label: 'DeepSeek V4 Pro',       tier: 'COMPLEX' },
    { backend: 'x-ai/grok-4.3',                   label: 'Grok 4.3',              tier: 'COMPLEX' },
    { backend: 'qwen/qwen3-coder-plus',           label: 'Qwen3-Coder-Plus',      tier: 'COMPLEX' },
    { backend: 'moonshotai/kimi-k3',              label: 'Kimi K3',               tier: 'REASONING' },
    { backend: 'google/gemini-3.1-pro-preview',   label: 'Gemini 3.1 Pro Preview', tier: 'REASONING' },
    { backend: 'openai/gpt-5.3-codex',            label: 'GPT-5.3 Codex',         tier: 'REASONING' },
];

function run(cmd, args, cwd) {
    return new Promise((resolve, reject) => {
        execFile(cmd, args, { cwd, maxBuffer: 20 * 1024 * 1024 }, (err, stdout, stderr) => {
            if (err) return reject(new Error((stderr || err.message || '').trim()));
            resolve(stdout);
        });
    });
}
const git = (cwd, args) => run('git', args, cwd);

function projectOr404(req, res) {
    const p = PROJECTS[req.params.name];
    if (!p) { res.status(404).json({ error: `unknown project "${req.params.name}"` }); return null; }
    return p;
}

// Shared by the merge gate below and the read-only /api/review/status
// endpoint further down — same file, same shape either way.
const REVIEW_STATE_PATH = path.join(AGENT_HOME, 'services/commit-reviewer/state.json');
async function readReviewState() {
    try {
        return JSON.parse(await fs.promises.readFile(REVIEW_STATE_PATH, 'utf8'));
    } catch { return {}; }
}

// Called right after a successful merge — the report describes a commit
// that's now live, so leaving it up would show a stale (possibly
// NEEDS_FIXES) verdict indefinitely for work that's already shipped. The
// review card just goes back to its idle "nothing pending" state until the
// next commit lands and gets reviewed.
async function clearReviewState(project) {
    const state = await readReviewState();
    if (!(project in state)) return;
    delete state[project];
    // audit M-11: atomic temp-file + rename, matching commit-reviewer's
    // saveState -- a reader (or a crash) never sees a partial state.json.
    const tmp = `${REVIEW_STATE_PATH}.tmp-${process.pid}-${Date.now()}`;
    await fs.promises.writeFile(tmp, JSON.stringify(state, null, 2));
    await fs.promises.rename(tmp, REVIEW_STATE_PATH);
}

const app = express();
app.use(express.json());
// Every route here reads or writes files or runs git; nothing on this
// service should be hammerable even from behind nginx's login gate
// (CodeQL js/missing-rate-limiting, 2026-09-10). Generous for a dashboard
// that polls a few endpoints every few seconds, tight against a loop.
app.use(rateLimit({ windowMs: 60 * 1000, limit: 600, standardHeaders: 'draft-7', legacyHeaders: false }));

app.get('/api/projects', (req, res) => {
    res.json(Object.keys(PROJECTS));
});

// Read-only: what's ready to merge, and can it fast-forward cleanly.
// Which ref in the sandbox holds the work under review. The agent now commits
// to a per-task branch (`agent/<task-id>`), so this is no longer simply the
// remote-tracking twin of live's own branch. The reviewer records the branch
// its verdict was produced against; preferring that keeps "what was reviewed"
// and "what gets merged/displayed" the same ref. The `agent/<liveBranch>`
// fallback covers a sandbox that hasn't run a task since per-task branches
// landed, and force-merges with no review state at all.
async function agentRefFor(p, name, liveBranch) {
    // Prefer the branch the reviewer actually produced its verdict against, so
    // "what was reviewed" and "what gets merged" are the same ref -- but only if
    // that ref still exists. Review state outlives the branch it names: a verdict
    // recorded against the old mirror branch `agent/main` survived the move to
    // worktrees, and trusting it unconditionally made this endpoint 500 on a
    // ref that no longer resolves.
    try {
        const st = (await readReviewState())[name];
        if (st?.branch) {
            try {
                await git(p.live, ['rev-parse', '--verify', `${st.branch}^{commit}`]);
                return st.branch;
            } catch { /* stale branch -- fall through to discovery */ }
        }
    } catch { /* fall through */ }
    // Otherwise the newest agent task branch. These are plain local refs now --
    // the workspace is a worktree of this repo, not a clone behind a remote.
    try {
        const out = (await git(p.live, [
            'for-each-ref', '--sort=-committerdate', '--format=%(refname:short)', 'refs/heads/agent',
        ])).trim();
        const first = out.split('\n').map((r) => r.trim()).filter(Boolean)[0];
        if (first) return first;
    } catch { /* fall through */ }
    // No agent work exists. Returning `agent/<liveBranch>` here used to be
    // correct because the clone's main was mirrored under that name; with the
    // clone gone that ref does not exist, and asking git for `main..agent/main`
    // made the status endpoint 500 for every project that simply had no task
    // in flight. null means "nothing to review or merge", which callers handle.
    return null;
}

app.get('/api/projects/:name/status', async (req, res) => {
    const p = projectOr404(req, res); if (!p) return;
    try {
        const branch = (await git(p.live, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
        const agentRef = await agentRefFor(p, req.params.name, branch);
        if (!agentRef) {
            const dirty0 = (await git(p.live, ['status', '--short'])).trim().split('\n').filter(Boolean);
            return res.json({ branch, agentRef: null, commits: [], dirtyFiles: dirty0, canFastForward: null });
        }
        const log = await git(p.live, ['log', `${branch}..${agentRef}`, '--format=%H|%an|%ad|%s', '--date=iso']);
        const commits = log.trim().split('\n').filter(Boolean).map(line => {
            const [hash, author, date, ...rest] = line.split('|');
            return { hash, author, date, subject: rest.join('|') };
        });
        const dirty = (await git(p.live, ['status', '--short'])).trim().split('\n').filter(Boolean);
        let canFastForward = null;
        if (commits.length) {
            canFastForward = await git(p.live, ['merge-base', '--is-ancestor', branch, agentRef])
                .then(() => true).catch(() => false);
        }
        res.json({ branch, agentRef, commits, dirtyFiles: dirty, canFastForward });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// Read-only: full diff of what the agent's workspace (a worktree of live) has that live doesn't yet.
app.get('/api/projects/:name/diff', async (req, res) => {
    const p = projectOr404(req, res); if (!p) return;
    try {
        const branch = (await git(p.live, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
        const agentRef = await agentRefFor(p, req.params.name, branch);
        if (!agentRef) return res.type('text/plain').send('');
        const diff = await git(p.live, ['diff', `${branch}...${agentRef}`]);
        res.type('text/plain').send(diff);
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// Can this service do its job right now, and if not, which dependency is
// missing? No model call, no git write, nothing that costs anything -- safe
// to poll from a monitoring box or a second person with curl, neither of
// which has a session. A configured secret reports `true`, never its value.
// 503 when anything is wrong, so a probe that only reads the status code is
// still correct.
app.get('/health', (req, res) => {
    const checks = {
        // Unset means every merge this service is asked for is refused, at the
        // end of a task that has already been paid for.
        review_secret: {
            ok: Boolean(REVIEW_CONTROL_SECRET),
            detail: REVIEW_CONTROL_SECRET ? null : 'REVIEW_CONTROL_SECRET unset: mutating endpoints are disabled',
        },
        // Onboarding, not the merged map, is what this answers for: see
        // healthProjectsCheck in services/shared/projects-config.js, which
        // both health routes share so they cannot drift apart.
        projects: healthProjectsCheck(PROJECTS),
        // The reviewer's verdict file is what gates every merge; unreadable
        // means the gate cannot answer and merges fail closed. Absent before
        // commit-reviewer has ever run, which is the same fresh-install case
        // as above: say so rather than failing.
        review_state: (() => {
            if (!fs.existsSync(REVIEW_STATE_PATH)) {
                return { ok: true, detail: 'not written yet (commit-reviewer has not run)' };
            }
            try {
                fs.accessSync(REVIEW_STATE_PATH, fs.constants.R_OK);
                return { ok: true };
            } catch {
                return { ok: false, detail: `cannot read ${REVIEW_STATE_PATH}` };
            }
        })(),
    };
    const ok = Object.values(checks).every((c) => c.ok);
    res.status(ok ? 200 : 503).json({ ok, service: 'agent-review', checks });
});

// The only WRITE path into a live repo. --ff-only means git itself refuses
// if live has diverged, or if any uncommitted local file would be clobbered
// — no custom conflict handling to get wrong, git's own guarantee.
//
// Gated on the commit-reviewer service's verdict for the exact commit being
// merged — this was originally launch-and-forget (the review card was purely
// informational, nothing stopped "Merge to live" from firing before the
// review even started). req.body.force=true bypasses the gate for a
// deliberate human override; the gate itself never auto-merges, it only ever
// blocks — same "manual action, always" posture as the rest of this file.
app.post('/api/projects/:name/merge', requireControlSecret, async (req, res) => {
    const p = projectOr404(req, res); if (!p) return;
    try {
        const branch = (await git(p.live, ['rev-parse', '--abbrev-ref', 'HEAD'])).trim();
        const agentRef = await agentRefFor(p, req.params.name, branch);
        if (!agentRef) {
            return res.status(409).json({ ok: false, reason: 'nothing_to_merge',
                error: 'No agent task branch exists for this project.' });
        }
        const tipSha = (await git(p.live, ['rev-parse', agentRef])).trim();

        if (!req.body?.force) {
            const review = (await readReviewState())[req.params.name];
            if (!review) {
                return res.status(409).json({ ok: false, reason: 'not_reviewed',
                    error: 'Not yet reviewed by the automated review service (polls every 2 minutes). Wait for it, or merge anyway.' });
            }
            if (review.inProgress?.sha === tipSha) {
                return res.status(409).json({ ok: false, reason: 'in_progress',
                    error: 'The review service is reviewing this exact commit now. Wait for it, or merge anyway.' });
            }
            if (review.lastReviewedSha !== tipSha) {
                return res.status(409).json({ ok: false, reason: 'stale',
                    error: 'Newer commit(s) since the last review — re-review pending. Wait for it, or merge anyway.' });
            }
            if (review.verdict !== 'READY') {
                return res.status(409).json({ ok: false, reason: 'needs_fixes',
                    error: review.summary || 'Automated review found blocking issues.', findings: review.findings || [] });
            }
        }

        const output = await git(p.live, ['merge', '--ff-only', agentRef]);
        await clearReviewState(req.params.name);

        // Push to the real GitHub remote as part of the merge, not as a
        // separate manual step afterwards. Before this, a merge only ever
        // moved live's own local branch — `origin` stayed silently behind,
        // with nothing anywhere reporting the drift (found 2026-08-23: two
        // separate agent commits had merged and fully deployed to the live
        // site while GitHub was still one and two commits stale).
        //
        // Deliberately best-effort: a push failure (network, an SSH key
        // problem, a remote that rejects) must NOT turn an otherwise
        // successful merge+deploy into a 409, because the merge has already
        // happened and is not being rolled back. The result is reported in
        // the response instead, so a failure is visible rather than assumed.
        let push = { ok: true, skipped: true };
        try {
            const remotes = (await git(p.live, ['remote'])).split('\n').map((r) => r.trim());
            if (remotes.includes('origin')) {
                push = { ok: true, output: await git(p.live, ['push', 'origin', branch]) };
            }
        } catch (e) {
            push = { ok: false, error: e.message };
        }

        res.json({ ok: true, output, push });
    } catch (e) { res.status(409).json({ ok: false, error: e.message }); }
});

// Explicit, separate from merge — deploying (rebuilding + restarting the
// live process to actually pick up merged code) is a deliberate second
// action, never implicit. Runs each project's build steps (see PROJECTS)
// BEFORE any pm2 restart, in declared order — for a monorepo project that's the API
// compiling before it restarts, then the two static frontends rebuilding
// with no restart of their own (nginx just serves whatever's newest on
// disk). A build failure aborts before touching any running process, so a
// bad build can't take down what's currently live.
app.post('/api/projects/:name/restart', requireControlSecret, async (req, res) => {
    const p = projectOr404(req, res); if (!p) return;
    const built = [];
    // Build steps that reach out to a live dependency (a prerender reading
    // the catalog from the running API) fail with an unhelpful error and
    // look like a code problem when that dependency is down. Check the
    // project's declared preflight URLs first and report that as its own
    // stage -- see preflight.js.
    const preflightFailures = await runPreflight(p.preflight);
    if (preflightFailures.length) {
        return res.status(500).json({ ok: false, error: formatPreflightError(preflightFailures), stage: 'preflight', built });
    }
    try {
        for (const step of p.build) {
            const dir = path.join(p.live, step.dir);
            await run(step.cmd, step.args, dir);
            built.push(step.dir);
        }
    } catch (e) {
        return res.status(500).json({ ok: false, error: e.message, stage: 'build', built });
    }
    try {
        for (const appName of p.pm2Apps) {
            await run('pm2', ['restart', appName], '/');
        }
        res.json({ ok: true, built, restarted: p.pm2Apps });
    } catch (e) { res.status(500).json({ ok: false, error: e.message, stage: 'restart', built }); }
});

// Read-only, deliberately cheap: just the single most recent routing
// decision, for the floating model badge on the review dashboard
// (polled every few seconds — this endpoint has to stay light). Since
// the model router's consumers are few, "most recent entry" is a
// good proxy for "what's answering the user's active conversation right now".
//
// Every real turn also produces a routing.jsonl line for the complexity
// classifier's OWN internal call (always deepseek-v4-flash, tier: null —
// that call isn't itself tier-routed, it's what DECIDES the tier) plus,
// separately, background context-condenser calls. Naively taking
// "the last line" flickers the badge between that overhead and the actual
// answering model on every turn (2026-08-16 — user watching a complex edit
// saw Pro for a few seconds then flash, and it was this, not misrouting).
// Only entries with `tier` set are real, classifier-decided completions.
app.get('/api/router/current', async (req, res) => {
    try {
        const raw = await fs.promises.readFile(ROUTING_LOG, 'utf8').catch(() => '');
        const lines = raw.trim().split('\n').filter(Boolean);
        for (let i = lines.length - 1; i >= 0; i--) {
            let e;
            try { e = JSON.parse(lines[i]); } catch { continue; }
            if (e.error || !e.routed_model || !e.tier) continue;
            const known = ROUTER_MODELS.find(m => m.backend === e.routed_model);
            return res.json({
                model: e.routed_model,
                label: known ? known.label : e.routed_model,
                tier: known ? known.tier : e.tier,
                ts: e.ts,
            });
        }
        res.json({ model: null });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// Read-only: model usage/routing visibility for the LLM router. Reads
// the router's own routing.jsonl (the router logs one line
// per completed request there) — an off-the-shelf proxy's spend API would need a
// Postgres DB we don't have set up, so this is the lightweight substitute.
//
// Every real turn logs TWO+ lines: the complexity classifier's own internal
// call (tier: null — that call decides the tier, it isn't itself routed by
// one) plus, separately, background condenser calls — both
// always land on deepseek-v4-flash. Blending those into the per-model
// breakdown made flash look dominant even on sessions where the real work
// was mostly complex (2026-08-16 finding: badge showed ~96% flash on a
// genuinely complex edit; the actual routed completions split ~49/42
// flash/pro). `models`/`recent`/`totals` below cover real completions
// (tier set) only; overhead is reported separately in `overhead`.
app.get('/api/router/stats', async (req, res) => {
    try {
        const raw = await fs.promises.readFile(ROUTING_LOG, 'utf8').catch(() => '');
        const allEntries = raw.trim().split('\n').filter(Boolean).map(line => {
            try { return JSON.parse(line); } catch { return null; }
        }).filter(Boolean);

        const entries = allEntries.filter(e => e.error || e.tier);
        const overheadEntries = allEntries.filter(e => !e.error && !e.tier);

        const byModel = {};
        for (const m of ROUTER_MODELS) {
            byModel[m.backend] = { ...m, requests: 0, errors: 0, cost: 0, promptTokens: 0, completionTokens: 0 };
        }
        for (const e of entries) {
            const key = e.routed_model || e.requested_model;
            if (!key) continue;
            if (!byModel[key]) byModel[key] = { backend: key, label: key, tier: null, requests: 0, errors: 0, cost: 0, promptTokens: 0, completionTokens: 0 };
            const b = byModel[key];
            if (e.error) { b.errors++; continue; }
            b.requests++;
            b.cost += e.cost || 0;
            b.promptTokens += e.prompt_tokens || 0;
            b.completionTokens += e.completion_tokens || 0;
        }

        const recent = entries.slice(-50).reverse();
        const totals = {
            requests: entries.filter(e => !e.error).length,
            errors: entries.filter(e => e.error).length,
            cost: entries.reduce((s, e) => s + (e.cost || 0), 0),
            since: allEntries.length ? allEntries[0].ts : null,
        };
        const overhead = {
            requests: overheadEntries.length,
            cost: overheadEntries.reduce((s, e) => s + (e.cost || 0), 0),
            note: 'Complexity-classifier + context-condenser calls — always deepseek-v4-flash, not a real routing decision. Excluded from `models`/`recent`/`totals` above.',
        };
        res.json({ models: Object.values(byModel), recent, totals, overhead });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// Read-only: OpenRouter account balance for the Router tab. Reads the key
// straight out of the model router's own .env (no new dependency for one value) —
// same key the router itself authenticates to OpenRouter with. Cached briefly
// since this hits OpenRouter's real API, not something to poll on every tick.
let _balanceCache = null; // { data, ts }
const BALANCE_CACHE_MS = 60_000;

// audit C-4: the mutating endpoints (merge, restart, review-check) were
// reachable by any local process -- an agent npm script doing `curl
// 127.0.0.1:4100/.../merge -d '{"force":true}'`, or a cross-site form POST
// riding the operator's nginx cookie -- because this service delegated auth
// entirely to an nginx proxy that the direct-to-port paths skip. A shared
// secret, required regardless of the proxy, closes both. nginx injects the
// header for the authenticated dashboard path; the agent's own review_gate
// client sends it; a rewritten npm script cannot forge it because the C-2 env
// allow-list keeps REVIEW_CONTROL_SECRET out of the check process's env.
// services/shared/.env, with the router's own .env as a legacy fallback for
// deployments that predate the split -- see services/shared/service-env.js.
const { readServiceSecret } = require('../shared/service-env');
const REVIEW_CONTROL_SECRET = readServiceSecret('REVIEW_CONTROL_SECRET', AGENT_HOME);

function requireControlSecret(req, res, next) {
    // Fail CLOSED: if the secret is unset the mutating surface is disabled, not
    // wide open -- a missing secret must never mean "no check".
    if (!REVIEW_CONTROL_SECRET) {
        return res.status(503).json({ ok: false, error: 'REVIEW_CONTROL_SECRET not configured; mutating endpoints disabled' });
    }
    // CSRF defense-in-depth: a browser sets Sec-Fetch-Site on cross-site
    // requests. Reject cross-site outright even before the secret check, so a
    // form POST riding the operator cookie can't reach these at all.
    const sfs = req.get('sec-fetch-site');
    if (sfs && sfs !== 'same-origin' && sfs !== 'none') {
        return res.status(403).json({ ok: false, error: `cross-site request rejected (Sec-Fetch-Site: ${sfs})` });
    }
    const provided = req.get('x-review-secret') || '';
    // Constant-time compare -- both are hex strings of known length.
    const a = Buffer.from(provided);
    const b = Buffer.from(REVIEW_CONTROL_SECRET);
    if (a.length !== b.length || !require('crypto').timingSafeEqual(a, b)) {
        return res.status(401).json({ ok: false, error: 'invalid or missing X-Review-Secret' });
    }
    next();
}

function readOpenRouterKey() {
    try {
        const env = fs.readFileSync(path.join(AGENT_HOME, 'services/model-router/.env'), 'utf8');
        const m = env.match(/^OPENROUTER_API_KEY=(.+)$/m);
        return m ? m[1].trim() : null;
    } catch { return null; }
}

app.get('/api/router/balance', async (req, res) => {
    if (_balanceCache && (Date.now() - _balanceCache.ts) < BALANCE_CACHE_MS) {
        return res.json(_balanceCache.data);
    }
    const key = readOpenRouterKey();
    if (!key) return res.status(500).json({ error: 'OPENROUTER_API_KEY not found' });
    try {
        const r = await fetch('https://openrouter.ai/api/v1/credits', {
            headers: { Authorization: `Bearer ${key}` },
        });
        const body = await r.json();
        if (!r.ok) return res.status(502).json({ error: body?.error?.message || 'OpenRouter API error' });
        const totalCredits = body.data.total_credits;
        const totalUsage = body.data.total_usage;
        const data = { totalCredits, totalUsage, remaining: totalCredits - totalUsage };
        _balanceCache = { data, ts: Date.now() };
        res.json(data);
    } catch (e) { res.status(502).json({ error: e.message }); }
});

// Read-only: the commit-reviewer service's last verdict per project. That
// service (separate pm2 process, /home/3d-agent/services/commit-reviewer/reviewer.js) polls
// each sandbox for new commits, runs real lint/test/build checks plus a
// Claude Sonnet 5 review in an isolated worktree off LIVE (real secrets,
// real env), and writes its findings to state.json — this just surfaces
// that file, it doesn't run anything itself.
app.get('/api/review/status', async (req, res) => {
    res.json(await readReviewState());
});

// Proxies to the commit-reviewer process's own localhost-only control port —
// triggers an immediate check instead of waiting out the rest of its 2-min
// poll (e.g. right after a fresh commit lands and the review card still says
// "not yet reviewed"). Fire-and-forget on the reviewer's side; this just
// forwards its {ok, started} response.
const REVIEW_CONTROL_URL = 'http://127.0.0.1:4101';
app.post('/api/review/check/:name', requireControlSecret, async (req, res) => {
    const p = projectOr404(req, res); if (!p) return;
    try {
        const r = await fetch(`${REVIEW_CONTROL_URL}/check/${encodeURIComponent(req.params.name)}`, { method: 'POST' });
        const data = await r.json();
        res.status(r.status).json(data);
    } catch (e) {
        res.status(502).json({ ok: false, error: `commit-reviewer unreachable: ${e.message}` });
    }
});

app.use(express.static(path.join(__dirname, 'public')));

const PORT = 4100;
app.listen(PORT, '127.0.0.1', () => console.log(`[Review] listening on 127.0.0.1:${PORT}`));
