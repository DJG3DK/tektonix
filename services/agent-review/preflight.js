'use strict';
/**
 * Deploy preflight: URLs a project's build steps depend on must answer
 * BEFORE any build step runs.
 *
 * Why: a build step is not always self-contained. storefront' storefront
 * prerender fetches the catalog from the live API; on 2026-09-09 that API
 * had silently stopped listening days earlier, the prerender failed with a
 * bare "fetch failed", and the gate treated it as a compile error for the
 * agent to fix. A preflight failure is reported as its own stage, so the
 * caller (verify_and_ship) escalates to a human instead of looping the
 * agent back onto code that was never wrong.
 *
 * Config shape, per project (see builtin-projects.local.js.example):
 *   preflight: [
 *     { url: 'http://127.0.0.1:3000/api/v1/health', why: 'prerender reads the catalog' },
 *     'http://127.0.0.1:8080/ready',   // bare string is fine too
 *   ]
 * Any non-2xx or unreachable URL fails the preflight. Checks run in order
 * and all of them are reported, not just the first.
 */

const DEFAULT_TIMEOUT_MS = 5000;

function describeCause(err) {
    // undici wraps the socket error as `cause` (ECONNREFUSED, ...); an abort
    // from AbortSignal.timeout is a DOMException whose numeric legacy `code`
    // (23) says nothing, so prefer its name.
    if (err?.cause?.code) return err.cause.code;
    if (err?.name && err.name !== 'Error') return err.name;
    return err?.code || err?.message || 'error';
}

async function runPreflight(checks, { fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    const failures = [];
    for (const check of checks || []) {
        const url = typeof check === 'string' ? check : check?.url;
        if (!url) continue;
        const why = (typeof check === 'object' && check.why) ? ` -- ${check.why}` : '';
        try {
            const res = await fetchImpl(url, { signal: AbortSignal.timeout(timeoutMs) });
            if (!res.ok) failures.push(`${url} answered HTTP ${res.status}${why}`);
        } catch (err) {
            failures.push(`${url} unreachable (${describeCause(err)})${why}`);
        }
    }
    return failures;
}

function formatPreflightError(failures) {
    return 'deploy preflight failed -- a live dependency the build needs is not answering, '
        + 'so no build step was run and nothing on disk was touched:\n'
        + failures.map(f => `  - ${f}`).join('\n');
}

module.exports = { runPreflight, formatPreflightError, DEFAULT_TIMEOUT_MS };
