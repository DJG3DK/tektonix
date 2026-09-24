# SWE-bench Verified

Tektonix, run on SWE-bench Verified and graded by the official harness.
`scripts/run_swebench.py` does both; `agent/evals/swebench.py` is the
module.

## What makes it a SWE-bench number

These are the conditions under which "X% on SWE-bench Verified" is a true
statement. Changing any of them makes it a different benchmark.

| | |
| --- | --- |
| tasks | `SWE-bench/SWE-bench_Verified`, the published 500, unmodified (the dataset's current official home; the older `princeton-nlp` copy has the same tasks but lacks the `image` field the harness now needs) |
| input | the issue (`problem_statement`) and the repository at the task's commit. Never `hints_text`, `patch`, `test_patch`, `FAIL_TO_PASS` or `PASS_TO_PASS` |
| environment | each task's official image, `swebench/sweb.eval.x86_64.<id>`, with the code at `/testbed` and its `testbed` conda env active |
| attempts | one per task (pass@1) |
| isolation | each task is its own project: no memory, episode or history is shared between tasks |
| internet | none. The published fix is on GitHub, so a benchmark project has no GitHub, web-page or cross-project tools, and its sandbox runs with `--network none` |
| grading | `swebench.harness.run_evaluation`, the official harness, in its own environment. Tektonix never grades itself |

The repository is copied out of the task's image, not cloned. Several
(astropy, scikit-learn) are built in place there, and the image's git history
already ends at the task. The runner checks that no commit is reachable
beyond it before a task starts, and refuses the task if one is.

## What is Tektonix's own, and so part of what is measured

- **The agent:** the same graph, models, prompts and tools as production,
  minus the tools listed above. The runtime settings are production's, read
  from the live store and recorded in the summary.
- **Its gate:** a benchmark project declares its own checks. Every changed
  Python file must compile, and the package must import. They are light on
  purpose: the grading tests are hidden, and a full suite (Django's) runs for
  hours.
- **The independent reviewer:** it reviews every diff, as it does in
  production. It runs no checks of its own here.
- **The prediction:** the task's committed and uncommitted changes against
  the image's commit. Test files are left out: the harness applies the task's
  own test patch after the prediction, and a clash fails the task. Build
  output and caches are left out too.

## Setup (once)

    python3 -m venv /opt/swebench/venv
    /opt/swebench/venv/bin/pip install swebench datasets
    /opt/swebench/venv/bin/python -c "import datasets, json; \
      ds = datasets.load_dataset('SWE-bench/SWE-bench_Verified', split='test'); \
      json.dump([dict(r) for r in ds], open('/opt/swebench/verified.json', 'w'))"

The harness lives in its own environment so its dependencies never meet
Tektonix's. `SWEBENCH_PYTHON` and `SWEBENCH_DATASET_JSON` override the paths.

## Running

    scripts/run_swebench.py --instances psf__requests-1142 django__django-10097
    scripts/run_swebench.py --sample 50 --seed 1 --parallel 3
    scripts/run_swebench.py --all --parallel 4

`--budget` caps each task ($3 by default) and `--ceiling` the whole run.
SWE-bench sets no time limit. `--task-timeout-min` (180) only guards against
a hung task. The budget is what bounds the work, as it does in production.

The tasks run in batches of `--batch-size` (20). A batch's images are pulled,
its tasks run, the harness grades them while the images are still there, and
then the images and the batch's repositories are deleted. All 500 images
together are over a terabyte. No image is pulled, and no batch starts, while
the disk is at `--max-disk-pct` (80%) or fuller. The run stops there, and
the tasks it never reached are recorded as `not_run` with the reason.
`--keep-images` keeps the images. At the end, the harness rebuilds one report
for the whole run from its own logs of every batch (`--rewrite_reports`),
without running anything again.

The **SWE-bench Verified** section of the Analytics page shows every run as it
goes, with each task's result, the tests the harness ran, the patch and the
agent's whole conversation. A run whose id starts with `diag-` is labelled
diagnostic and is never shown as a score.

Everything goes to `logs/swebench/<run-id>/`:
- `predictions.jsonl`: the submission, in the harness's format.
- `tektonix.<run-id>.json`: the official harness's report (`resolved_ids`
  and the rest).
- `grading.log`: the harness's output.
- `summary.json`: per task, the outcome, cost, time, patch size, the
  reviewer's verdict and **which models the router served**, plus the runtime
  settings.

## Validating the environment: the reference fixes

    scripts/run_swebench.py --gold-check --all

This grades the dataset's own reference fix for each task, exactly as a
prediction is graded. A task whose reference fix fails cannot be resolved by
anyone in these images. Run it before a full run, and publish the list with
the score.

**Known: `django__django-10097`.** Its code is Django from 25 June 2018, which
migrates SQLite tables by renaming them through `<table>__old`. SQLite 3.26 and
later break that (Django ticket #29182, fixed in 2.1.5/2.2), and the official
image ships SQLite 3.45.3. Its test patch changes only data files, so the
harness runs Django's entire suite. Migrations then corrupt the test database
partway through, with `no such table: main.django_site__old` and about 985
errors after it, 7 of them in the task's FAIL_TO_PASS. The reference fix fails
exactly as our agent's identical fix does (checked 2026-09-24). It is the only
Django task with both conditions: the other three built on pre-fix code run
only their own test modules. Fixing it means changing the image, which is
SWE-bench's to do. Doing it here would make the result not SWE-bench.

## Advertising a result

- **Only a full run is "SWE-bench Verified".** Quote it with the date, the
  harness version (`pip show swebench`), the models (`summary.json`), and the
  gold-check's list of tasks no one can resolve in the official images. A
  sample is "N of a 50-task sample", never "N%" alone.
- **The leaderboard** (swebench.com) wants the predictions, the harness's
  logs and the agent's trajectories. The runner writes each task's full
  conversation, every message and tool call, to `trajectories/<id>.json` in the
  run folder.
