# The consolidation card is not green

## What you see

On the Models page, the consolidation card says one of:

| Card | Means |
|---|---|
| **never run** | no marker file at all — the cron has not completed once on this box |
| **failed (exit N)** | the last run exited non-zero |
| **stale** | the last run succeeded, but more than 48 hours ago |
| a `marker_error` line | the marker file exists and cannot be read or parsed |

Consolidation is what turns a pile of episodic task records into the memory the
agents actually read. Nothing breaks the moment it stops; the agents simply get
slowly worse at remembering this project, which is exactly why it needs a card
of its own. A failed run used to be indistinguishable from a healthy one.

---

## Check

**The marker the card reads:**

```bash
cat /home/tektonix/data/last_consolidation.json
```

```json
{"ran_at":"2026-09-11T04:15:01Z","exit_code":0,"ok":true}
```

**The log the marker points at** — the last run's output, with its header:

```bash
tail -40 /home/tektonix/data/consolidation.log
```

**Is it scheduled at all?**

```bash
crontab -l | grep consolidation
# 15 4 * * * /home/tektonix/scripts/consolidation-cron.sh
```

---

## Act

### never run

Either the cron line is missing, or the wrapper has never completed.

```bash
crontab -l | grep consolidation || echo "NOT SCHEDULED"
# add it:  15 4 * * * /home/tektonix/scripts/consolidation-cron.sh
```

Then run it once by hand and watch it. It is safe to run at any time — it
reads episodes and writes memory, it does not touch a repo:

```bash
/home/tektonix/scripts/consolidation-cron.sh; echo "exit $?"
cat /home/tektonix/data/last_consolidation.json
```

### failed (exit N)

The log's tail names the cause. In order of likelihood:

- **A model refusal.** Consolidation runs on the `agent-consolidator` alias. If
  its pin cannot do what the prompt needs (tool calls, a large context), the
  run dies mid-way. See [router-refusals.md](router-refusals.md), then repin
  the role on the Models page and re-run the wrapper.
- **Postgres unreachable.** `curl -s 127.0.0.1:8100/api/health` will already be
  red on `postgres`. Fix that first; the run needs the store.
- **A bad `.env` or a missing venv** after an upgrade — the log shows a Python
  traceback rather than a model error. `.venv/bin/python -c "import agent.server"`
  reproduces it in one line.

Re-run the wrapper after the fix; the card follows the marker.

### stale

The last run succeeded but is more than 48 hours old, so the cron is not
firing. Check that cron itself is running (`systemctl status cron`) and that
the line is in the right crontab — the agent runs as root here, and a line in
another user's crontab will never fire.

### marker_error

The file exists but does not parse. Look at it, then simply delete it and run
the wrapper once; the card goes back to "never run" until that finishes.

```bash
cat /home/tektonix/data/last_consolidation.json
rm /home/tektonix/data/last_consolidation.json
/home/tektonix/scripts/consolidation-cron.sh; echo "exit $?"
```

---

## What not to do

- **Do not "fix" it by writing the marker by hand.** The card would go green
  while memory stayed stale, which is the exact failure this card was added to
  end.
- **Do not run `scripts/run_consolidation.py` directly** when you want the card
  to update — the wrapper is what writes the marker and preserves the exit code.
