# A task that is not moving

## What you see

The dashboard shows a task as **running**. The stream has not produced a line
in a long time. Possibly the step counter is stuck partway, or the page shows
thinking bubbles that never stop.

There are six different causes behind that one appearance, and they need
different actions. Work down this page in order; each check is cheap.

---

## 1. Is the browser wrong, or is the task wrong?

A socket can die without either end noticing (laptop sleep, a NAT idle-kill, a
proxy dropping without a FIN). Since 2026-09-11 the page notices 70 seconds of
silence and reconnects itself, but an older tab, or one mid-reconnect, can
still be showing you a stale view.

**Check:** reload the page. If the task jumps forward, it was the socket.

**Look for the amber banner.** Under the thinking bubbles, past two minutes of
silence, the task view says *"No activity for N min"*. That banner means the
opposite of a dead socket: the connection is alive and the server is still
sending heartbeats, but nothing of substance has arrived. It is the page
telling you the agent itself is quiet — a long model call, or genuinely stuck.
If the banner is absent and the bubbles are animating, work is arriving.

If reloading does not change anything, the browser is telling the truth.

---

## 2. Is anything actually being spent?

The router logs one line per completed model call. If the task is really
working, that file is moving.

```bash
cd /home/3d-agent
python3 - <<'PY'
import json, time, datetime
rows = [json.loads(l) for l in open('services/model-router/logs/routing.jsonl') if l.strip()]
recent = [r for r in rows if r['ts'] >= time.time() - 600]
f = lambda t: datetime.datetime.fromtimestamp(t, datetime.UTC).strftime('%H:%M:%S')
print('calls in the last 10 min:', len(recent))
for r in recent[-5:]:
    print(' ', f(r['ts']), r.get('routed_model'), '| out', r.get('completion_tokens'), '| err', (r.get('error_detail') or '')[:60])
PY
```

- **Calls are arriving** → the agent is working. A single model call can take
  several minutes on a hard planning turn or a large diff. Leave it, or stop
  it from the dashboard if the spend is not worth it.
- **Nothing for ten minutes** → continue.

---

## 3. Is the process actually driving it, or is the task orphaned?

The Store records a task as `running`. If the agent restarted while a pass was
in flight, that record can outlive the process that was driving it. The
dashboard marks this as **orphaned** when it can tell; here is the direct way:

```bash
cd /home/3d-agent
set -a; . ./.env; set +a
timeout 60 .venv/bin/python - <<'PY'
import asyncio
from agent.config import load_config, PROJECTS
from agent.graph import open_store
async def main():
    cfg = load_config()
    async with open_store(cfg) as store:
        for repo in PROJECTS:
            for it in await store.asearch(("tasks", repo), limit=60):
                v = it.value
                if v.get("status") in ("running", "escalated", "awaiting_approval", "awaiting_merge"):
                    print(f"{repo} {it.key[:8]} {v.get('status')} ${v.get('cost_so_far') or 0:.2f}")
asyncio.run(main())
PY
```

Cross-check against the agent's own log for the auto-resume line:

```bash
grep 'auto-resume' /root/.pm2/logs/tektonix-error.log | tail -3
```

**Action — resume it.** From the dashboard, open the task and press Resume.
No extra budget is needed unless it ran out; the resume panel only shows the
budget field when the task is near its ceiling, and zero is accepted.

The equivalent API call, if you have a session:

```bash
curl -sX POST -b cookies.txt -H 'Content-Type: application/json' \
  -d '{"additional_budget_usd": 0}' \
  http://127.0.0.1:8100/api/tasks/<task-id>/resume
```

Resuming does not replan and does not lose the diff: the work continues from
the last checkpoint.

---

## 4. Is it waiting for you?

A task at `pending_approval` or `pending_merge_approval` is **resting, not
stuck** — the graph has returned and is holding its checkpoint. The dashboard
shows the prompt inline; a task that has been sitting there since before you
looked is easy to scroll past.

```bash
cd /home/3d-agent
set -a; . ./.env; set +a
timeout 60 .venv/bin/python - <<'PY'
import asyncio, json
from agent.config import load_config
from agent.graph import open_checkpointer, open_store
from agent.outer_graph import build_outer_graph
TASK = "<task-id>"
async def main():
    cfg = load_config()
    async with open_checkpointer(cfg) as cp, open_store(cfg) as store:
        graph = build_outer_graph(cfg, cp, store).compile(checkpointer=cp)
        v = (await graph.aget_state({"configurable": {"thread_id": TASK}})).values
        pa = v.get("pending_approval")
        print("waiting on:", json.dumps(pa)[:400] if pa else "nothing")
        print("escalated:", v.get("escalated"), "|", v.get("escalation_reason"))
asyncio.run(main())
PY
```

**Action:** answer it in the dashboard. An `ask_user` question wants a typed
reply; a gated command wants approve or reject.

---

## 5. Is pm2 killing it on a schedule?

A task that never finishes a pass, with the step counter frozen and the
dashboard showing nothing for hours, may be losing the pass to pm2's memory
watchdog. The agent holds one pass's whole message history in memory -- 70-80k
tokens, plus a subagent's beside it -- so a long task's footprint grows for as
long as the pass runs. Past `max_memory_restart` pm2 SIGKILLs it, auto-resume
reconnects, and the pass starts over. Nothing in the agent's own log says why:
it sees a KeyboardInterrupt, the same as any restart.

```bash
grep "max-memory-restart" /root/.pm2/pm2.log | tail -5
```

Live on 2026-09-12: four kills in eleven hours at a 1536 MiB cap (05:00,
07:57, 10:36, 11:42), on a task that was doing real work the whole time --
1167 deletions sat in the worktree, uncommitted, because no pass ever returned
to the gate.

**The cap is in `ecosystem.config.js`.** Raising it needs a restart, which
costs the pass in flight -- but a pass that is going to be killed anyway is not
worth protecting:

```bash
pm2 restart tektonix --update-env    # after editing ecosystem.config.js
```

Check what the box actually has (`free -g`) before choosing a number. The cap
is there to catch a runaway, not to recycle a working process.

---

## 6. Is another process holding the project?

One task per project is enforced with a Postgres advisory lock. A second
process holding it makes a task wait silently at the very start.

```bash
grep 'locked by another process' /root/.pm2/logs/tektonix-error.log | tail -3
```

```bash
cd /home/3d-agent; set -a; . ./.env; set +a
psql "$LANGGRAPH_PG_DSN" -c "select pid, classid, objid from pg_locks where locktype='advisory'"
```

A lock with no live task behind it means a process is still running that you
did not expect (`pm2 list`, `ps aux | grep uvicorn`). Postgres releases the
lock when that process's connection closes, so stopping the stray process is
the whole fix — never delete the row.

---

## What not to do

- **Do not restart `model-router`** to unstick a task. A model call in flight
  dies with it and the task escalates. Stop the task first if you must.
- **Do not restart `3d-agent` mid-pass** unless you accept losing that pass.
  A running task auto-resumes, a planning turn does not.
- **Do not delete the task** to "clear" it. Every state is resumable, and the
  diff lives in the task worktree until it is merged.
