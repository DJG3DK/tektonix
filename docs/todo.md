# Todo

Work that is agreed but not built. One entry per item: what breaks, what
already exists, and the shape of the fix. `docs/roadmap-packaging.md` holds the
packaging/Windows/CLI milestones; this is everything else.

---

## Keep a task moving when live main moves under it

**Status:** done, 2026-09-19.

The merge stays `--ff-only`, which is what guarantees the thing that merges is
the thing that was reviewed. The branch moves instead:

* `rebase_onto_base` (`agent/tools/git.py`) rebases the task branch onto the
  live tip, aborting cleanly on conflict so the branch is never left half
  rebased. It reports whether the branch's own patch survived the move
  unchanged.
* `verify_and_ship` rebases after committing, so the review measures the
  branch against the base it will actually merge into.
* The merge endpoint answers `diverged` instead of throwing when live has
  moved since the review, and the agent rebases and goes round once more
  rather than stopping.
* A conflict goes back to the agent as feedback, with the file list, the same
  shape as a review finding.

Covered by `tests/test_rebase_on_moved_base.py`, and simulated against the
real merge endpoint: a reviewed branch, a concurrent push, a `diverged`
refusal, a rebase, and a fast-forward that lands both changes.

---

## The review dashboard is unreachable in the bundle

**Status:** not started. Raised 2026-09-20.

### What breaks

The gate runs in compose and the agent drives it, but a person cannot. Ports
4100 and 4101 are unpublished, and the console's review calls go to
`/_review/…`, a path nginx serves on a host install and nothing serves here.
So *Check now*, the diff view and the manual merge button are host-install
only, while the thing they control runs in the bundle.

### Shape of the fix

Proxy `/_review/*` from the agent's own server to `REVIEW_SERVICE_HOST`,
gated on the same admin auth the rest of the API uses, injecting the control
secret the way nginx does. That keeps the ports unpublished — the console is
already the authenticated front door — and it works on a host install too,
which would make the nginx snippet in INSTALL.md §7 optional rather than
required.

### Open question

Whether to then drop the nginx location entirely, or leave it as the faster
path and have the proxy be the fallback.
