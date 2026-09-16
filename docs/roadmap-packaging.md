# Roadmap: packaging, Windows, GitHub projects, and the CLI

Written 2026-09-13; remote projects replaced by GitHub-based projects and
the CLI added 2026-09-15. Every claim about the current code below was checked
against the tree, not remembered — including, this time, a "done" in M0 that
was not.

This is a plan, so unlike `docs/architecture.md` it *is* allowed to talk about
things that do not exist yet. Anything describing the present tense has a file
path next to it.

---

## What we are actually trying to fix

Today the agent must be installed on a Linux box that the projects also live
on. That is two separate constraints wearing one coat:

1. **The host must be Linux** — a packaging problem.
2. **The projects must be on the same machine** — an execution problem.

They are independent, they have different fixes, and conflating them is why
this looks bigger than it is.

A third thing is *not* a constraint, despite the README's framing: **a domain
was never required.** `install.sh` prompts for nginx + certbot and skips it
happily; the agent binds `127.0.0.1:8100`; INSTALL.md §3a already documents an
SSH tunnel as the alternative. "Local edition" is a packaging and onboarding
job, not an architecture change.

---

## What ties us to Linux right now

Four things, and only two of them are interesting.

| Thing | Where | Severity |
|---|---|---|
| Process-group kill | `agent/tools/shell.py` — `start_new_session=True` + `os.killpg`, so killing host-side git takes its children with it | Small. The only POSIX-only construct in the Python tree; Windows wants `CREATE_NEW_PROCESS_GROUP` + `taskkill /T` |
| Same-path bind mounts | `agent/tools/sandbox.py` — mounts host absolute paths into the container **at the same absolute path** (`-v {main_git}:{main_git}:ro`), because a worktree's `.git` is a pointer file naming an absolute path | **This is the design constraint.** `C:\repo\.git` cannot be mounted at `C:\repo\.git` inside a Linux container |
| Installer and process manager | `install.sh` (bash, apt/pacman/dnf), `ecosystem.config.js` (pm2) | Medium, and largely sidestepped by shipping containers |
| Hard dependencies | Postgres, Docker, Node, Python | All cross-platform; the problem is asking a laptop user to install four things |

The agent core, LangGraph, the router, the dashboard and both Node review
services are already portable. Nothing there needs porting.

---

## Where execution actually happens

This matters for the remote-projects work, because it is a much smaller
surface than it looks. Everything funnels through four places:

| Seam | File | What runs through it |
|---|---|---|
| Sandboxed shell | `agent/tools/sandbox.py` → `run_shell_sandboxed` | Every command the model runs, and the whole check suite (`agent/tools/checks.py`) |
| Host shell | `agent/tools/shell.py` → `run_shell` | Host-side git only (`agent/tools/git.py`) |
| Direct file I/O | `agent/tools/files.py` | The `read` / `write` / `edit` tools |
| The reviewer | `services/commit-reviewer` | Its own git and check runs, in Node |

Four. That is the whole list.

---

## Answering the question that prompted this

> If my projects live in GitHub rather than on the agent's box, does the
> backend have to be installed wherever they run?

**No — and it never needed to be.** The agent does not work against a running
deployment; it works against a git repository. So the answer is to *clone*,
not to reach across the network at execution time. The clone is local, every
command and check runs locally, and the only remote thing is `origin`.

This is a much smaller job than remote execution, and most of it already
exists:

* **The push is already there.** After `git merge --ff-only`,
  `services/agent-review/server.js:304` runs `git push origin <branch>`
  whenever an `origin` remote exists — best-effort, reported in the response
  rather than assumed. Added 2026-08-23, after two commits deployed live while
  GitHub sat two commits stale.
* **Deploying is already optional.** `deploy`, `pm2Apps` and `build` are all
  absent from two of the three projects in `projects.example.json`. A project
  with none of them skips the restart stage; nothing breaks.
* **Push credentials already exist per project.** `agent/deploy_keys.py`
  generates, installs and verifies an ed25519 deploy key against the real
  remote, with an SSH host alias per project.

Two things are genuinely missing, and one of them is a trap:

1. **Nothing clones.** `provisioning.detect_project` blocks on a path that is
   not already a git repository, so onboarding starts from a directory you
   already have.
