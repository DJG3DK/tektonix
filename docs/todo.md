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

## Six accuracy improvements (operator's list, 2026-09-21)

Stored for later. Annotated against what this codebase already has, because
two of the six are built, two are half-built, and only two are new work — and
building what exists again is the expensive kind of mistake.

### 1. AST code graphing (tree-sitter) — NOT BUILT, and the largest of the six

Splitting files by token count cuts through function boundaries, so a chunk
arrives holding half a signature. Parse to an AST instead and extract the
symbol map: classes, signatures, imports/exports, call hierarchy. An agent
editing one file is then handed the exact signatures of what depends on it
rather than whole files.

**What exists:** `agent/cartographer.py` builds a codebase map, but it is an
LLM writing prose about structure, refreshed when the inventory hash changes.
Useful for orientation, and not a symbol graph — it cannot answer "who calls
this" or "what is this function's signature" without reading the files.

**Shape:** `tree-sitter` plus the grammars for the languages actually in these
projects. The map becomes data rather than prose. The interesting question is
whether it replaces the cartographer skill or feeds it; the honest answer is
probably feeds — the prose map is good at "why is this laid out this way",
which an AST cannot tell anyone.

### 2. Line-exact patch tools — ALREADY BUILT

`edit(path, old_string, new_string)` in `agent/tools/agent_tools.py` is exactly
this: exact string replacement that must match once, never a full-file rewrite.
There is also an edit-repeat guard that refuses the same failed
(path, old_string, new_string) a second time, seeded across work-node passes so
it survives a loop-back.

**Remaining gap, small:** no unified-diff tool for a patch spanning several
hunks, where the agent currently makes several `edit` calls.

### 3. Inline typecheck before the gate — HALF BUILT, and the gap is worth closing

**What exists:** `run_checks`, and the review gate re-runs the same checks.
**What is missing is that it is the model's choice.** The prompt asks the agent
to call it; nothing makes it. So a syntax error or a missing import can survive
until the gate, which is a Docker run away, and comes back as a review failure
rather than a compiler line.

**Shape:** run the project's fastest diagnostic (`tsc --noEmit`, `ruff`, the
configured lint) automatically after a patch, feed the raw diagnostics straight
back, and cap it at one or two micro-loops so a genuinely broken build still
reaches a human. The per-project check config already exists in
`projects.json`; what is missing is a "cheap subset" marker on it and the node
that runs it.

### 4. Reflexion — HALF BUILT, and the built half is the wrong half

**What exists is loop DETECTION:** `no_diff_streak`, `last_failed_edit_signature`,
`incomplete_plan_streak`, `short_conclusion_streak` in `agent/outer_state.py`.
These notice a stuck agent and eventually escalate.

**What is missing is the reflection itself.** Nothing forces a critique before
the next attempt, so the counters catch the third identical fix rather than
preventing the second.

**Shape:** a reflection key in the state schema; on a failed verification the
agent writes why it failed and what it will try differently, and that critique
is in the prompt for the retry. Note it composes with the new episode/history
search: a critique is exactly the kind of durable "what went wrong" the history
index is now able to retrieve across tasks.

### 5. Hybrid retrieval with re-ranking — SPARSE BUILT, DENSE IN FLIGHT, RE-RANKER NOT

**Built (2026-09-21):** `agent/history_index.py`, Postgres full-text with a
weighted tsvector, over episodes, tasks and build transcripts.
**Built but NOT SWITCHED ON:** the dense/vector leg (`agent/episode_vectors.py`),
registered through `agent/episode_recall.py`'s leg registry.
**Not built:** the cross-encoder re-ranker over the fused candidates.

**The gate this was supposed to pass was never evaluated, and that is on the
record here rather than left to be inferred.** The build order made the dense
leg contingent on four weeks of retrieval telemetry showing a top-5 miss rate
above 20%. `logs/retrieval_events.jsonl` holds 15 `query` events and 5 `use`
events, all inside one 54-minute window on 2026-09-21 — no proportion can be
computed from that, so the decision the gate existed to make was never taken.
The code landed anyway. What did not land is the decision to run it:
`EMBEDDINGS_ENABLED` is unset, which is the default and which is what the gate
actually governs. Leave it unset until the telemetry has four weeks behind it.

**Switching it on takes three steps, in this order**, because the running
router predates `/v1/embeddings` and answers 404 to it:

1. restart `model-router` (never with a build task mid-call),
2. set `EMBEDDINGS_ENABLED=1`,
3. restart `tektonix`.

Until step 1, `embeddings.available()` correctly reports false and the store
opens with no index — the capability probe working as designed, not a fault.

**What a first measurement of the leg actually found (2026-09-21, 146 embedded
episodes, 11 queries, `openai/text-embedding-3-small`):** 2 clear wins on
paraphrase, 1 marginal, 3 neutral and 5 worse. Every one of the 5 came from the
same cause — a nearest-neighbour search has no concept of "no match", so the
leg filled all 8 slots of every page whether or not it had found anything, and
fusion counts positions. `episode_vectors.MIN_SIMILARITY` (0.35, measured) now
drops those, and the re-run shows both nonsense queries returning empty and the
paraphrase wins untouched. On an identifier-heavy corpus this leg is a
minority contributor by design; it is worth having for the query full text
cannot answer, and it is not worth turning on before the telemetry says
retrieval is missing.

Worth being precise about the claim in the list: the reason exact identifiers
rank correctly here is not a re-ranker, it is that sparse retrieval already
wins those and the fusion keeps its ordering. A re-ranker earns its place when
BOTH legs return plausible candidates and the ordering between them is wrong —
which is a question the retrieval telemetry
(`logs/retrieval_events.jsonl`) is being written to answer. Do not add a third
model to the path before that data says the ordering is the problem.

### 6. Dynamic context compression — ALREADY BUILT

Summarization middleware, with `summarization_trigger()` /
`summarization_keep()` in `agent/deep_agent.py`, the keep window clamped to 60%
of the trigger so summarization cannot fire immediately after summarizing.
Both are runtime-settable.

**Remaining gap:** it summarizes by token count, indiscriminately. The list's
sharper idea is to collapse *old tool output* while preserving active diffs and
the goal. That is a real improvement and smaller than it sounds, since the
middleware already exists and this is a change to what it keeps.

---

**If picking these up in order of value on this codebase:** 3 (cheap, closes a
real feedback gap), 4 (composes with history search), 6's refinement (existing
machinery), then 1 (large, and the only one that needs a new dependency). 5's
re-ranker last, and only if the telemetry says ordering is the problem. 2 needs
nothing.
