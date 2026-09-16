# The map

This page exists to answer one question: **which file do I open?** It is not a
design document and it does not explain why anything is the way it is — that
reasoning lives in comments next to the code it justifies, where it cannot go
stale silently. Read this to find the right file, then read the file.

Written 2026-09-11. Everything here was checked against the running system.

---

## 1. The four processes

All of them are pm2 apps. `pm2 list` shows them; `pm2 logs <name>` tails one.

| Process | Port | What it is | Restart interrupts |
|---|---|---|---|
| `tektonix` | 127.0.0.1:**8100** | The agent: API, dashboard, planning chat, the task graph. `uvicorn agent.server:app` | The current work pass. A task whose Store status is `running` **auto-resumes** a few seconds after boot; a **planning turn does not** — the operator re-sends the message. Paused states (approval, merge approval, escalated) survive untouched. |
| `model-router` | 127.0.0.1:**4001** | The model router (`services/model-router`). Every model call from every process goes through it, under an `agent-*` alias, and each caller holds its own key. | **A model call in flight dies**, and the task holding it escalates with "peer closed connection". Never restart this while a task is mid-call — stop the task first. |
| `agent-review` | 127.0.0.1:**4100** | Review dashboard, and the only write path into a live repo: `merge` (fast-forward only) and `restart` (build + pm2) | A merge or deploy in flight. Nothing else — it holds no task state. |
| `commit-reviewer` | 127.0.0.1:**4101** (control) | Polls each project's task branch, runs the checks, asks a model for a verdict, writes `state.json` | The review round in progress; it re-polls on boot. The gate re-asks, so nothing is lost except the round's spend. |

**Health.** Each one answers a local health route that makes no model call and
costs nothing, so it is safe to poll. The path differs per process -- the
agent serves the dashboard at `/`, so its route is under `/api`, and the
router splits liveliness from readiness:

```
curl -s 127.0.0.1:8100/api/health   # postgres, router, sandbox image, review secret
curl -s 127.0.0.1:4100/health       # review secret, project checkouts, reviewer state file
curl -s 127.0.0.1:4101/health       # review secret, projects, state.json
curl -s 127.0.0.1:4001/health/liveliness   # the router is up
curl -s 127.0.0.1:4001/health/readiness    # ...and has aliases and an upstream key
```

Each returns `503` when a check fails, so a probe that reads only the status
code is still correct. A configured secret reports `true` — never its value.

The project check answers for what this deployment has **onboarded**, not for
the built-in project map: a name in `projects.json` promises its checkout is
on disk, and a built-in repo this host never took on is reported as dormant
rather than broken. A fresh install with nothing onboarded is healthy.

---

## 2. Where each secret lives

Nothing is duplicated except where two processes must agree on the same value,
which is called out explicitly.

| Secret | File | Read by |
|---|---|---|
| `LANGGRAPH_PG_DSN`, `AUTH_SECRET_KEY`, `SMTP_*`, `GITHUB_TOKEN` (fallback) | `.env` | the agent |
| `MODEL_ROUTER_KEY` | `.env` | the agent — **must equal** `MODEL_ROUTER_KEY` below |
| `OPENROUTER_API_KEY`, `MODEL_ROUTER_KEY` | `services/model-router/.env` | the router; the two Node services read the OpenRouter key from here for their own model calls |
| `REVIEW_CONTROL_SECRET` | `.env` **and** `services/shared/.env` | the agent sends it, both Node services check it. The two copies must match |
| GitHub tokens for the inbox | Postgres, encrypted with `AUTH_SECRET_KEY` | the agent. Managed in Settings → GitHub, never in a file |
| Per-project deploy keys | `keys/<project>.key` (`AGENT_KEYS_DIR`), plus whatever `~/.ssh/config` points at | git, through the host's SSH |
| Per-project secret files copied into a review worktree | listed in `projects.json`, stored under `services/commit-reviewer/review-secrets/<project>/` | the reviewer, so checks can run |

Check the whole layout at once with `.venv/bin/python scripts/doctor.py`: it
verifies presence, file modes, key length and that the two pairs that must
match actually do, comparing them by hash rather than printing them.

`REVIEW_CONTROL_SECRET` used to live in the router's `.env`, which made the
model proxy a secrets bus. The services still fall back to that path with a
warning so an upgrade keeps working — see `services/shared/service-env.js`.

---

## 3. Three checkouts, and which is which

The single most common confusion. Every project has up to three copies:

| Path | What it is | Who writes it |
|---|---|---|
| `/home/<project>` | **Live.** What the world is running. | Nobody, except the merge step of a deploy (fast-forward only) |
| `/home/agent-workspaces/<project>` | **Task worktree.** A git worktree of live, on a per-task branch `agent/<task-id>`. Mounted into the sandbox container as `/workspace` | The agent. Every edit a task makes lands here first |
| `services/commit-reviewer/worktrees/<project>-<sha>` | **Review worktree.** A detached checkout at the exact commit under review, with the project's secret files copied in and its installed dependencies bound in read-only, so checks can run | The reviewer, then deleted |

