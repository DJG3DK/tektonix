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

Or from the dashboard: **Analytics → Golden suite → Run suite** (admins
only). See [Running it from the dashboard](#running-it-from-the-dashboard).

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

## Fixtures and tasks

Five fixtures under `fixtures/`, thirty tasks under `tasks/`:

| fixture | what it stands in for | tasks |
| --- | --- | --- |
| `pylib` | a small Python reporting library | 6 |
| `nodelib` | a small Node utility library | 4 |
| `webui` | a component and its stylesheet, so the ui-styling category — which lands on a different coder seat — is actually exercised | 2 |
| `pyservice` | a Python shop back-end: sqlite catalog, static assets, money, a cache, order dates, request validation | 9 |
| `nodeapp` | a Node web back-end: HTML rendering, object merging, CSV import, a job queue, a router, config loading | 9 |

The tasks span nine categories: bug-fix, feature, ui-styling, performance,
investigation, security, refactor, tests and other. The two back-end fixtures
are where the harder ones live: SQL injection, path traversal (including
through a symlink), XSS, prototype pollution, a concurrency off-by-one, a
time-zone bug reported only by its symptom, half-up money rounding, and two
"write the tests" tasks scored by **mutation**: the assertion breaks the code
one documented rule at a time, and the agent's new suite has to fail every
time. A test suite that passes against broken code does not count.

Every one of the eighteen back-end tasks was also checked the other way
round before it went in: a hand-written correct fix, scored with the task's
own assertions, passes. `--verify` proves a task is not green before the agent
starts; that proves it can be green at all. It caught one assertion that
expected the wrong order and would have failed every correct answer.

Performance tasks time a workload the original code needs seconds for and a
linear version finishes in milliseconds — a margin of 100× or more, so the
result does not depend on how busy the box is.

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
| runtime settings | the **opposite**: production's, read once from the live store before the run switches to its own, and recorded in the report. They live in the task store, so a fresh one meant the defaults -- a 180s model-call timeout against production's 300s -- and a task failed that production would have finished |

That last one is not obvious, and the first smoke run of
`agent/evals/reviewer.py` is why it exists. Pointing `AGENT_PROJECTS_JSON` at
the fixtures is not enough: the reviewer also merges in
`builtin-projects.local.js`, and a built-in-only project appears whether or
not `projects.json` mentions it. The eval instance came up polling the
operator's real repositories — and with a live task branch on one of them, two
reviewers would have been racing on the same repo, writing verdicts into
different state files, each unaware of the other's worktree.

`tests/test_evals_isolation.py` pins every row of that table.

## Cost and wall-clock — measured, not estimated

First full run, 2026-09-22: **11/12 passed, $0.28, 67 minutes** — and the one
failure turned out to be this suite's fault, not the agent's (see below), so
the honest score is 12/12.

Second run, 2026-09-23, as a regression check after a day of lifecycle,
supervisor, workspace and router changes: **12/12, $0.24, 24 minutes**, every
review READY first time, no escalations.

| | measured |
| --- | --- |
| cost per task | $0.01 – $0.05 (median ~$0.02) |
| cost for all twelve | **$0.28** against a $25 ceiling |
| wall-clock per task | 64s – 1482s (median ~140s) |
| wall-clock for all twelve | **67 minutes** |

**Money is not the constraint; time is.** The fixtures are small, so a full
run costs pennies — but it takes over an hour, and one task (`node-chunk-zero`)
took 25 minutes on its own. That makes this an overnight or CI tool rather
than something to run between two edits. Use `--only` while iterating.

The ceiling is $25 and the thirty tasks' caps add up to $64.50, so
`--dry-run` marks the later ones `room?`. That is the worst case, every task
spending its whole cap. A real run checks the actual spend so far plus the
next task's cap, and at a few cents a task it never gets near $25.

The ceiling still exists and still matters: it bounds a suite that grows, or a
task that loops. The suite tracks cumulative spend and **stops before**
starting a task that could cross it, reporting what it has rather than
continuing quietly. The bound is actual spend so far plus the next task's cap
— a true hard bound, and one that uses the budget rather than reserving caps
tasks never reach.

## Running it from the dashboard

Analytics → **Golden suite**, visible to admins. It shows the latest full
run as a scorecard (pass rate, first-pass rate, cost and time per task, by
category), what regressed or got fixed since the run before, every run's
history, and each task with its failing assertions and diff. **Copy
scorecard** puts a one-line summary on the clipboard.

**Run suite** asks for a note (what changed, so the history says why a
number moved) and shows what the last run cost and took, scaled to the
current suite. The run is `scripts/run_evals.py --status-file`, started
**detached** (`setsid --fork`). A full run takes about an hour, and a deploy restarts
the server: pm2 kills a restarting process's whole tree, so a run started as
a child of the server would die with every deploy. The runner writes its
progress to `logs/evals/status.json` and the panel reads that, so progress
survives a restart and is correct whoever started the run, from the dashboard
or from a shell with `--status-file`.

Only one run at a time. A click is claimed in the status file **before** the
child exists, so a double click in the second it takes to start cannot start
two. **Stop** sends SIGINT to the run's session, which the runner turns into
an orderly stop: its reviewer pair shut down, its working tree removed, the
report written with what it has.

## Reports

`logs/evals/<timestamp>.json`, plus a table on stdout and a diff against the
previous run naming what regressed. A **failing** task also carries its actual
git diff; a passing one does not. The first full run is why: a task failed a
guard saying the test file must not change, the report recorded only that the
file was among the changed paths, and there was no telling whether the agent
had *added* a test or *weakened* one — opposite findings — with the fixture
already torn down. A path list says a rule was broken; the diff says what the
agent did. The aggregate half is the **same six
numbers** `agent/benchmarks.py` computes for production, so a run is directly
comparable to the fortnight it was run in — an eval scoring itself on private
metrics would answer a question the dashboard cannot be compared against.

## What the first run taught the suite

The single failure was `py-top-n-heap`, on a guard reading
`diff_excludes: ["tests/test_report.py"]` whose stated intent was *"the tests
that already pass should still pass untouched"*. The agent had added eight
edge-case tests — ties, `n` bounds, non-mutation, row identity — and changed
not one existing line. The suite failed it anyway, because **"do not weaken
the tests" had been written as "do not touch the file"**, and adding tests to
a refactor is a good thing to do.

Two lessons, both now in the suite:

* **Say what you mean, not something stricter that is easier to express.**
  `diff_excludes` on a test file forbids improvement as well as sabotage. The
  guard is now `file_matches` on the test functions that must survive, which
  is what "do not weaken" actually is.
* **Keep the diff.** Working this out needed the actual patch, and the first
  run recorded only the changed *paths* — "the test file changed" is the same
  sentence whether a test was added or deleted. Failing tasks now carry their
  diff.

A benchmark that marks good work as a failure is worse than no benchmark,
because the number looks like a result. This one only surfaced because a
failing task was investigated rather than believed.

## Adding a task

1. Write `tasks/<id>.yaml`. The `id` must match the filename — they are one
   identity used two ways, and drift makes a report row nobody can grep for.
2. Give it at least one goal assertion, and guards for whatever it must not
   break.
3. `scripts/run_evals.py --verify` — it must report the goal assertions
   correctly failing and the guards holding.
4. Check it is passable: apply a correct fix by hand to a copy of the fixture
   and run the task's `command` assertions against it. An assertion no
   correct answer satisfies fails the agent for the suite's mistake.
5. `scripts/run_evals.py --only <id>` to run just that one.

Prefer goals with one right answer. A golden task is compared across months;
one whose correct output is a matter of taste produces a number that moves for
reasons nobody can reconstruct.
