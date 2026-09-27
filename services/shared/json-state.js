'use strict';
/**
 * state.json has two writers: commit-reviewer records verdicts, agent-review
 * drops a branch's record when it merges. Each used to read the whole file,
 * change its part and write it all back, so a write from one landing between
 * the other's read and write was silently undone -- a merged branch's verdict
 * reappearing, or a fresh verdict vanishing.
 *
 * Every read-modify-write now happens under `<file>.lock`, created O_EXCL.
 * That works across processes on a host install and across containers sharing
 * the REVIEW_STATE_DIR volume. The critical section is one small JSON read and
 * write, so a lock older than STALE_MS belongs to a process that died holding
 * it and is broken.
 */

const fs = require('fs');
const crypto = require('crypto');

const STALE_MS = 10_000;
const WAIT_MS = 15_000;
const POLL_MS = 20;

const sleeper = new Int32Array(new SharedArrayBuffer(4));
function sleepSync(ms) {
    Atomics.wait(sleeper, 0, 0, ms);
}

function readJson(file) {
    try {
        return JSON.parse(fs.readFileSync(file, 'utf8'));
    } catch {
        return {};
    }
}

function writeJsonAtomic(file, value) {
    const tmp = `${file}.tmp-${process.pid}-${crypto.randomBytes(4).toString('hex')}`;
    fs.writeFileSync(tmp, JSON.stringify(value, null, 2));
    fs.renameSync(tmp, file);
}

function tryAcquire(lockPath) {
    try {
        const fd = fs.openSync(lockPath, 'wx');
        try { fs.writeSync(fd, String(process.pid)); } finally { fs.closeSync(fd); }
        return true;
    } catch (e) {
        if (e.code !== 'EEXIST') throw e;
    }
    try {
        if (Date.now() - fs.statSync(lockPath).mtimeMs > STALE_MS) fs.unlinkSync(lockPath);
    } catch (e) {
        if (e.code !== 'ENOENT') throw e;
    }
    return false;
}

function release(lockPath) {
    try { fs.unlinkSync(lockPath); } catch { /* already broken as stale */ }
}

function timedOut(file) {
    return new Error(`timed out after ${WAIT_MS}ms waiting for the lock on ${file}`);
}

function applyUnderLock(file, mutate) {
    const state = readJson(file);
    if (mutate(state) !== false) writeJsonAtomic(file, state);
    return state;
}

/**
 * Read `file`, let `mutate` change the object in place, write it back -- all
 * under the lock. `mutate` returning `false` skips the write.
 */
function updateJsonSync(file, mutate) {
    const lockPath = `${file}.lock`;
    const deadline = Date.now() + WAIT_MS;
    while (!tryAcquire(lockPath)) {
        if (Date.now() > deadline) throw timedOut(file);
        sleepSync(POLL_MS);
    }
    try {
        return applyUnderLock(file, mutate);
    } finally {
        release(lockPath);
    }
}

/** updateJsonSync for a server: waits for the lock without blocking the loop. */
async function updateJson(file, mutate) {
    const lockPath = `${file}.lock`;
    const deadline = Date.now() + WAIT_MS;
    while (!tryAcquire(lockPath)) {
        if (Date.now() > deadline) throw timedOut(file);
        await new Promise((r) => setTimeout(r, POLL_MS));
    }
    try {
        return applyUnderLock(file, mutate);
    } finally {
        release(lockPath);
    }
}

module.exports = { readJson, updateJsonSync, updateJson, STALE_MS };