A review worktree contains what git contains, which is not enough to run
anything: `node_modules`, PHP's `vendor/`, Elixir's `deps/` and a bundled
Ruby project's `vendor/bundle` are all gitignored. The reviewer borrows them
from the live checkout, **bound read-only** — the code about to run against
them is by definition unreviewed, and a writable borrow would let it edit
what production has installed. When the branch changes its own manifest the borrow is
wrong anyway — those are the old dependencies — so the reviewer installs that
stack's into the worktree instead, with scripts and plugins disabled: the
same `--ignore-scripts` discipline the npm path has always used. That covers
`package.json`/lockfiles, `composer.json` and `mix.exs`.

Bundler is the exception. `bundle install` builds native extensions, which is
code execution at install time and has no `--ignore-scripts` equivalent, so a
branch that changes its `Gemfile` gets neither an install nor the borrow: the
reviewer records why as a failed setup check, and the verdict accounts for it
rather than being quietly green against the wrong gems.

A task's diff is the task worktree against its branch point. The live checkout
never moves until the gate approves and the operator approves the merge.

---

## 4. The graph is two nodes

`agent/outer_graph.py`:

```
START → work → verify_and_ship → (work | END)
```

- **`work`** (`agent/nodes/work.py`) drives one whole deep-agent pass: the
  coordinator and its subagents, every tool call, the budget guard.
- **`verify_and_ship`** (`agent/nodes/verify_and_ship.py`) commits, runs the
  checks, waits for the reviewer's verdict for that exact sha, and — when
  everything says yes — merges and deploys **inside this node**. There is no
  third node; a deploy preflight failure is a stage of this one.

**Resting states.** The graph returns `END` and the task simply waits, holding
its checkpoint. Nothing is lost and every one of them is resumable:

| State | Means | Ends when |
|---|---|---|
| `pending_approval` | a gated action needs a yes, or `ask_user` asked a question | the operator answers (`POST /api/tasks/{id}/approve`) |
| `pending_merge_approval` | the review passed; the final look is the operator's | the operator decides (`POST /api/tasks/{id}/merge-decision`) |
| `escalated` | the agent cannot proceed (budget, a loop, a merge failure) | the operator resumes, optionally with more budget |
| `done` / `error` | settled | a resume, which is allowed and needs a message |

A task is never a dead end: every state above can be resumed from the
dashboard or `POST /api/tasks/{id}/resume`.

**One task per project.** Held as a Postgres *advisory* lock keyed by the
project name (`agent/graph.py`), not an in-process lock — so a second worker
or an overlapping restart cannot run two tasks against one worktree, and a
crash releases the claim when its connection closes.

---

## 5. Where things are

```
agent/
  server.py            the API: auth, tasks, planning, github, settings, uploads
  outer_graph.py       the two-node graph and its routing
  nodes/               work.py, verify_and_ship.py
  deep_agent.py        the build agent: seats, tools, the approval gate
  planning_chat.py     the planning agent: three seats, brief-first, draft gate
  graph.py             Postgres pools, the project advisory lock
  health.py            what "up" means (section 1)
  github_settings.py   tokens (encrypted) and per-project inbox policy
  github_inbox.py      the poller, the items, the signed approve links
  middleware/          budget_guard, repeat_guard, sanitize_tool_calls,
                       hidden_tools, pinned_brief, model_pin, todo_nag
  tools/               files, bash/sandbox, git, review_gate, planning_tools,
                       github_tools, vision, checks
scripts/
  doctor.py            checks the configuration above, without printing a secret
  backup.sh            + verify_backup_restore.sh (docs/backup.md)
  package_release.sh   a tarball with the dashboard prebuilt
services/
  model-router/        the router: config.yaml (the aliases and their pins;
                       yours, gitignored, seeded from config.example.yaml),
                       its .env, and logs/routing.jsonl (the billed ledger)
  agent-review/        merge + deploy control, review dashboard
  commit-reviewer/     the verdict: checks, the model review, state.json
  shared/              projects.json reader, service secrets reader
frontend/src/          the dashboard (Vite + React)
docs/middleware.md     which rule is attached to which agent, and what it forbids
docs/playbooks/        how to add a role, an inbox source, a runtime knob
docs/runbooks/         symptom → check → action, for when something is wrong
tests/                 pytest, plus node tests for the two services
```

---

## 6. When something is wrong

`docs/runbooks/` has one page per symptom, each in the same shape: what you
see, what to check, what to do. Start there rather than here.

---

## 7. When you are adding to it

Two pages, for the two questions the source does not answer quickly.

[docs/middleware.md](middleware.md) is the inventory of what each middleware
forbids and which of the six agents it is attached to. The modules under
`agent/middleware/` carry the incident that produced each rule, at length;
that is the story, and the inventory is the index. Subagents do **not**
inherit the coordinator's chain, which is the single most expensive thing to
learn by discovery.

[docs/playbooks/](playbooks/README.md) covers the three things added most
often -- a model role, a GitHub inbox source, a runtime knob. Each playbook
starts with a test that fails until the work is finished and names the file
still unwired, because every one of those extension points is silent when you
miss a step.
