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

**Status:** done, 2026-09-20.

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

### What was done

`/_review/{path}` on the agent's own server forwards to the review service,
admitting only an admin over the session the console already required. The
secret is SET there, never forwarded: a caller who sends their own
`X-Review-Secret` cannot influence what the review service sees, because this
endpoint's authority comes from the session rather than from a header.

The nginx location stays, as an optimisation rather than a prerequisite. It
keeps the traffic out of the Python process where somebody already has nginx;
INSTALL.md §7 says so now.

Covered by `tests/test_review_proxy.py`, and verified against the running
service: a read route returns real data, and a mutating one returns the gate's
own `not_reviewed` rather than `invalid or missing X-Review-Secret`, which is
what proves the injection reached it.

---

## Split `agent/server.py` along the seams that already exist

**Status:** not built. Do not flatten it in one pass.

### What breaks

`agent/server.py` is past 5,600 lines and still growing. The review proxy
landed at the bottom of the same file. `tests/test_route_inventory.py` still
says "3,700 lines with 60+ routes" — the inventory grew; the comment is the
record of when this was last treated as urgent. A route can lose
`check_repo_access` inside the handler body and the inventory stays green,
because the snapshot pins the FastAPI dependency, not the repo check.

### What already exists

- `tests/test_route_inventory.py` is the safety net. The playbooks README
  already forbids a big-bang split: keep that test green through every step,
  and do not lose the incident comments.
- Seams named there: auth, tasks, planning, github, settings, uploads. The
  review proxy (`/_review/{path}`) is a seventh, already isolated at the
  bottom of the file.

### Shape of the fix

One router module per seam, imported and included from `server.py`. First
commit extracts one seam and updates the inventory comment. Repo-scoped
checks stay next to the handler that needs them until a later commit can
make `check_repo_access` a dependency rather than a manual call. Do not
extract `provisioning.py` or `history_index.py` in the same change.

---

## Virtualize the task log

**Status:** not built.

### What breaks

`useTaskStream.ts` caps the in-memory log at 3,000 entries. `ChatMessage`
is memoized (audit H-16) so an append does not re-render every row, but
the DOM still mounts every row. A multi-hour task hits the cap and the tab
gets slow before it does.

### What already exists

`ChatMessage` is already `React.memo`. `useTaskStream` already drops from
the front of the array. There is no windowing library in `frontend/`.

### Shape of the fix

A viewport-sized window over the existing array — either a small existing
pattern (absolute-positioned spacers + a slice) or one dependency if the
bundle cost is measured. Keep the 3,000 cap. A test that renders a long
log must not mount 3,000 nodes.

---

## A CI job that actually starts the compose stack

**Status:** partial. `docker compose config` is now in the shell job so a
broken interpolate fails offline. The stack itself is still never started.

### What breaks

Bundle regressions (`70e3cd9`, `29f7305`) were found by hand. Offline CI
cannot see a missing healthcheck, a secret file that is not written before
the reviewers start, or an image that does not contain `curl`.

### What already exists

Image HEALTHCHECKs on agent and router. Compose now waits on them (see
`docker-compose.yml`). The shell job validates the file. The manual
"Stack checks" job is the precedent for an expensive, opt-in run.

### Shape of the fix

A `workflow_dispatch` (or nightly) job: `docker compose up -d --wait`,
curl `/api/health` until 200, `docker compose down`. Needs
`OPENROUTER_API_KEY` only if the readiness probe on the router requires a
real upstream — liveness does not. Do not put this on every push; the
images are the cost.

---

## The reviewer runs agent-authored checks on the host

**Status:** not built. This is a decision, then a change.

### What breaks

`services/commit-reviewer` runs the project's typecheck/lint/test suite
against a review worktree, on the host (or, in the bundle, in the reviewer
container with the projects mount). Env is sealed. That is not isolation.
A branch that changes `package.json` to add a `postinstall` is the
residual path the reviewer already refuses to borrow for; a test file that
opens a socket is not.

### What already exists

The agent sandbox (`docker/agent-sandbox`, `--cap-drop ALL`, mount
allow-list). The reviewer already installs with `--ignore-scripts` when
the branch changed a manifest. Bundler is already refuse-rather-than-run.

### Shape of the fix

Pick one and write it down in `SECURITY.md` before coding:

1. Run the check suite in the same sandbox image the agent uses, with
   network only when the project's own checks need it, and label that
   case in the verdict.
2. Keep host execution, and treat network-touching tests as an explicit
   project setting (onboarding already disables some of those).

Do not silently move checks into `--network none` — that is how a suite
that needs a registry becomes a false `NEEDS_FIXES`.

---

## The sandbox is filesystem isolation, not network isolation

**Status:** not built. Same decision as the reviewer item.

### What already exists

`agent/tools/url_guard.py` on the host browse/preview path (scheme,
resolve-then-validate, re-check redirects). In-sandbox `curl` is not
that guard. `SECURITY.md` already calls arbitrary shell the product.

### Shape of the fix

Either default the sandbox to no network and punch a hole for
`run_checks` / preview, or document that egress is intended and keep the
guard on the host-side URL tools only. The wrong fix is a prompt
instruction.
