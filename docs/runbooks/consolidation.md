# The consolidation card is not green

Since 2026-09-28 the agent schedules memory consolidation itself
(`agent/jobs.py`): once a day, at the first quiet moment after it is due
(no task running, no planning turn open), checked two minutes after
startup and every ten minutes after that. The memory panel has a **Run
now** button. There is no cron in the compose bundle or the desktop app,
and none is needed. A host install may keep the old cron line
(`scripts/consolidation-cron.sh`); both write the same marker,
`data/last_consolidation.json`, which is what the card reads, and a run
from either side means the other is not due again for a day.

## What you see

On the Models page, the consolidation card says one of:

| Card | Means |
|---|---|
| **never run** | no marker file at all -- the job has not completed once on this box |
| **failed (exit N)** | the last run exited non-zero |
| **stale** | the last run succeeded, but more than 48 hours ago |
| a `marker_error` line | the marker file exists and cannot be read or parsed |

Consolidation is what turns a pile of episodic task records into the memory the
agents actually read. Nothing breaks the moment it stops; the agents simply get
slowly worse at remembering this project, which is exactly why it needs a card
of its own. A failed run used to be indistinguishable from a healthy one.

---

## Check

Paths below are under the install root: the checkout on a host install,
the `agentdata` volume in the bundle (`docker compose exec agent cat
/app/data/last_consolidation.json`).

**The marker the card reads:**

```bash
cat data/last_consolidation.json
```

```json
{"ran_at":"2026-09-11T04:15:01Z","exit_code":0,"ok":true}
```

**The log the marker points at** -- the last run's output, with its header:

```bash
tail -40 data/consolidation.log
```

**Is the scheduler waiting on something?** The jobs endpoint says when each
job last ran, whether it is due, and why the last due check did not run it
(a task in flight, a planning turn open):

```bash
curl -s 127.0.0.1:8100/api/jobs | python3 -m json.tool
```

---

## Act

### never run

The agent has not had a quiet moment since it started, or it has not been
up for the two-minute settle. Press **Run now** on the memory panel, or
wait: a due job runs at the next ten-minute check with nothing in flight.
It is safe to run at any time -- it reads episodes and writes memory, it
does not touch a repo.

### failed (exit N)

The log's tail names the cause. In order of likelihood:

- **A model refusal.** Consolidation runs on the `agent-consolidator` alias. If
  its pin cannot do what the prompt needs (tool calls, a large context), the
  run dies mid-way. See [router-refusals.md](router-refusals.md), then repin
  the role on the Models page and press Run now.
- **Postgres unreachable.** `curl -s 127.0.0.1:8100/api/health` will already be
  red on `postgres`. Fix that first; the run needs the store.
- **A bad `.env` or a missing venv** after an upgrade on a host install --
  the log shows a Python traceback rather than a model error.
  `.venv/bin/python -c "import agent.server"` reproduces it in one line.

Run it again after the fix; the card follows the marker.

### stale

The last run succeeded but is more than 48 hours old, so the job is not
getting its quiet moment: a task or planning turn has been open at every
check, or the agent has not been running (a desktop app closed at night
runs the job when it is next opened and idle). `/api/jobs` shows which.
Run now works whenever the agent is up.

### marker_error

The file exists but does not parse. Look at it, then simply delete it and
press Run now; the card goes back to "never run" until that finishes.

```bash
cat data/last_consolidation.json
rm data/last_consolidation.json
```

---

## What not to do

- **Do not "fix" it by writing the marker by hand.** The card would go green
  while memory stayed stale, which is the exact failure this card was added to
  end.
- **Do not run `scripts/run_consolidation.py` directly** when you want the card
  to update -- the scheduler and the cron wrapper are what write the marker
  and preserve the exit code.

---

## The cron line, on a host install that keeps one

Optional. The wrapper writes the same marker and fails loudly:

```bash
crontab -l | grep consolidation
# 15 4 * * * /path/to/tektonix/scripts/consolidation-cron.sh
/path/to/tektonix/scripts/consolidation-cron.sh; echo "exit $?"
```

If it is in another user's crontab than the one the agent runs as, it never
fires; `systemctl status cron` says whether cron itself is up.
