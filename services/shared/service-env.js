'use strict';
/**
 * Where the two Node services read their own secrets.
 *
 * REVIEW_CONTROL_SECRET used to live in the router's `.env` for one reason:
 * that file already existed and both services already read it for the
 * OpenRouter key. So the model proxy became a secrets bus -- a file whose
 * blast radius is "anything that can read the router's config" ended up
 * holding the secret that authorises merge and deploy. Those are different
 * trust domains and they now have different files.
 *
 * Read order, first hit wins:
 *   1. process.env                 -- an operator or pm2 passing it explicitly
 *   2. services/shared/.env        -- the home, written 0600 by install.sh
 *   3. services/model-router/.env  -- LEGACY. The router was renamed from
 *      llm-router; this is where an upgrade that did not re-run the installer
 *      still has the secret. Logged once so it gets moved.
 *   4. services/llm-router/.env    -- ANCIENT. Only a tree that never took
 *      the rename. Kept so that upgrade still boots.
 *
 * Deliberately a hand-rolled reader, not dotenv: these services have one
 * dependency each on purpose, the format in question is `KEY=value` lines,
 * and process.env must win over a file rather than the other way round.
 */

const fs = require('fs');
const path = require('path');

const SHARED_ENV = 'services/shared/.env';
const LEGACY_ENV = 'services/model-router/.env';
const ANCIENT_ENV = 'services/llm-router/.env';

const warned = new Set();

function readFrom(file, name) {
    try {
        const m = fs.readFileSync(file, 'utf8').match(new RegExp(`^${name}=(.+)$`, 'm'));
        return m ? m[1].trim() : null;
    } catch {
        return null;
    }
}

/**
 * One secret, by name. `agentHome` is the installation root.
 * Returns the value or null; callers fail closed on null.
 */
function readServiceSecret(name, agentHome) {
    if (process.env[name]) return process.env[name].trim();

    // <NAME>_FILE, the convention Docker and systemd both use for handing a
    // secret to a process without putting it in the environment, where `ps`
    // and a crash dump can read it. The bundle needs it for a different
    // reason: the agent container generates this secret on first run and the
    // two review containers have to end up with the same value, so it is
    // written to a shared volume rather than duplicated in compose.
    const fromFile = process.env[`${name}_FILE`];
    if (fromFile) {
        try {
            const v = fs.readFileSync(fromFile, 'utf8').trim();
            if (v) return v;
        } catch {
            // Fall through: a named-but-unreadable file is one more place to
            // look that did not answer, not a reason to take the service down.
        }
    }

    const fromShared = readFrom(path.join(agentHome, SHARED_ENV), name);
    if (fromShared) return fromShared;

    const fromLegacy = readFrom(path.join(agentHome, LEGACY_ENV), name);
    if (fromLegacy) {
        if (!warned.has(name)) {
            warned.add(name);
            console.warn(
                `[service-env] ${name} was read from ${LEGACY_ENV}. Move it to ${SHARED_ENV} ` +
                `(mode 600): the model proxy's config should not carry service secrets.`,
            );
        }
        return fromLegacy;
    }

    const fromAncient = readFrom(path.join(agentHome, ANCIENT_ENV), name);
    if (fromAncient) {
        if (!warned.has(name)) {
            warned.add(name);
            console.warn(
                `[service-env] ${name} was read from ${ANCIENT_ENV}. Move it to ${SHARED_ENV} ` +
                `(mode 600): that path is the pre-rename leftover.`,
            );
        }
        return fromAncient;
    }
    return null;
}

module.exports = { readServiceSecret, SHARED_ENV, LEGACY_ENV, ANCIENT_ENV };
