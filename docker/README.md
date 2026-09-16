# Running the bundle

One command, any host with Docker: Windows, macOS or Linux. See
`docs/roadmap-packaging.md` for where this fits and what it does not cover yet.

```
cp docker/.env.example .env      # set OPENROUTER_API_KEY and PROJECTS_DIR
docker compose up -d
```

Then open <http://localhost:8100> and sign in with the admin account the logs
print on first run (`docker compose logs agent`).

## What you need

Docker Desktop (Windows/macOS) or Docker Engine (Linux). Nothing else —
Python, Node, Postgres and ripgrep all live in the images.

## The two settings that matter

**`PROJECTS_DIR`** is where your repositories live *on the host*. Everything
under it appears to the agent as `/projects/<name>`.

```
Linux/macOS   PROJECTS_DIR=/home/you/code
Windows       PROJECTS_DIR=C:\Users\you\code
```

**`BIND_ADDRESS`** defaults to `127.0.0.1`, so the dashboard is reachable only
from the machine running it. That is the intended shape for a local install:
no domain, no TLS, nothing exposed. Change it deliberately, and put something
in front of it if you do.

## Why the Docker socket is mounted

The agent runs every command the model issues inside a throwaway container. In
the bundle it asks the *host's* Docker daemon to start those, so they are
siblings of the agent container rather than nested inside it — no privileged
mode, no second storage driver.

The consequence is the one thing to understand here: **bind mounts are resolved
by the host daemon, not by the agent container.** The agent sees a project at
`/projects/foo`; the daemon must be told `C:\Users\you\code\foo`. That is what
`AGENT_HOST_PATH_MAP` does (compose sets it from `PROJECTS_DIR`), and it is why
a wrong `PROJECTS_DIR` produces an agent that reads an *empty* repo rather than
an error.

## First run

The entrypoint generates `AUTH_SECRET_KEY` and `REVIEW_CONTROL_SECRET` into the
data volume, writes an empty `projects.json`, waits for Postgres, and builds
the sandbox image if the host does not already have it. All of it is
idempotent: `down` and `up` again changes nothing.

## What is not in the bundle yet

* **The review services** (`agent-review`, `commit-reviewer`). The review gate
  is optional — INSTALL.md §7 — and they are the next piece.
* **Remote projects.** This bundle runs against projects on the machine it runs
  on. Driving a project on another box is M2/M3 in the roadmap.

## Data

Four named volumes: `pgdata` (tasks, memory, users — this is the one worth
backing up), `agentdata` (generated secrets), `agentlogs`, `routerlogs`.
`docker compose down` keeps them; `down -v` destroys them.

## Model pins

`services/model-router/config.yaml` is mounted read-only if you have one. Without
it the image ships `config.example.yaml`, which is a working default rather
than a stub.
