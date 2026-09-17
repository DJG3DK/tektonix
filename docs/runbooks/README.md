# Runbooks

One page per symptom, all in the same shape: **what you see → what to check →
what to do**. They assume nothing about who you are beyond shell access to the
box and the dashboard in a browser.

| Page | You are looking at |
|---|---|
| [stuck-task.md](stuck-task.md) | A task the dashboard shows as running, that is not moving |
| [consolidation.md](consolidation.md) | The consolidation card saying never-run, stale or failed |
| [router-refusals.md](router-refusals.md) | "No endpoints found", a role that silently answers from its fallback, a reviewer that returns nothing |
| [merge-vs-github.md](merge-vs-github.md) | A merge that succeeded while GitHub stayed behind |

**The commands assume this deployment's layout.** They are written to be
pasted, which means they carry concrete paths; on another install, substitute:

| In these pages | Means |
|---|---|
| `/home/3d-agent` | your `AGENT_HOME` — where install.sh put the agent |
| `/root/.pm2/logs/...` | wherever pm2 writes for the user it runs as (`pm2 logs <app>` avoids the path entirely) |
| `/home/storefront`, `/home/webapp` | your own projects' live checkouts, from `projects.json` |
| `.venv/bin/python` | the agent's virtualenv |

Most snippets start with `cd /home/3d-agent`; `cd "$AGENT_HOME"` works just as
well if you export it.

Before any of them, the cheapest question: **is everything up?**

```bash
.venv/bin/python scripts/doctor.py --quiet   # config: modes, key pairs, paths
curl -s 127.0.0.1:8100/api/health | python3 -m json.tool   # the agent
curl -s 127.0.0.1:4100/health     | python3 -m json.tool   # merge + deploy
curl -s 127.0.0.1:4101/health     | python3 -m json.tool   # the reviewer
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:4001/health/liveliness   # the router
pm2 list
```

Each health route returns 503 when one of its checks fails and names the
failing dependency. None of them costs a model call.

The map of what these processes are, and which file holds what, is
[../architecture.md](../architecture.md).