2. **Nothing ever fetches.** `sync_workspace_to_base` moves the workspace to
   *local* `main`, and `agent/tools/git.py:135` says outright that there is
   "no remote to fetch through". That is correct while the live repo on this
   box IS the source of truth. It becomes wrong the moment GitHub is: a
   teammate pushes, the agent edits stale code, and the `--ff-only` merge
   fails with diverging branches. This exact failure already happened once
   locally (2026-08-26) and is the reason `sync_workspace_to_base` exists at
   all — the GitHub case just reintroduces it through a different door.

---

## Milestones

Each is independently shippable and independently useful. Ordering is by
dependency, not by appetite.

### M0 — Portability groundwork *(no user-visible change)* — **done, d2c2747**

* ~~An executor interface with exactly one implementation.~~ **Did not
  ship.** `d2c2747` touched three files and delivered the two items below;
  the host-vs-sandbox executor is real work and now lives in M3. Corrected
  2026-09-15, having been listed as done for two days.
* Fix the process-group kill in `shell.py` so it degrades correctly off Linux.
* Make the sandbox's mount paths come from one place, so "host path equals
  container path" becomes a property we can satisfy deliberately rather than
  an accident of the host layout.

**Done when:** the full suite passes with no behaviour change, and the mount
paths are computed in one function.

### M1 — The container bundle *(this is "the Windows package")* — **core done**

A `docker compose` stack — agent, Postgres, router, both reviewers — that runs
identically wherever Docker Desktop runs: Windows, macOS, Linux.

* The agent image gets the Docker CLI and the host socket, so it spawns
  *sibling* sandbox containers rather than nesting.
* Projects mount at a fixed root (`/projects/<name>`) inside the agent
  container, and the sandbox uses that same path — which is what makes the
  same-path constraint above hold by construction instead of by luck.
* First run generates secrets, creates the admin account and prints the URL.

**Done when:** a Windows machine with Docker Desktop runs `docker compose up`,
reaches the dashboard, and completes a task against a repo cloned **locally on
that machine**.

**Risk to watch:** the socket mount means the sandbox containers are siblings
on the host daemon, so their bind mounts are resolved by the *host*, not by the
agent container. The fixed `/projects` root is what keeps those two views
identical. If this is wrong, we find out here — which is why it is M1 and not
M4.

**Settled, 2026-09-13.** Built and run on Linux: all four health checks green,
and a sibling container spawned by the agent container read a real repo's file
contents *and* its git history through the map. Two things the build found that
a review would not have: the router image needs its own requirements in a
clean image (the host install had those dependencies already, so
`requirements.txt` never needed them), and an unset `REVIEW_CONTROL_SECRET` makes `/api/health` return
503 forever, so the entrypoint generates one the same way it generates the
signing key.

**Still open:** the review services are not in the bundle yet (M1b), and
Windows itself is untested — that is the operator's next step, and the only
variable left is whether Docker Desktop's own path translation agrees with the
map.

### M2 — GitHub-based projects *(replaces the old remote-projects milestone)*

The agent stops requiring that you already have the repo checked out, and
starts treating the local checkout as a **cache of GitHub** rather than as a
deployment it owns.

* **Clone on add.** The onboarding wizard accepts a GitHub URL (or
  `owner/repo`) as well as a path. It clones into `AGENT_PROJECT_ROOTS`, then
  hands off to the existing `detect_project` flow completely unchanged — the
  wizard's detection, the worktree, `projects.json` and every downstream
  consumer stay exactly as they are.
* **Fetch before each task.** `sync_workspace_to_base` gains a fetch against
  `origin` for projects marked as GitHub-backed, so a task always branches
  from the real tip. This is the trap named above; it is the part that must
  not be skipped.
* **Ship as a pull request, not a push to main.** A per-project
  `ship: "push" | "pr"`. `push` is today's behaviour and stays the default for
  an existing project. `pr` opens a PR from the task branch and reports the
  URL instead of fast-forwarding the base branch. `require_merge_review`
  (outer_state.py) is unaffected and still gates everything either way.
* **Credentials.** Read+write now, where `Config.github_token` is documented
  as read-only. The deploy-key path in `agent/deploy_keys.py` already covers
  push; the PR route needs a token with `pull_requests: write`.

**Done when:** a project that exists nowhere but GitHub can be added from the
dashboard by URL, complete a task, and land it as a pull request — with no
`deploy` block, no pm2, and nothing pre-checked-out.

