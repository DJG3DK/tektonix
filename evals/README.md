# The golden-task eval suite

The same question, asked twice.

`agent/benchmarks.py` measures production tasks and answers *is the agent
getting better in practice*. That is the right question for the Analytics
panel and the wrong one for *did that prompt change help*: production tasks
move with whatever the operator happened to need that fortnight, so two
windows are never the same question.

This is a fixed set of goals against a fixed repository state, run on demand.

```
scripts/run_evals.py --verify      # free: are the fixtures and specs sound?
scripts/run_evals.py --dry-run     # free: what would run, and what would it cost?
scripts/run_evals.py               # the real thing, real money
scripts/run_evals.py --only py-median --ceiling 5
```

## What a run actually does

It drives the **same compiled graph a production task runs** — the real work
node, the real check suite, a real commit, the real reviewer. The one thing it
does not do is merge, and that needs no special mode:
`require_merge_review=True` already parks a task after a READY verdict waiting
for the operator's final look, and the harness is an operator who never
approves. The verdict is recorded, the episode is written,
`_route_after_verify` returns `END`, and nothing is merged anywhere.

A second "run but don't ship" code path would be a path production never
takes, and it would drift.

## How a task is scored

**On its assertions, never on its outcome.** A task can ship, pass its checks
and earn a READY verdict having "fixed" the bug by weakening the test — the
one failure every gate in this system is blind to. So each task declares
predicates a script can evaluate:

| kind | means |
| --- | --- |
| `checks_pass` | the gate's own check suite went green |
| `review_verdict` | what the real reviewer said |
| `command` | a command run **in the sandbox** against the finished tree |
| `file_matches` | a file exists and matches a regex |
| `file_absent` | a file is not there |
| `diff_touches` | the diff changed every one of these globs |
| `diff_excludes` | the diff changed none of these globs |
| `max_iterations` | it got there within N redos |

`command` runs through `run_shell_sandboxed`, because it executes against a
worktree the agent just wrote. A harness that shelled out on the host would
reintroduce, in the name of measurement, the privilege escalation the reviewer
was fixed to close.

### Goal assertions and guards

```yaml
- diff_excludes: ["tests/**"]
  guard: true
  why: the fix must not be to weaken the suite
```

A **goal** assertion must *become* true; a **guard** must *stay* true.
`--verify` refuses a suite whose goal assertions already pass on the pristine
fixture — an assertion true before the agent runs tests nothing, and a task
made entirely of them reports success however badly the agent behaves. Guards
are exempt, because being true at the start is their whole job. A task with
no goal assertion at all is refused at load.

`max_iterations` and the episode's `iteration_count` count **redos**, not
passes: `verify_and_ship` only increments it in `_loop_back`, so `0` means it
landed first time.

## Fixtures

Three, under `fixtures/`: `pylib` (Python), `nodelib` (Node), `webui` (a
component and its stylesheet, so the ui-styling category — which lands on a
different coder seat — is actually exercised).

They carry **no dependencies**. Checks are `python3 -m unittest` and
`node --test`, both already in the sandbox image, so a run needs no npm
install, no package index and no network. A golden suite whose result depends
on whether a registry was up that morning is not a benchmark.

Each is **rebuilt per task, not reset**. A reset that misses a stray file does
not fail — it contaminates the next task, and the number that comes out is
wrong in a way nobody can see.

## Isolation

A run must not be able to touch production, and the failures here are all
silent — the run would succeed while corrupting something else.

| | how |
| --- | --- |
| episodes | its own SQLite store (`eval_config`), so eval tasks never enter the numbers the Analytics panel is computed from |
| projects | `AGENT_PROJECTS_JSON` → an eval-only file listing only fixtures. The live `projects.json` is never written; that rule is what stops a model granting its own code network egress |
| reviewer | its own pair on free ports (`REVIEW_SERVICE_PORT` / `REVIEW_CONTROL_PORT`) |
| verdict state | `REVIEW_STATE_DIR` |
| reviewer spend | `REVIEW_USAGE_LOG` — the dashboard's figure is summed from the live one |
| review worktrees | `REVIEW_WORKTREE_ROOT` |
| **which projects exist at all** | `REVIEW_ONLY_PROJECTS_JSON=1` |

That last one is not obvious, and the first smoke run of
`agent/evals/reviewer.py` is why it exists. Pointing `AGENT_PROJECTS_JSON` at
the fixtures is not enough: the reviewer also merges in
`builtin-projects.local.js`, and a built-in-only project appears whether or
not `projects.json` mentions it. The eval instance came up polling the
operator's real repositories — and with a live task branch on one of them, two
reviewers would have been racing on the same repo, writing verdicts into
different state files, each unaware of the other's worktree.

`tests/test_evals_isolation.py` pins every row of that table.

## Cost

Every task is a real agent run spending real money. The suite tracks
cumulative spend and **stops before** starting a task that could cross the
ceiling (default `$25`), reporting what it has rather than continuing quietly.
The bound is actual spend so far plus the next task's cap — a true hard bound,
and one that uses the budget rather than reserving caps tasks never reach.

## Reports

`logs/evals/<timestamp>.json`, plus a table on stdout and a diff against the
previous run naming what regressed. The aggregate half is the **same six
numbers** `agent/benchmarks.py` computes for production, so a run is directly
comparable to the fortnight it was run in — an eval scoring itself on private
metrics would answer a question the dashboard cannot be compared against.

## Adding a task

1. Write `tasks/<id>.yaml`. The `id` must match the filename — they are one
   identity used two ways, and drift makes a report row nobody can grep for.
2. Give it at least one goal assertion, and guards for whatever it must not
   break.
3. `scripts/run_evals.py --verify` — it must report the goal assertions
   correctly failing and the guards holding.
4. `scripts/run_evals.py --only <id>` to run just that one.

Prefer goals with one right answer. A golden task is compared across months;
one whose correct output is a matter of taste produces a number that moves for
reasons nobody can reconstruct.
