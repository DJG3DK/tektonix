# Running the bundle

One command, any host with Docker: Windows, macOS or Linux. See
`docs/roadmap-packaging.md` for where this fits and what it does not cover yet.

```
cp docker/.env.example .env      # set OPENROUTER_API_KEY and PROJECTS_DIR
docker compose up -d
```

**On Windows, double-click `Install Tektonix.bat`.** It asks for the two
values, starts Docker Desktop if it is installed but not running, waits for
it, brings the stack up and offers to open the console. The window stays open
at the end, so a failure is readable rather than a flash of text.

From a terminal, or on macOS and Linux with PowerShell 7:

```
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The `.bat` exists because neither half works without it. A `.ps1` cannot be
double-clicked into running -- Explorer opens it in Notepad -- and Windows
client editions default to an execution policy of Restricted, so even from a
terminal the bare path fails with "running scripts is disabled on this
system". The flag applies to that one process and changes nothing about your
machine.

It does exactly the two steps above: checks Docker is
installed AND running, asks for the two values with no sensible default,
writes the `.env`, and brings the stack up. `-DryRun` prints every action
without performing one; `-Yes` never prompts and fails naming whatever is
missing. Re-running is safe, keeps every value already in your `.env`, and is
also the upgrade path.

(The `cp` above is the reason it exists: Windows has no such command, so the
documented first step could not be followed as written.)

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

## The review gate is in the bundle

`agent-review` and `commit-reviewer` come up with everything else. A task's
branch is reviewed by a second model and only a READY verdict merges, which is
the whole point of the thing and used to be missing from the easiest install.

They share the agent's data volume read-only, so the control secret the agent
generates on first run already matches, and they read the same `projects.json`
a project onboarded from the dashboard writes.

The agent does not become ready until the router answers, and the reviewers
do not become ready until the agent does. A first task that used to start
while the router was still booting died mid-call with "peer closed
connection". `depends_on: service_healthy` is what closes that.

## What is not in the bundle yet

* **Deploying after a merge.** The review services can merge, but not restart
  your app: pm2 runs on the host and a container cannot reach it. That endpoint
  reports itself unavailable rather than failing, so a task ends at a real
  merge. Restart it yourself, or use the host install if you want that
  automated.
* **A project that lives only on GitHub.** The dashboard clones it here and
  ships as a pull request (M2). The clone is still on this machine; there is
  no remote execution.
* **Checks run in the reviewer process, not a sandbox.** On a host install the
  reviewer puts each project's checks in a container, because it is otherwise
  root on the real machine running code the agent wrote. Here it does not: this
  service is already inside a container, and it is deliberately NOT given
  `/var/run/docker.sock` — handing it the socket so it could start a sandbox
  would give it host-root equivalent, which is worse than the containment it
  already has. See `SECURITY.md`.
* **The logo tools.** `logo_render`, `logo_export_brand_kit` and the rest call
  LogoLoom's Node modules, and the agent image is Python-only — no Node, and
  60MB of image libraries for a feature most installs never touch. The agent
  detects their absence and simply has no logo tools, which is the intended
  outcome rather than a failure. The host install picks them up from
  `services/logoloom` (see `install.sh`).

## Data

Four named volumes: `pgdata` (tasks, memory, users — this is the one worth
backing up), `agentdata` (generated secrets), `agentlogs`, `routerlogs`.
`docker compose down` keeps them; `down -v` destroys them.

## Model pins

`services/model-router/config.yaml` is mounted read-only if you have one. Without
it the image ships `config.example.yaml`, which is a working default rather
than a stub.
