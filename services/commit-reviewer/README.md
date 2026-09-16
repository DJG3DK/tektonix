# commit-reviewer

An independent post-commit review gate for [Tektonix](https://github.com/DJG3DK/tektonix).

The agent's own claim of "done" carries no authority. When a task commits, this service checks out
that commit, runs the project's **real** checks, and hands the diff to a model for review. Only a
`READY` verdict merges. It runs as its own process, with its own model pin, deliberately: a gate the
agent can influence is not a gate.

## How a review runs

1. **Detect.** Poll each project for an unreviewed `agent/<task-id>` branch. The review unit is that
   **branch plus its merge-base** — fixed at the fork point.

   This matters more than it sounds. The original design compared the sandbox HEAD to the live HEAD
   and inferred everything else. When live moved ahead, the diff was computed *backwards*: a
   branch's additions read as deletions of everything live had gained since. That manufactured two
   `blocking` findings against a commit which had in fact **added** the trading-safety settings it
   was accused of removing. A fixed base makes direction unambiguous by construction rather than by
   guard.

2. **Set up a worktree.** Detached checkout at the reviewed sha, with:
   - `node_modules` linked or bind-mounted from the live checkout (never installed fresh per review)
   - **review-only credentials**, never the live ones (see below)
   - gitignored test inputs bound **read-only** — for the trading bot that is 348MB of candle data,
     without which 31 of 49 suites self-skip and report green having asserted nothing

3. **Run the real checks.** Lint, tests, build, database drift, secret scan. Whatever the repo says
   its checks are — the reviewer runs `test:review` and lets the repo decide what is safe, rather
   than carrying its own copy of the list.

4. **Review the diff.** One call, one `submit_review` tool call back. The model never writes or runs
   code; the mechanical work already happened.

5. **Record.** Verdict, findings and usage to `state.json` / `history.jsonl` / `usage.jsonl`.

## Credentials

Review worktrees get a **non-production** credential set from `review-secrets/<project>/`, and the
path fails closed — a missing review secret is recorded as a failed check and never falls back to
the live one.

This replaced copying the live `.env` and `config/*.json` straight in, which meant a review — the
step whose entire job is to run code nobody has vetted yet — executed that code holding production
exchange keys, the live JWT secret, real SMTP and Telegram credentials, and for the e-commerce
project a live payment-provider secret, supplier keys able to submit real orders, and the production
`DATABASE_URL`.

The replacements are structurally valid rather than blank: encrypted exchange keys that decrypt to
obvious dummies, a real bcrypt hash, mail pointed at an unroutable host, side-effecting flags forced
off, and `DATABASE_URL` aimed at the test database. All 49 suites pass on them.

## Model

Routed through the model router as the `agent-reviewer` alias, so the model is swappable
from the agent's dashboard and its spend is rated and logged. It previously called OpenRouter
directly with a hardcoded model id, which made it invisible three ways: absent from the model
picker, no cost rates anywhere, and never seen by the router's logging callback.

**What this role needs, in order:** instruction-following and judgement, long context, then reliable
tool calling. It is *not* agentic — one call, no loop — and it never writes code. Its failure mode is
**false positives**: a wrong `blocking` finding costs a full fix-and-review round. Coding-tuned
models are the wrong instinct here, because coding benchmarks reward producing plausible code while
this job rewards *not* producing a plausible finding.

Note it pins `tool_choice` to `submit_review` — a forced tool call, which several model families
reject in thinking mode. `REVIEW_MODEL_OVERRIDE` is an evaluation-only escape hatch for A/B'ing a
candidate against a real commit before pinning it; a raw model id (containing `/`) bypasses the
router and talks to OpenRouter directly.

## Layout

```
reviewer.js        the whole service — detection, worktree setup, checks, review, verdict
review-secrets/    non-production credentials per project (gitignored)
state.json         latest verdict per project (gitignored)
history.jsonl      append-only review log (gitignored)
usage.jsonl        per-review model usage and cost (gitignored)
worktrees/         ephemeral per-review checkouts (gitignored)
```

## Running it

Runs under pm2 alongside the agent. It needs the router reachable at `MODEL_ROUTER_URL`
(default `http://127.0.0.1:4000`) and `MODEL_ROUTER_KEY` in the router's `.env`.

**Zero npm dependencies** — `reviewer.js` is Node stdlib only, so there is no `package.json` and
nothing to install. (The `require('argon2')` you may grep into is a string: a check command
executed inside the *reviewed project's* worktree, resolved against that project's own
`node_modules`, not this service's.)

**You must edit `PROJECTS` before this reviews anything of yours.** It is defined in source, per
deployment, on purpose: it names each repo, its live and workspace paths, its real check commands,
which gitignored inputs get bind-mounted read-only, and which credential files come from
`review-secrets/`. Those are facts about *your* projects that no config template can guess — and
the check commands especially deserve to be read, not copied, because they encode which of your
test suites are safe to run against a detached checkout.
