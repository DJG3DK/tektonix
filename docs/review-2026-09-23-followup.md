# Codebase review follow-up — 2026-09-23 (pass 2)

Second-pass review of `main` at `c8465af`, after the forty findings in
`docs/review-2026-09-23.md` were worked through. This file is the inventory
for a follow-up agent. No application code was changed in this review.

The first pass was verified against the tree, not against §17 of the earlier
document. The changelog's claim that every finding was answered is **true**
for the items that were supposed to change behaviour. A handful of comments
were missed in the refresh, and the new modules (lifecycle, supervisor, deep
links) have their own residues.

House rule unchanged: a comment explains something the code can't. Do not
flatten comments into silence. Do not "clean up" identity re-exports,
upgrade shims, or intentionally-off features. The "Do not fix" table in
`docs/review-2026-09-23.md` still applies.

---

## Scores (pass 1 → pass 2)

| Axis | Pass 1 | Pass 2 | Why it moved |
|---|---|---|---|
| Code cleanliness | 7.5 | **8.5** | Dead surface gone; helpers shared; leftovers are comments, not ghosts |
| Dead code / comment currency | 6.5 | **8.0** | Almost all cleaned; three stale path/line-count comments remain |
| Security (self-hosted, single operator) | 8.0 | **8.5** | Sealed DB-check env, gated review reads, approve-link CSRF, health reasons |
| Security (internet-facing / multi-tenant) | 5.0 | **5.5** | Review reads no longer open on the compose network. Still an operator console |
| Structure | 7.0 | **8.0** | `tasks.py` + `github` router out; lifecycle is one table. Tasks/planning HTTP still in `server.py` |
| Usability for this operator | 8.5 | **9.0** | Deep links, 2FA card, recovery codes typeable, supervisor heals plumbing |
| Usability for a new installer | 6.0 | **6.0** | Same install surface. Not this pass's job |
| Mainstream readiness | 4.5 | **5.0** | A URL you can send a teammate exists. Licence, CLI, and four processes did not move |

The product is now a well-kept single-operator console with a real review
gate and a supervisor that closes the "click Resume because the reviewer
blipped" class of work. It is still not a mainstream coding agent. That
gap is licence + install shape + no CLI, not leftover `LogEntryCard`.

---

## What the first pass got done (verified)

Every behaviour item from the first inventory was checked in the code.
`docs/review-2026-09-23.md` §17 is accurate. Compact restatement:

| # | Was | Now |
|---|---|---|
| 1 | `runDatabaseCheck` inherited `process.env` | `runSealed` for drift/seed/e2e **and** `psql`. `tests/test_db_check_env.py` plants secrets. **Residual:** `redis-cli flushdb` still uses `run()` — see F1 |
| 2 | `check_repo_access` invisible to inventory | `tests/test_repo_scope.py` snapshots 34 routes. A missing call fails. Substring match (a comment could fake it) — acceptable |
| 3 | Review-service GET APIs open | Every route but `/health` needs `X-Review-Secret`. Booted in `tests/test_review_services_gated.py` |
| 4 | Reviewer `/health` named projects | Count for anyone; names only with the secret |
| 5 | Approve-link POST had no site check | `Sec-Fetch-Site` + 10/min + `no-store`. GET still inert. URL-token residue documented in `SECURITY.md` |
| 6 | 2FA admin-only | Policy kept (product decision). Users can opt in from Settings |
| 7 | `disable2FA` had no UI | Settings card. Admins cannot disable (forced screen). Recovery codes now fit the login box |
| 8 | Router balance any user | `require_admin`. `BalanceStrip` already hid the chip |
| 9–14 | CSRF / rate-limit / health text / TOTP JSON / WS tests / upload `../` | Documented or tested as §17 says |
| 15–18 | Newsletter / socket / egress / bundle in-process | Correctly left alone |
| 19–22 | `LogEntryCard`, `/api/stats`, `current_step_index`, `counts()` | Deleted |
| 23, 25–31 | Stale comments and shared helpers | Fixed, except F2–F4 below |
| 32 | `server.py` the pile | `agent/tasks.py` + `agent/routers/github.py` out. Tasks/planning HTTP still in `server.py` — F8 |
| 33 | Host-vs-bundle comments | Named both modes where one stood alone |
| 34 | No deep links | `/task/:id`, `/planning/:id`, views. No router dependency. Residual: F7 |
| 35 | Credit chip | Resolved by #8 |
| 36–40 | Windows / CLI / licence / `api.ts` / cohesive piles | Correctly left alone |