**Known gap at this point:** the clone is still on the machine running the
agent. That is the point, not a limitation.

### M3 — The CLI *(the agent lives in the repo)*

`3d-agent` run from inside a checkout, the way `claude` and `cursor` are. No
dashboard, no nginx, no Postgres, no accounts. Same graph, same tools, same
plan/verify loop.

The seams for this already exist, which is why it is M3 and not a rewrite:

| What the CLI needs | Seam today | Work |
|---|---|---|
| Local persistence | `graph.open_checkpointer` / `open_store` are the only two constructors | Swap Postgres for SQLite under `.3d-agent/`. The store is opened with **no `index=`**, so there is no vector search to port — it is plain key-value plus filtered `asearch` |
| A workspace | `provisioning.create_worktree` | A worktree under `.3d-agent/work`, so `live` and `sandbox` still differ and the whole task/branch/review model is untouched |
| Somewhere to render | `_stream_graph` needs only `store`, `graph` and a `_publish` sink | Pass a terminal renderer instead of the SSE bus |
| Config | `load_config()` hard-requires `SMTP_*` and `AUTH_SECRET_KEY` via `os.environ[...]` | Make the server-only fields optional; a CLI has no email and no sessions |
| Running commands | `sandbox.run_shell_sandboxed` (Docker per call) | Keep Docker where it exists. Add a host executor gated by the HumanInTheLoop middleware that `auto_approve_commands` already drives — that is precisely the CLI permission prompt, and it is already built |
| Checks and review | `agent/tools/checks.py` runs the suite directly; the reviewer is two Node services over HTTP | Run checks in-process. The reviewer becomes optional: `--review` for those who want it, off by default |

**Note on M0.** Its bullet list claims an executor interface shipped. It did
not — `d2c2747` delivered `AGENT_HOST_PATH_MAP` and the process-group fix, and
touched three files. The host-vs-sandbox executor is real work and it lives
here, in the row above.

**Done when:** `pip install` (or a single binary), `cd` into any git repo,
`3d-agent "fix the failing test"`, and watch it plan, edit, check and commit
on a task branch — with nothing running but the CLI itself.

**Cost to be honest about:** this is the milestone with the most genuinely new
code. Everything above it reuses the existing loop; this one gives it a second
front end and a second persistence backend, and both need their own tests.

### M4 — Desktop shell *(optional, last)*

A small Tauri wrapper that starts the stack, shows status and opens the UI. It
is a finish, not a foundation: building it before M1 means shipping an
installer around a stack that still needs manual setup.

---

## Non-goals

* **Remote execution (the old M2/M3).** Dropped 2026-09-15. Pointing
  `DOCKER_HOST` at `ssh://…` would relocate every model command and check for
  free, but `agent/tools/files.py` (direct local I/O behind `read`/`write`/
  `edit`) and the Node reviewer would not ride along, and every edit would
  become a network round trip. Cloning from GitHub gets the same outcome —
  work on a repo that lives elsewhere — for a fraction of the work, because
  the code ends up local and only `origin` is remote.
* **Mounting a remote filesystem** (sshfs and friends). Superseded by the
  same reasoning, and worse: the sandbox and the checks would execute locally
  against a network filesystem.
* **Native Windows without containers.** WSL2 runs the current code today with
  zero changes, and everything a native port would need is superseded by M1.
* **A hosted multi-tenant edition.** Not what these milestones are for.

---

## Open questions

* Does the socket-mounted sibling-container model hold on Docker Desktop for
  Windows, where the daemon lives in a WSL2 VM and the "host" path is already
  a translation? M1 answers this.
* Postgres in the bundle, or SQLite for a single-user local edition? Postgres
  is one more container but zero code change; SQLite is a smaller install and
  a real port of the checkpointer. **M3 forces this question anyway** — the
  CLI needs the SQLite backend regardless, so the bundle can adopt it once it
  exists rather than deciding now.
* For a GitHub-backed project, what owns the base branch when a human and the
  agent both push? Fetch-before-task makes the agent lose races safely
  (it rebranches from the new tip); it does not make a half-finished task
  survive one.
* Does the CLI share `projects.json` semantics at all, or is a repo it is
  invoked inside simply an implicit project of one? The second is simpler and
  probably right.
