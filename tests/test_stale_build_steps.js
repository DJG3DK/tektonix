// Which build steps a merge actually made stale.
//
// Running every build on every merge is wasted minutes on most of them, and on
// a project with several packages it is most of the wall clock after a
// one-line change. The rule has to be conservative in one direction only:
// never skip a build that was needed.
const test = require('node:test');
const assert = require('node:assert/strict');

// The predicate as server.js applies it.
function makeStepIsStale(changed) {
    const MANIFESTS = /(^|\/)(package\.json|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|requirements\.txt|pyproject\.toml|go\.mod|Cargo\.toml|composer\.json|Gemfile)$/;
    const manifestChanged = changed !== null && changed.some((f) => MANIFESTS.test(f));
    return function stepIsStale(step) {
        if (changed === null || manifestChanged) return true;
        const dir = (step.dir || '.').replace(/^\.\//, '').replace(/\/$/, '');
        if (!dir || dir === '.') return true;
        return changed.some((f) => f === dir || f.startsWith(`${dir}/`));
    };
}

test('a build is skipped when the merge did not touch its directory', () => {
    const stale = makeStepIsStale(['agent/server.py', 'docs/readme.md']);
    assert.equal(stale({ dir: 'frontend' }), false, 'a backend-only change');
});

test('a build runs when the merge touched its directory', () => {
    const stale = makeStepIsStale(['frontend/src/App.tsx']);
    assert.equal(stale({ dir: 'frontend' }), true);
});

test('a step rooted at the repo covers everything and always runs', () => {
    const stale = makeStepIsStale(['docs/readme.md']);
    for (const dir of ['.', './', '', undefined]) {
        assert.equal(stale({ dir }), true, `dir=${JSON.stringify(dir)}`);
    }
});

test('a changed dependency manifest runs every build', () => {
    // This is how a change OUTSIDE a directory reaches the build inside it:
    // a shared package moves, and the only evidence is a lockfile.
    for (const f of ['package-lock.json', 'frontend/package.json', 'pnpm-lock.yaml',
                     'requirements.txt', 'go.mod', 'Cargo.toml']) {
        const stale = makeStepIsStale([f]);
        assert.equal(stale({ dir: 'unrelated' }), true, f);
    }
});

test('an unknown diff runs everything rather than guessing', () => {
    // The merge response had no usable commit, or the diff could not be read.
    // Skipping a build on no information is the one unsafe direction.
    const stale = makeStepIsStale(null);
    assert.equal(stale({ dir: 'frontend' }), true);
});

test('a directory is not matched by a prefix of another name', () => {
    // `frontend-old/x` must not count as a change to `frontend`.
    const stale = makeStepIsStale(['frontend-old/src/App.tsx']);
    assert.equal(stale({ dir: 'frontend' }), false);
});

test('a nested build directory matches its own files only', () => {
    const stale = makeStepIsStale(['apps/web/src/index.ts']);
    assert.equal(stale({ dir: 'apps/web' }), true);
    assert.equal(stale({ dir: 'apps/api' }), false);
});

test('a trailing slash in the configured dir is tolerated', () => {
    const stale = makeStepIsStale(['frontend/src/App.tsx']);
    assert.equal(stale({ dir: 'frontend/' }), true);
});