Tests that matter are real, not rubber stamps: `test_db_check_env.py`
spawns Node and inspects child env; `test_repo_scope.py` fails if a
handler drops the call; `test_ws_auth.py` drives the handshake;
`test_review_services_gated.py` boots the Node services.

---

## Do not "fix"

Same list as the first document, plus:

| What | Why it stays |
|---|---|
| 2FA not required for `role="user"` | Product decision, now written in `SECURITY.md` "Signing in" |
| Approve-link token still in the confirmation flow | Documented residue. Do not invent a second token scheme in this pass |
| Supervisor regex classification | Conservative by design. Do not replace with a model call |
| `conclude_landed` has no age cap | Intentional: waiting on a merge that already landed is waiting for nothing |
| `as_node="work"` meaning "next is verify_and_ship" | LangGraph semantics. The module header of `lifecycle.py` is the map. Do not "fix" the names |
| Embeddings still off | Still waiting on telemetry |
| Flatten `server.py` in one pass | Still forbidden |
| Docker socket / sandbox egress | Still accepted |

---

## New and leftover findings

Numbered F1… so they do not collide with the first document's 1–40.

### F1. `redis-cli flushdb` still inherits `process.env`

**Severity:** low. Not agent-authored.
**File:** `services/commit-reviewer/reviewer.js` ~line 1341.

`psql` and the three project commands went through `runSealed`. The
`redis-cli -n 15 flushdb` call still uses `run()`, which merges
`process.env`. The comment two lines later says "runSealed, never run()"
for the agent-authored three; it does not claim redis is sealed.

**Shape of the fix:** `runSealed('redis-cli', …)` with no extras. Cheap,
and it makes the function's child set uniformly sealed. Extend
`tests/test_db_check_env.py` if the dump of child env already covers this
call; if not, plant a secret and require it absent from the redis-cli
child too.

### F2. `server.py` still says it is past 5,600 lines

**Severity:** comment-only.
**File:** `agent/server.py` lines 432–433.

```
# Per-seam routers (agent/routers/). server.py is past 5,600 lines and is
# being split one seam at a time
```

`wc -l` is **4,688**. `agent/routers/__init__.py` and
`tests/test_route_inventory.py` already say 4,688. This one comment was
missed in the refresh.

**Shape of the fix:** one sentence, today's count, same "split one seam
at a time" rule.

### F3. `rate_limit.py` still says the approve POST lives in `server.py`

**Severity:** comment-only.
**File:** `agent/rate_limit.py` lines 44–46.

The handler is `agent/routers/github.py`. The limit itself is correct.

**Shape of the fix:** name `agent/routers/github.py`.

### F4. `graph.py` still names `_run_task` as living on the server

**Severity:** comment-only.
**File:** `agent/graph.py` ~lines 327–328 (`read_with_retry` docstring).

"the writes in `_stream_graph`/`_run_task` (the live task-execution path)"
— `_run_task` is `run_task` in `agent/tasks.py`. `_stream_graph` is still
on the server.

**Shape of the fix:** name `tasks.run_task` and `_stream_graph`. Do not
move the helper.

### F5. Supervisor (and startup auto-resume) only see the newest 50 tasks per repo

**Severity:** medium for a busy project. Silent miss, not a crash.
**Files:** `agent/server.py` `_supervisor_deps.list_tasks` (line 118) and
`_auto_resume_orphaned_tasks` (line 185). Both call
`recent_items(..., 50)`.

The supervisor's job is every escalated / awaiting-merge task whose
cause has cleared. `recent_items` is "the newest 50", correct on both
Postgres and SQLite (`agent/store_paging.py`). A project with a long
history can have an escalated task sitting just outside that window:
the sweep will never see it, and it will look like the supervisor is
ignoring it.

Startup auto-resume has the same window: an orphaned `running` task
older than the 50 newest is not resumed.

The HTTP `GET /api/tasks` list also pages at 50 (`server.py` ~2860).
That one is a UI page. The supervisor is not a UI.

**Shape of the fix**

