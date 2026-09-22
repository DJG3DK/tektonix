"""The golden-task eval suite: does a change to this agent make it better?

agent/benchmarks.py measures PRODUCTION tasks, which is the right way to ask
"is it getting better in practice" and the wrong way to ask "did that prompt
change help". Production tasks move with whatever the operator happened to
need that fortnight; two windows are never the same question asked twice.

This is the same question asked twice. A fixed set of goals against a fixed
repository state, run on demand, scored two ways: per-task assertions that
say whether the agent actually did the thing, and the same six benchmark
metrics computed over the run so an eval is directly comparable to the panel.

WHAT MAKES IT HONEST.

* It runs the real pipeline -- the real work node, the real check suite, the
  real commit, the real reviewer. Not a simulation of them. The one thing it
  does not do is merge: `require_merge_review` already parks a task on the
  operator's decision after a READY verdict, so the harness simply never
  approves, and gets the verdict without touching a live branch. No special
  mode in the graph, no second code path that could drift from the real one.

* It is isolated by construction, not by convention. An eval run opens its
  own SQLite store and checkpointer in a temp directory, so its episodes
  cannot reach the live store and skew the very panel it exists to explain,
  and it reads an eval-only projects.json through AGENT_PROJECTS_JSON so it
  can never see -- or be seen by -- a real project.

* It stops on cost. Every task is a real agent run spending real money, so
  the suite tracks cumulative spend and halts the moment it crosses the
  ceiling, reporting what it got rather than quietly continuing.

WHAT IT DELIBERATELY DOES NOT DO.

No model judges the output. Assertions are objective predicates a script can
evaluate -- a test exits 0, a file matches, the diff did not touch the tests
directory. A rubric scored by a model would catch more, and would make the
benchmark's own verdict drift from run to run, which defeats the purpose of
having a fixed suite at all.
"""