- Supervisor and auto-resume should walk with `iter_namespace` /
  `all_items` (whatever `store_paging` already uses for "the whole
  namespace"), filtered to `status in (escalated, awaiting_merge)` or
  `running`, not "newest 50 of everything."
- A test that the 51st escalated task is still healed. Put 51 metas in
  a fake store; the current `list_tasks` wrapper will drop it.
- Do not raise the HTTP list to "everything" as a side effect.

### F6. `push_failed` heals on a three-word match

**Severity:** medium. Bounded by cap + 24h + alerts, but it will spend
attempts (and review/model time on a gate heal) on a permanent refusal.
**File:** `agent/supervisor.py` `Kind("push_failed", re.compile(r"could not push"), None, "gate")`.

Workflow-scope refusals, missing deploy keys, and GitHub 403s all say
some form of "could not push". Those will not clear on retry. The
supervisor will backoff through four delays, then stop at
`auto_heal_attempts`. Each gate heal is cheap (no model call) but it is
still motion the operator did not ask for.

**Shape of the fix**

- Tighten the pattern to the connection/remote-unavailable family, **or**
  add a `needs` probe (origin reachable / deploy-key test) before
  retrying.
- Add the current "could not push: workflow scope" / permission strings
  to the LEAVE list in `tests/test_supervisor.py`.
- Do not drop `push_failed` entirely — a flaky remote is why it exists.

### F7. `/planning` and `/` are not inverses

**Severity:** low / UX.
**File:** `frontend/src/route.ts`.

`parseRoute("/planning")` → planning view. `routePath({ view: "planning" })`
→ `/`. Bookmarking `/planning` works once; the next in-app navigation
rewrites it to `/`. Plan-first landing on `/` is the 2026-08-28
decision and should stay.

**Shape of the fix:** either stop accepting `/planning` as a synonym
(redirect once to `/`), or treat `/planning` as a stable alias in
`PATH_OF`. A test in `route.test.ts` should pin the chosen pair so
`parseRoute(routePath(r))` rounds trips for every view including a
session-less planning view.

`App.tsx` popstate also leaves `selected` set when going back to a
non-task view. Harmless today because the pane follows `view`. Clear it
if a later view starts reading `selected` while `view !== "task"`.

### F8. `tasks` and `planning` HTTP still live in `server.py`

**Severity:** structural, already named. Not a regression.
**Files:** `agent/server.py` (4,688); `docs/todo.md` split section.

`start_task` / `run_task` are in `agent/tasks.py` (imports nothing from
the server; `tests/test_tasks_module.py` pins that). `github` has
followed. The remaining cut is the one the first review sequenced:

1. Move `_stream_graph` and the planning-turn machinery onto `app.state`
   the way `stream_graph` already is, **or** move them with the routers.
2. Extract `tasks` (list / get / stop / resume / approve / merge /
   message / stream) as `agent/routers/tasks.py`.
3. Extract `planning` the same way.
4. One seam per commit. Inventory + `test_repo_scope.py` stay green.
5. Do not extract `provisioning.py`, `history_index.py`, or the review
   proxy in the same change.

`frontend/src/api.ts` is 1,314 lines. Still later, still not with the
server split.

### F9. Two-factor runbook hardcodes `/home/3d-agent`

**Severity:** low / ops.
**File:** `docs/runbooks/two-factor.md` line 31.

The SQL is right (`disable_totp` shape, session purge). The `cd` is one
install's path. A doctor-style "the checkout is wherever you cloned it"
sentence, or `$(git rev-parse --show-toplevel)`, stops the next person
from running psql in the wrong tree.

### F10. `command_decision` applies one click to every pending action

**Severity:** low today; high if multi-action interrupts appear.
**File:** `agent/lifecycle.py` lines 190–202.

One approve/reject/respond is copied across `action_requests`. Correct
for the current "one interrupt, one card" UI. If a turn ever queues two
distinct gated calls, one click would answer both.

**Shape of the fix:** a comment on `command_decision` naming the
assumption (one card, N identical decisions). A test that `count == 2`
still fans out — so a future per-action UI fails this and has to change
the function on purpose. Do not build per-action UI in this pass.

### F11. An operator who *leaves* an infra escalation will see it healed

**Severity:** low, by design, worth knowing.
**File:** `agent/supervisor.py`.

Stopped tasks are skipped. Budget/loop/review escalations are skipped
(`classify` returns None). An infrastructure escalation younger than
24h, under the attempt cap, **will** be resumed after backoff even if
the operator saw it and walked away. There is no per-task "do not
heal" flag. `auto_heal_attempts=0` is the off switch, global.

That matches the module docstring ("it is supposed to heal itself").
If a per-task mute is wanted, it is a product feature: a "leave it"
control that sets a marker the sweep honours. Do not add it unless
asked. Do mention the global off switch on the Settings runtime card
if it is not already there (check `auto_heal_attempts` in
`RuntimeLimitsPanel`).

### F12. Admin deep-link URLs are parseable by any session

**Severity:** none for data (APIs still 403). UX only.
**Files:** `frontend/src/route.ts` `SIMPLE`; `App.tsx` admin gates.

A restricted user can open `/analytics` and get a pane that does not
show the data. Fine. Do not special-case `parseRoute` by role — the
server is the boundary. No work unless the empty pane is confusing;
then render the same "admin only" card the sidebar already implies.

---

## New modules — what is in good shape

Do not rewrite these. They are the best new structure in the tree.

**`agent/lifecycle.py`** — every resting state × action is one table.
`as_node` is documented at the top and the tests walk the matrix.
Merge approve pins the SHA the operator saw. Heal is not an HTTP
action. The 2026-09-23 resume/approval/re-ship bugs live here as
invariants.

**`agent/supervisor.py`** — plumbing vs task-owned is a regex table
with a LEAVE list in tests. Backoff, 24h cap, attempt cap, run-slot
claim so a heal loses to an operator resume. Alerts + task log on
every action. `Deps` lets the sweep be tested without a server.

**`agent/tasks.py`** — creation only; no import of `server`. Empty
goal refused before side effects. `write_task_meta` is read-merge-write.

**`agent/routers/github.py`** — inbox + approve links. CSRF, rate
limit, nonce, inert GET. Error pages do not echo exceptions.

**`frontend/src/route.ts`** — six paths, History API, no dependency.
Round-trip is tested except the session-less `/planning` alias (F7).

**`frontend/src/format.ts`** — one `relativeTime` / `modelColor` /
`shortModel`. Tests included.

---

## Priority

1. **F5** — supervisor and auto-resume must see every parked/orphaned
   task, not the newest 50. This is the one that can silently fail in
   production.
2. **F6** — tighten `push_failed` or probe before retry.
3. **F1** — seal `redis-cli` the same way as `psql`.
4. **F2, F3, F4, F9** — comment/runbook refresh. One commit.
5. **F8** — extract `tasks` then `planning`, one seam each, after the
   stream/turn machinery is reachable without importing `server`.
6. **F7** — pick a `/planning` vs `/` rule and pin it.
7. **F10, F11, F12** — comment / product, not this week unless asked.

---

## Suggested commit series

1. `fix(supervisor): scan every parked task, not the newest 50` — F5
2. `fix(supervisor): do not heal a push that will not succeed on retry` — F6
3. `fix(reviewer): seal redis-cli the same as psql` — F1
4. `docs: leftover path and line-count comments after the review split` —
   F2, F3, F4, F9
5. `fix(frontend): /planning and / are one route` — F7
6. Later PRs: `agent/routers/tasks.py`, then planning — F8

Each behaviour change needs a test that fails without it.

---

## Verification

```bash
.venv/bin/python -m pytest -q tests/test_supervisor.py tests/test_lifecycle.py \
  tests/test_db_check_env.py tests/test_repo_scope.py tests/test_tasks_module.py \
  tests/test_route_inventory.py
cd frontend && npx tsc --noEmit -p tsconfig.app.json && npm test -- src/route.test.ts src/App.routes.test.tsx
```

`tests/test_prompt_injection.py` and `tests/test_repo_scope.py` stay green
across any router move.

---

## Finding index (this pass only)

| # | Finding | Sev | New? |
|---|---|---|---|
| F1 | `redis-cli flushdb` still uses `run()` | low | residual of #1 |
| F2 | `server.py` comment still says 5,600 lines | comment | missed refresh |
| F3 | `rate_limit.py` names `server.py` for approve POST | comment | missed refresh |
| F4 | `graph.py` names `_run_task` on the server | comment | missed refresh |
| F5 | Supervisor + auto-resume window of 50 tasks/repo | medium | new (supervisor) |
| F6 | `could not push` heals permission failures | medium | new (supervisor) |
| F7 | `/planning` vs `/` do not round-trip | low | new (deep links) |
| F8 | Tasks/planning HTTP still in `server.py` | structure | leftover of #32 |
| F9 | Two-factor runbook hardcodes `/home/3d-agent` | low | new (runbook) |
| F10 | `command_decision` fans out one click | low | new (lifecycle) |
| F11 | Infra escalations auto-heal unless globally off | info | new (supervisor) |
| F12 | Admin URLs parseable by any session | none | new (deep links) |

Closed items from the first forty are not repeated as work. Mainstream
blockers (licence, CLI, four processes, internet-as-perimeter) did not
move and are still not this agent's job unless the operator says so.
