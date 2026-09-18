# Changelog

## Unreleased

### The public tree is this product, not the box it grew on

The example router config that `install.sh` copies onto a fresh install still
carried the maintainer's mail-agent and trading-bot aliases, with comments
naming `/home/agentEmail` and a live trading gate. The Models page introduced
those callers by name. A visitor reading the seed file, or an operator opening
Settings → Models on a brand-new box, was looking at somebody else's other
software.

Those aliases are gone from `config.example.yaml`. Existing `config.yaml`
files are untouched. The Models page now says the property: aliases that do
not begin `agent-` are left alone.

The review-secret fallback the Node services claimed to keep for upgrades
still pointed at `services/llm-router/.env` after the directory was renamed
to `model-router`. Doctor warned about the current path; the services never
read it. They do now, and the pre-rename path remains a last resort.

A handful of docs and one fallback URL still sent people to the LiteLLM
port `:4000`. The router has been on `:4001` since the cutover.

CONTRIBUTING claimed to list the exact CI jobs and then omitted the landing
page, the model-router suite, and `ruff check services/model-router`. The
list is five jobs now, and the hygiene test pins the commands that used to
drift. The shipped example `projects.json` and watchdog list no longer name
the maintainer's other services.

### The review dashboard could not press its own buttons

`POST /api/review/check/:name` required the control secret, then proxied to
the reviewer **without** it, so "Check now" 401'd even when nginx had
injected the header on the way in. The proxy now forwards the secret it
already checked. A new nginx vhost also gets a `/_review/` location; an
existing one still needs that added by hand (INSTALL.md §7).

`wait_for_review` matched a verdict on the first 12 hex characters of the
sha. It now requires the full string.

A successful 2FA code left the verify-2fa rate-limit window counting, so a
few typos before a good code could lock a legitimate login. Success clears
it, the same way the password step already did.

## v0.7.0 — a page that is not the product, an installer that installs, and a gate that blames the right thing

### A Windows installer

The Docker bundle already ran on Windows: the bind-mount translation handles
drive letters, and the example env file gives the Windows form of the projects
directory. What was missing were the four steps between a downloaded copy and a
running console, the first of which is `cp`.

`install.ps1` does them — checks Docker is installed and running, asks for the
two values that have no default, writes the `.env`, and brings the stack up.
`-DryRun` and `-Yes` mean what they do in `install.sh`. It runs under
PowerShell 7 on macOS and Linux too; Windows is why it exists.

### The installer can install the prerequisites

It checked for git, Python, Node and Docker and then stopped, leaving you to
work out the package names — while it already drove apt, pacman and dnf to
install nginx and certbot.

It offers now, for those four plus ripgrep, and for Postgres at the database
step. It asks every time and the answer defaults to no. `--yes` is not consent:
an unattended run should mean "do not stop to ask me", never "put a Node runtime
and a database on this machine". `INSTALL_PREREQS=1` is the opt-in.

Only absent commands are offered. A runtime that is present but below the floor
gets the real remedy instead, since the distribution's package is the version
already installed — Debian and Ubuntu ship Node well behind the floor.

### Download the release instead of cloning

Releases now carry `tektonix-<version>.tar.gz` and its checksum, and the landing
page links to it. One archive serves both install paths on every operating
system, with the dashboard prebuilt so no Node build runs on install.

### The landing page is not part of the agent

It never should have been. `tektonix.io` serves the public page,
`agent.tektonix.io` serves the console, and `site/` is its own project that the
release tarball drops — a self-hosted install has no use for a page selling the
thing you just installed. The page ships no JavaScript; the console is
`noindex`.

**If you had the console installed as an app, reinstall it.** A service worker,
an installed app and a push subscription all belong to an origin, and the
console's origin changed. The old subscriptions were removed server-side;
re-enable notifications from Settings once it is installed from the new host.

### A newsletter instead of a sign-in button

"Sign in" pointed at a private console the visitor has no account on. It is a
newsletter signup now — a name, an address, and what the list is for: the
changelog for each release, plus what is being built next and what turned out
to be a bad idea. Nothing sends yet.

The form is plain HTML, since the page ships no JavaScript, and every outcome
redirects to a real page rather than a status code a browser would show to
nobody. A repeat signup counts as success. Addresses never reach the log, and
every subscriber gets an unsubscribe token at signup so the first issue does
not have to backfill one.

### The review gate blames the right thing

It rejected a clean commit three times and then escalated it. Four causes, all
fixed:

- The baseline re-ran failed checks against a base worktree provisioned
  differently from the branch, so an environment difference read as the
  branch's fault. Provisioning is now an argument the baseline reuses, and
  results are cached per environment as well as per commit.
- A check that could not RUN counted as one that FAILED. A missing command is
  recognised as the harness's fault now and escalates to a human on the first
  round, instead of being handed to an agent that cannot see the environment.
  It still blocks: a check that did not run is not a pass.
- A root dependency install was assumed to cover every package in a repository,
  which only holds when the root manifest declares workspaces.
- The rejection never said why. Check output was captured, shown to the review
  model, then dropped before being stored; it is kept with the verdict now, and
  unrunnable checks are listed apart from real failures.

### Every code scanning alert closed

Forty-four, plus the dependency alerts. Most were three hand-written versions of
one containment check, now a single function — one spelling was weaker than it
looked, since a symlink inside the directory passed it. Stack traces no longer
reach an error response, and the two places that build a command dispatch on a
literal rather than on a value that matched a list.

## v0.6.0 — a name of its own, a router of its own, and a console you can carry

**2026-09-17**

Seventy-five commits on top of v0.5.0, and the largest release so far.

The agent is called Tektonix and lives at tektonix.io. The dashboard installs
as an app on a phone and can wake you with a notification, in whichever of
five colour schemes you pick. Projects are things you create and remove from
that dashboard rather than files you edit by hand. The LiteLLM proxy is gone
and a router this repo owns has taken its place, the console's numbers come
from this box rather than from LangSmith, and Settings became a rail with one
section at a time.

### Tektonix

3D-Agent is now Tektonix, everywhere it is user-visible: the dashboard, the
landing page, the sign-in card, OG cards, the TOTP issuer, the router's
OpenRouter headers, the sandbox image tag, the release tarball and the GitHub
repository (`DJG3DK/tektonix`).

The mark is a plumb line and the palette is called Drafting: dark charcoal
with a gold accent. The landing page was reshot in it and condensed (features
8 → 4, controls 7 → 6), and the hero copy no longer describes a tier system
that was removed weeks ago. For search: a real `robots.txt`, a sitemap,
structured data, and the landing page is prerendered at build time so a
crawler with JavaScript off sees the page rather than an empty root div.

### The dashboard installs as a phone app

Chrome on Android now offers *Install app*, and iOS Safari's *Add to Home
Screen* does the same: a launcher icon, a standalone window with no browser
chrome, and the status bar in the app's own colour. It is the same React app
from the same build -- a web app manifest, a service worker and the icons the
brand generator already knew how to draw (`scripts/brand/build_assets.py`
gained a maskable variant, because Android crops an adaptive icon to whatever
mask the launcher uses and a rounded tile handed to that crop loses its own
corners).

There is deliberately no offline mode. This is a console onto a live agent --
running tasks, streaming logs, a review gate -- so a cached screen would be a
screen that lies. The worker exists for two narrower things: Chrome will not
offer to install an origin that has no fetch handler, and a cold start on a
bad connection should show the app rather than the browser's offline page.
It never touches `/api`, which keeps the session cookie, every mutation and
the task and planning WebSocket upgrades entirely out of its hands; hashed
bundles under `/assets/` are kept forever because their names change when
their contents do; and navigations go to the network first, with the cached
shell only as a fallback. That last one is not a preference: `index.html`
names the hashed bundles of the deploy it came from, so a stale shell served
while the network was fine would ask for a bundle that no longer exists and
white-screen the app after every deploy. Verified by installing a client,
deploying a new bundle underneath it, and reloading.

### Notifications on the phone, and a console in your own colours

The installed app now gets push notifications: the same alerts Telegram
already carried -- a task finishing, escalating, or waiting on an approval --
delivered to the lock screen with the app's own icon. It is one fan-out, not
two. `notify_operators` sends to both transports behind one copy of the
scoping rule, because the alternative is two copies that drift, and the last
time that scoping was wrong (audit H1) a single-repo account was receiving a
live feed of every project.

Permission belongs to a device rather than to an account, so **Settings →
Notifications** talks about this device and says how many others the account
has. Two cases are handled rather than papered over: a browser that has
already been told "block" can never be asked again from JavaScript, so the
panel sends you to site settings instead of offering a button that does
nothing; and iOS delivers web push only to an app installed to the Home
Screen, which the panel detects and explains instead of letting you subscribe
into silence. A subscription the push service reports as gone (404/410) is
deleted rather than retried, since those never recover.

The VAPID keypair is written once to `keys/vapid.json` and read back
thereafter. Regenerating it would invalidate every subscription ever issued,
and the symptom is "notifications just stopped" with nothing in any log.

**Five colour schemes**, per account, in **Settings → Appearance**: Drafting
(the brass the mark was drawn for), Indigo, Orchid, Ember and Moss. Pick one,
see it in a preview pane, then Save applies it everywhere.

The preview is the scheme, not a rendering of it. The pane carries
`data-theme` and every `[data-theme]` block in `theme.css` is scoped by
attribute, so the custom properties inside it resolve to the chosen scheme
while the rest of the page stays on the saved one. A mock built from
hard-coded colours would drift from the stylesheet the first time a token
moved, and it would drift silently -- it would still look like a preview.

Each scheme overrides only what carries its identity: the ground, the accent
family, the ambient light and the shadows' tint. The ambient light had to
become a token to do that; while it was a literal brass `rgba()` in App.css,
a theme changed its buttons and kept the original's light, which is most of
what makes a scheme read as a different room. Everything structural stays
shared, and so do the state colours -- running, waiting, done and failed mean
the same thing in all five, because a colour that carries meaning must not
move with a preference. Every foreground was computed against its own surface
and clears 4.5:1; `tests/test_themes.py` does that arithmetic rather than
trusting the comments, and holds the three copies of the scheme list (the
stylesheet, the picker, and the server's allow-list) in step.

### A project that did not exist yet

Until now a project had to exist before Tektonix could see it: the wizard
and `scripts/add_project.py` both take a directory that is already a git
repository, so the first commit of anything new was made somewhere else, by
hand, before the agent could be pointed at it. The planner's Repo dropdown
now has a third door, **New project…** (admins only): a name, an optional
description and, when a GitHub token is stored, a **Create a private GitHub
repo** box. The server makes the directory under the first allowed root,
refuses a path that exists or that fails the containment rule the wizard
applies, runs `git init -b main` with one commit (a repository with zero
commits gives the worktree no branch to start from), and then runs exactly
the wizard's provisioning steps with the wizard's recommended answers — one
code path, so a project that arrives by either door is wired identically.
The planner opens a session on it at once, and Settings → Projects and every
Repo dropdown refresh without a reload. `scripts/new_project.py` is the
headless twin. Creation is audited as `project.create`.

With the GitHub box ticked, the token is resolved before anything is
created, so a missing token is a refusal and not a half-made directory. The
private repository is made with `auto_init` off, a deploy key is minted for
the project and registered on it with write access, `origin` is set to the
SSH URL — never HTTPS, which the host's credential helper would push as the
operator's personal account — and `main` is pushed from the server process.
The agent still cannot push; this is the API process acting on an admin's
request, the same trust level as the review service's post-merge push,
which is why SECURITY.md's claim about agents and `git push` stays true. A
GitHub failure is a failed step, not an abort: the repository on disk is
real, and the remote can be connected from the project card later.

The planning agent can propose one. When a request turns out to be a new
application rather than a change to the current project, the agent asks for
a name and whether to create a private repo, restates both, and after the
operator confirms calls a `create_project` tool. The tool creates nothing:
it records a proposal, and the dashboard shows a confirm card with Confirm
and Dismiss. Confirm creates the project through the same path and moves the
session onto it, so the conversation continues against the new repository;
a non-admin is told that an admin must confirm. A model that could create
repositories by calling a tool would be one that could create them by being
asked to in a pasted document.

A brand-new project has no checks, so its first review is model-only — and
the merge that lands is the first moment the repository has code for
detection to read. After a merge in a project the reviewer reports as
having no checks, the ship path runs the wizard's detection on the live repo
with its recommended answers and writes `review.checks` into
`projects.json`; the task log shows which checks were written, or that none
are detectable yet. It fills an empty slot only: a non-empty list is never
overwritten, and a project whose checks come from
`builtin-projects.local.js` is left alone. This replaces the manual "add
checks" step that Auto for the GitHub inbox used to point at.

### Taking a project back off the agent

There was a button to add a project and none to remove one, so removing one
meant editing `projects.json`, deleting a worktree, finding the deploy key,
clearing the reviewer's state file and purging six store namespaces by hand --
in that order, because getting it wrong leaves a project half-configured and
unreachable. **Settings → Projects** now has *Remove from Tektonix*.

What it does not do is the half worth stating first: **the live repository is
never touched.** Not a file, not a branch the agent pushed to it, not the
remote. Removing a project means Tektonix forgets it. A control that sits in a
list of someone's own repositories must not be one misread click away from
deleting their code, so the confirmation also asks for the project's name
typed out, and removal is refused outright while a task or a planning turn is
in flight -- pulling the workspace out from under a running task would leave a
half-finished branch nobody owns.

The single exception is the opposite of destructive: `core.sshCommand` is
unset on the live repo, because the agent set it when it minted the deploy
key. Leaving it would point the operator's own git at a key file that no
longer exists and break every push they made by hand afterwards.

What the agent *learned* is a separate choice. **Archive** writes its memory,
generated skills, planning sessions, transcripts and task history to one JSON
file under `archives/`, then removes the rows; **delete** removes them without
the file. The archive is not write-only: the onboarding wizard lists any
archive matching the name of a project being added and offers to restore it,
so re-adding a project is a continuation rather than a fresh start. Archives
are listed in the same panel with their item counts and dates, and can be
deleted there, because an archive nobody can find is a file that accumulates
rather than a safety net.

An archive that fails to write refuses the whole removal rather than
continuing -- the operator asked to keep that, and deleting it anyway is the
one mistake here with no undo.

### The model router is ours

`services/model-router` replaces the LiteLLM proxy. All 21 deployments were
`openrouter/...`, so the proxy was a proxy in front of a proxy; what was
actually used was alias resolution, ordered fallbacks, a billed cost figure
and a callback into our own ledger, and the router does those four things
directly. It also retries transient failures, falls back on a stream up to
the first byte, keeps a stats view of the ledger, runs under pm2 and is
linted and tested in CI.

**Breaking:** LiteLLM is removed — the process, the package, its admin panel,
the WebAuthn gate, its vhosts and its cert. Five consumers were on `:4000`,
and only two were findable by grepping `.env` files. Every one now resolves
through the router on `:4001` **with its own key**; anything of yours still
pointing at `:4000` will stop. What to do when the router refuses a call is
in `docs/runbooks/router-refusals.md`.

The dead tier system (SIMPLE / MEDIUM / COMPLEX / REASONING) is gone from the
router and from the Models page, which now says what it actually controls.

### The model pins are yours

`services/llm-router/config.yaml` is gitignored. The Models page rewrites it
on every repin, so every model change used to arrive in this repo as a diff
somebody had to explain — and an upgrade could overwrite pins an operator had
chosen. The shape is tracked instead, as `config.example.yaml`, which
`install.sh` copies into place on a fresh install and never touches again.

The example's pins are one deployment's answers on one day, not
recommendations. Three consequences worth knowing: the file is not in git, so
it belongs with your backups; a new managed role has to be added to the
example as well as to your live config, and a test enforces that; and a
checkout that has never been installed still reads the example, so the rate
table and the Models page work in a fresh clone.

### The dashboard's numbers come from this box

Three Analytics panels — per-role model usage, tool reliability, run health —
were read back out of LangSmith, which made an optional third-party service
load-bearing for "what is this agent doing", and cost a core: LangSmith's
input masking walked every run's payload on the event loop that serves the
dashboard. The panels now read the router's ledger and the tool-event table
here. Per-role usage is one row per role and says where each number comes
from; the ledger records the alias the client asked for, not only the model
it resolved to, so a role's traffic is no longer split ten ways.

### Settings is a rail

The Settings page was one 4,334px scroll of every card the deployment has,
at three different widths, with two unrelated cards both titled "GitHub". It
is now a rail — Account, Agent behavior, Notifications, Projects, GitHub,
Runtime limits, Environment, Audit log, with admin-only sections hidden from
accounts that cannot use them — and one section at a time, each in a single
readable column.

### Bash is for running things

Measured live: one test-writer subagent made 229 bash calls against one edit
and one write, patching JavaScript through Python heredocs while holding
`read`, `write` and `edit` the whole time. Every bash call starts a container,
so that is wall-clock, not style. The tool descriptions now say what each is
for, the read and shell nudges catch what the model actually types, the repo
tools accept `file_path` so a name slip is not a cliff, and an
empty-but-successful command says what it did.

### Planning and build

A planning session's transcript is kept where a restart cannot take it: the
in-memory buffer died with the process and the checkpoint's message list is
rewritten on every compaction, so a 78-minute turn had no readable history.
The build summarization ceiling is raised from 80k tokens and is tunable —
a coder rode the old ceiling for an hour, re-reading its own output after
each compaction discarded it. A reworded plan step is the same step, the
step counter cannot go backwards, and a verify pass with no diff can still
reach a conclusion.

Auto mode covers GitHub inbox tasks: a Dependabot fix that cannot change a
version string unattended was not safer, only slower. The merge gate still
applies to every one of them.

### The review services read projects.json live

Both Node services bound the project map once at startup, so a project
created or given checks at runtime did not exist for them until pm2
restarted: the reviewer never polled its branches, the deploy service
answered 404 for it, and the agent's wait for a verdict timed out on a
verdict that could not arrive. They now re-read `projects.json` on every
poll and every request. The reviewer also no longer crashes on a project
entry with no `review.checks` — exactly the entry a new project produces —
and returns a verdict instead of an internal error.

### Operating it

A health watchdog restarts only what a restart can fix: it polls each
service's live and ready probes, so a process that is alive but wedged, or
healthy in front of a dead database, is distinguished from one that died — the
silent outage the operator hit before. It covers storefront, webapp and its
compute service. The pm2 memory cap is declared, with its cost stated.

Performance: tool-call streaming is off (langchain-core re-parsed the whole
accumulated argument JSON on every chunk — 25 seconds of CPU for one 734-token
call), the plan-check nag no longer edits the system message so the cached
prompt prefix survives, and a task's bill is durable.

### Packaging

Two seams for the container bundle (`AGENT_HOST_PATH_MAP`, so bind-mount
sources resolve against the host the Docker daemon actually sees), then the
bundle itself: postgres + router + agent as a compose stack, one command on
any host with Docker, all four health checks green on Linux. Windows is the
next test. `docs/roadmap-packaging.md` has the plan, including the CLI and
GitHub-hosted projects.

## v0.5.0 — work that arrives on its own, ten stacks it can check, and a box a second person can run

**2026-09-11**

Thirty-six commits on top of v0.4.0. Three things changed: the agent can pick
work up from GitHub instead of waiting to be told; onboarding detects real
checks for ten stacks instead of two, so the review gate is not a no-op for
most repositories; and the parts that only ever had one operator — a global
auto-approve switch, an unrecorded approval, a lock that lived in one process
— now expect a second person.

### The GitHub inbox

The agent can pick work up from GitHub instead of waiting to be told.
A poller reads each project's open Dependabot pull requests, Dependabot
security alerts, code scanning (CodeQL) alerts grouped per rule, reviews
that request changes, and failing checks on the default branch (from check
runs or, with only *Actions: read*, from workflow runs — Dependabot's own
update jobs excluded). Each source has a
policy per project: **Off**, **Propose** (the item lands in the new
**GitHub** tab and an approve link goes out over Telegram and, optionally,
email) or **Auto** (the task starts at once, within a cap on open auto
tasks). Auto removes only the click: every task this creates runs the
review gate and keeps the operator's merge approval.

**Settings → GitHub** holds the tokens — fine-grained PATs stored encrypted
with the TOTP key, shown as a name and last four characters, with a Test
button that reports which projects a token reaches and whether it may read
alerts, code scanning and checks — the dashboard URL approve links are built on, delivery
switches, and the per-project policy table with budget, cap, author filter
and coder route. The PR tools resolve their token per project from the
same place; `GITHUB_TOKEN` in `.env` is now only a fallback.

Approve links are signed with the auth secret, expire after 48 hours and
are single-use. The link opens a page with one button and only the
button's POST acts, because messengers fetch links for previews.

Also: the origin-remote parser accepts SSH host aliases (a deploy key per
project means one per project; two of three live projects resolved to no
repository until now). `git remote get-url` applied insteadOf rewrites, so
on a box with a GitHub token helper the Settings page reported an SSH
origin as HTTPS and would have shown the helper's token; both the deploy-key
status and the PR-tool slug now read the configured URL.

### Docs and startup

README, INSTALL and the public landing page now describe the inbox's fifth
source (code scanning), the frontend seats, billed-cost budgets, runtime
limits and deploy keys. `MODEL_PLAN` / `MODEL_EXECUTE` / `MODEL_REFLECT` are
no longer required at startup — they had not been read since the alias
pipeline. The landing page's Node floor is 24+, matching `install.sh`.

### Onboarding that produces a real gate

Check detection knew npm properly and treated everything else as "a manifest
exists". A Go, Rust, Ruby, Elixir, Java, PHP or .NET project therefore
onboarded with an **empty checks list** — which does not make the review gate
weaker, it makes it a no-op that still reads as a gate, approving every change
because it runs nothing.

Ten stacks are now read from their own manifests: npm/pnpm/yarn, Python, Go,
Rust, Ruby, Elixir, Java (Maven, or Gradle through `./gradlew` when the repo
ships one), PHP, .NET, and Makefile targets as a fallback. Linters are
proposed only where the repo configures them — a lint its authors never opted
into would fail every review on findings they never agreed to.

The rules the npm path already followed carried over. A suite whose test files
call the network arrives **disabled**, with the file and the idiom named; a
file that stubs or serves its own HTTP (WebMock, httptest, `responses`, nock,
Bypass, Moq) is not counted as calling out, because flagging every honest
suite teaches an operator to click through the flags. A repo that declares its
own reviewer-safe suite is trusted over its aggregate one, in whichever
spelling its stack uses: `test:review`, a Makefile `test-review` target, a
cargo or mix alias, a Gradle `testReview` task, a rake task, a composer
script. And the client may still only narrow what the server proposed —
enabling a flagged suite sends a name, and the server substitutes its own
command.

`scripts/verify_stack_checks.py` (new) is why these are worth trusting: it
runs every proposed command against a fixture repo inside the official
toolchain image, then re-runs it against a deliberately broken assertion,
because a check that cannot fail is not a gate. That is how the rust image
shipping without rustfmt or clippy was found. CI runs the host half on every
push; the container half is a manual job.

The review checkout can now actually run those checks. A git worktree carries
what git carries, so PHP's `vendor/`, Elixir's `deps/` and a bundled Ruby
project's `vendor/bundle` were simply absent and `vendor/bin/phpunit` exited
127 — which reads as a broken suite rather than as nothing having installed
it. The reviewer borrows them from the live checkout, **bound read-only**,
since the code about to run against them is by definition unreviewed. A
branch that changes its own manifest gets a fresh install instead, with
scripts and plugins disabled (`composer install --no-scripts --no-plugins`,
`mix deps.get`) — the same discipline npm's `--ignore-scripts` has always
had here. Bundler is the exception and says so: `bundle install` builds
native extensions, so that branch gets neither the install nor the stale
borrow, and the reason is recorded as a failed setup check.

The same audit found the older node_modules bind had been logging a warning
and keeping a **writable** mount of live's modules when the read-only remount
failed. It unmounts now.

### Team-grade control, not more autonomy

**Auto mode is per project.** It was one global boolean: on meant on
everywhere the account could reach. That was written for a single operator who
understood the sandbox, and it does not survive a second account — a new user
handed the same default inherits it for production along with the scratch
project it was meant for. It now has two halves, the operator's intent and the
projects they intended it for, and both must agree before a task skips a
prompt. Turning it on requires naming projects; there is deliberately no "all
projects" option. Accounts that already had it on are scoped once, at startup,
to the projects that exist, so no deployment changes behaviour silently.

**An audit log**, in the same Postgres as tasks and memory, shown on Settings
for admins: who onboarded a project, who approved or rejected a gated command
and what the command was, who approved a merge, who moved auto-approve or
merge review and for whom, who set a GitHub source to Auto, who generated or
deleted a deploy key, and every inbox item that became a task — by a click, by
a signed link (recorded as *signed link*, because that path carries no
session), or by the poller itself (*github-inbox*). Telegram is a notification
channel: best-effort, unordered, and deleted at the whim of whoever owns the
chat. Writing a record never blocks the action it records.

**Inbox Auto is refused for a project whose review gate runs nothing
mechanical**, and if a project's checks disappear later its items are proposed
rather than started. Otherwise "auto" would mean shipping work that a model's
opinion alone had verified.

**Prompt injection has a fixture test.** SECURITY.md already stated the model;
`tests/test_prompt_injection.py` is now the regression test for it. A repo file
tells the agent, in as many words, to disable merge review, read the deploy key
and force-push, and the tests pin what makes that inert — no tool reaches a
control, every named endpoint refuses an unauthenticated call, the container
mounts only the workspace and is handed exactly two environment variables, and
the approval gate is decided from the account's setting before any file is
read. It is not a proof; injection is contained here by what the text cannot
reach.

### One task per project, across processes

"One task per project" was an in-process lock, which made it true only while
exactly one process existed: a second worker, an overlapping restart, or a
script run against the same database would each hold their own lock object and
happily run two tasks on one worktree. It is a Postgres session-level advisory
lock now, and Postgres drops it when the connection closes, so a crashed
process releases its claim with nobody cleaning up.

### Operating it without reading the source

- **Health routes** on all four processes (`/api/health` on the agent,
  `/health` on the two node services, LiteLLM's own), unauthenticated so a
  monitoring box can reach them, `503` when a check fails, and never echoing a
  secret's value. There was no health route at all until now: every restart
  check in this repo's history curled the agent and got the dashboard's
  `index.html` with a 200.
- **`scripts/doctor.py`** — file modes, env completeness, the key pairs that
  must match between the agent and the node services, every project's
  checkouts, the sandbox image, the pm2 processes. It refuses to print
  anything shaped like a secret.
- **Backups** — `scripts/backup.sh` and `scripts/verify_backup_restore.sh`,
  which restores into a scratch database and checks the tables came back. A
  backup nobody has restored is a hypothesis. `docs/backup.md` has the rest.
- **Releases** — `scripts/package_release.sh` builds a tarball with the
  dashboard prebuilt, so installing needs no Node toolchain.
- **[docs/architecture.md](docs/architecture.md)** (the processes, the
  secrets, the three checkouts, the graph), **[docs/runbooks/](docs/runbooks/)**
  (one page per symptom), **[docs/middleware.md](docs/middleware.md)** (what
  each middleware forbids and which agent has it — subagents do not inherit the
  coordinator's chain), and **[docs/playbooks/](docs/playbooks/README.md)**
  (adding a model role, an inbox source or a runtime knob, each starting with a
  test that fails until the wiring is finished). Tests keep all four honest.

### The task stream

A task page opened mid-run could show less than it knew. The stream now
connects the socket **before** hydrating, buffers what arrives during the
fetch, and merges by content-derived entry id with a monotonic sequence number
per task, so nothing is dropped and nothing is duplicated. A dead socket is
detected after 70 seconds of silence and reconnected; past two minutes with a
live socket the page says *no activity* — the opposite diagnosis, and the one
that tells you the quiet is the agent's.

The coordinator also gets a nudge when it stops maintaining the plan it wrote:
a twelve-item task once sat at 0/12 for two hours and snapped to 12/12 at the
end, with nothing broken except that the list was never touched.

### Smaller things

- Auto mode's delete gate asks git whether the target is repo content or the
  agent's own scratch, instead of matching destructive markers. Deleting a
  probe script it just wrote no longer stops the run; deleting anything git
  tracks still does.
- `/api/health` reports how many projects are onboarded, never which. The
  route has no session behind it, and a private repo's name is the one field
  in that payload that describes its owner rather than the process.
- A request arriving before the database pool is up answers `401` or `503`
  rather than `500`.
- CONTRIBUTING's "exactly what CI does" list is now asserted against
  `.github/workflows/ci.yml`, in both directions. It had drifted twice.
- The test suite no longer calls the live router. conftest's placeholder was
  the real router's address, which is a closed port on a contributor's laptop
  and the production router on the machine that runs it.
- A raw NUL byte in `useTaskStream.ts` made git treat the module as binary, so
  every diff of it read "Binary files differ" and no reviewer ever saw it
  change.

## v0.4.0 — a planner that keeps the brief, a Kimi seat for frontend work, costs as the router bills them (pre-release)

**2026-09-10**

Twenty-eight commits on top of v0.3.0. The headline is that a planning
turn no longer loses the request it was given, that frontend work can go
to a different model than everything else, and that the budget guard
counts what the router actually billed.

### Planning that keeps the brief, and searches before it reads

A hard plan on 2026-09-08 spent 68 model calls reading the repo before
writing a line; the context grew to 169k tokens, was compacted, and the
operator's request — the oldest thing in the window — went first. The
planner now works the other way round:

- **The brief comes first and stays pinned.** `save_brief` is the only
  tool a new session can call until a brief exists; the brief rides in
  the system message on every call after that, so compaction cannot touch
  it, and it persists with the session so follow-up turns do not re-force
  it. Saving the brief also matches the request against the project's
  skills and names the architecture skills to read before any file.
- **Search, then read the window a hit points to.** `search_project`
  (ripgrep, capped per file and overall) and `find_files` (the
  `.gitignore`-aware file list) against the real repo. Loop-proofed from
  the start: an identical search is answered from cache and refused on the
  third; zero hits come back with what was scanned and what to change; a
  per-turn search budget ends searching with "write the plan from what you
  have". Both are dials under **Settings → Runtime limits**.
- **The draft gate.** After N repo reads without a saved plan (default 50)
  file reads close with "save a draft now" and reopen once a plan is
  saved. A gate-forced save replies with what comes next — the budget has
  reset, take the open questions, finish the plan — rather than "the user
  can now use Build Now", which one planner read as "done". Paged reads
  return at least 500 lines whatever `limit` asks; eight reads of a
  1,100-line component become two, with no cooperation from the model.
- **What changed lately, and what may be stale.** The cartographer builds
  a model-free `recent-changes` skill (the newest 30 commits with their
  files) and keeps a freshness ledger: a memory fact that cites a file
  which changed after the fact was first seen is flagged, and the flags
  ride into both agents' memory blocks.
- **Pull requests.** With an optional fine-grained `GITHUB_TOKEN` the
  planner and coder can read a pull request — description, checks, review
  comments, diff — host-side and read-only. The sandbox never sees the
  token. "See PR 12 and fix the audit issues" is now a task.

### A frontend seat, pinned to Kimi

The operator wanted Kimi k3 on frontend work and DeepSeek everywhere
else. `agent/frontend_route.py` decides, with a reason: the choice on the
form beats everything; then the classifier's ui-styling category; then any
named backend path or keyword (a migration, a schema, an endpoint) routes
general ahead of any file count; then a two-thirds majority of frontend
paths; then two distinct keywords. Planning sessions decide on their first
message and stay put. On a frontend task the coordinator and the
investigator use `agent-coder-frontend`; the test-writer keeps its own
pin. The New Task form, Build Now and the new-session panel carry an
Auto / Frontend / General selector, and tasks and sessions show a route
badge with the reason on hover — silent routing is how an expensive run
happens. Both frontend seats are managed roles on the Models page.

### Costs as the router bills them

A planning turn was ended at "$8.09 spent against an $8.00 ceiling" when
OpenRouter had billed $1.72: the guard priced calls from token counts
against a rate table that was missing one model's cache-read discount,
and the 4.7x estimate was the last word. Every router row now carries the
proxy's call id and OpenRouter's billed cost; the budget tracker carries a
call at its estimate only until the router's line for that id appears,
and enforces the ceiling against the billed figure. Fallback rates come
from OpenRouter's live catalog, a pin the catalog does not list by name is
resolved through the per-model endpoints, and the table reloads when
`config.yaml` changes under a running agent. The Build Now popup and the
New Task form seed their budget from the Settings default instead of a
hard-coded $2.

### Loops end, and bad history never reaches a provider

- **A repeat-call guard on every tool.** The third identical call whose
  two predecessors matched the same result is answered from cache; the
  fourth and later are refused; after eight refusals in a row the pass
  ends with an escalation naming the looping tool, so the task can resume
  on a different seat instead of paying for forty refusals. A call whose
  result changes (a poll, a flaky test) is never blocked.
- **Malformed tool calls are stripped from every model request.** A
  truncated `write_todos` from one model was serialised as a tool call
  whose arguments were not JSON, and a stricter provider refused every
  later turn of that task — 37 times — while the fallback silently
  planned instead. The checkpoint is untouched; the request is cleaned.

### The gate and the deploy tell code problems from infrastructure

- **Pre-existing failures do not block.** A check that fails on the
  branch is re-run on a worktree at the base commit; one that fails there
  too is marked pre-existing, does not force NEEDS_FIXES, and is listed
  for the agent with an instruction not to chase it.
- **Deploy preflight.** A project can declare URLs that must answer before
  any build step runs. A failure is its own stage and escalates to a
  human with the dependency named, rather than being handed to the agent
  as a compile error. Found the hard way: a storefront prerender that
  reads the catalog from an API which had been dead for days.
- **Resume works without a top-up.** The resume panel only shows the
  budget field when a task is nearly out of money and sends zero
  otherwise; the endpoint rejected zero, so a task that stopped for any
  reason other than cost could not be resumed from the dashboard.

### A passkey gate for a public admin panel

`services/llm-router/auth-gate` puts a WebAuthn passkey in front of the
LiteLLM admin UI when it is on a public hostname: nginx consults it via
`auth_request`, bearer requests pass through for LiteLLM to judge,
everything else needs a session minted by a passkey ceremony. Sessions
are server-side SHA-256 hashes in a 0600 state file; enrolment is a
single-use 30-minute token. Opt-in, and configured entirely from `.env`.

### Fixes

- **Summarization fired before every model call.** The trigger OR'd a
  message-count clause that the token-sized keep window satisfied on its
  own after every compaction. Triggers are tokens-only now, and a test
  refuses any trigger the keep window can satisfy by itself.
- **A compaction could silence the live stream.** Both publishers tracked
  how many messages they had sent; after a compaction the list was
  shorter than that count and nothing was published until it regrew. A
  coder calling every 40 seconds read as "no movement for 20 minutes".
  Messages are tracked by identity, and a quiet tick sends a heartbeat
  the stall watchdog can see.
- **The plan strip snapped back on refresh** to the list frozen when the
  previous pass ended; the live mirror wins now.
- **Build Now was dead for a long plan.** The goal field was capped at
  20,000 characters and a plan came to 20,364; every click was a silent
  422. The cap is 80,000 and a failed start is shown beside the button.
- **The Build Now strip ran off the edge on a phone**, with the confirm
  button unreachable. It wraps.
- **ripgrep is a prerequisite.** CI installs it, the installer warns with
  the package name, and the search tests skip without it.
- Failure rows in the router log carry the exception text; the
  cartographer and both frontend seats have fallbacks.

### Known limits

- The preflight only checks what a project declares; a build step with an
  undeclared live dependency still fails as a build error.
- The half-open-socket gap for task streams noted in v0.3.0 remains.

### Requirements

Linux, Python 3.12+, Node 24+, Docker, PostgreSQL 14+, an OpenRouter API
key, and now **ripgrep** for the planner's search tools. pm2 optional.
**879 Python tests, 173 frontend tests** pass on this release.

## v0.3.0 — runtime limits in the console, one save bar everywhere, planning that stops for the right reasons (pre-release)

**2026-09-02**

Fourteen commits on top of v0.2.0. The headline is that the dials which
used to need a file edit and a restart are now in the console, and that a
planning turn is no longer killed for the crime of taking a while.

### Runtime limits, from the console

Fourteen limits were module constants and environment variables, so
retuning one meant editing a file and restarting — and a restart is
exactly what you cannot do while the thing you want to retune is running.
They live in the store now (**Settings → Runtime limits**, admin only):

    planning turn budget      planning stall timeout    default task budget
    model calls per run       tool calls per run        review wait timeout
    model call timeout        planning model timeout    lint timeout
    typecheck timeout         test suite timeout        review test timeout
    frontend build timeout    default shell timeout

Every one is read at the point of *use*, so a change lands on the next
turn or task and never mutates something already in flight. Values are
clamped to each knob's bounds rather than rejected; unknown names are
rejected, because a typo must not sit in the database looking like
configuration. Existing environment variables still seed the defaults.
Seconds render beside their plain-units equivalent (1200 → "20 min"), and
the label carries the full reasoning as a tooltip.

Worth knowing: `npm test` was capped at 180s, and a repo that chains
dozens of suites can exceed that. An abort reads to the agent as a
*failing* test rather than a slow one, so it would try to fix a suite that
was merely long. That cap is now a dial.

Deliberately not exposed: auto-approve and merge review. Those change what
the agent is *permitted* to do; these are dials on effort.

### One save bar, on both pages that have something to save

Every editable card on the settings page carried its own Save button — a
row of controls disabled almost all of the time, and an edit in a card you
had scrolled past was easy to abandon. There is now one bar, pinned
bottom-right, that appears only when something is genuinely dirty, names
the count, and offers Discard. On a phone it goes full width above the tab
bar rather than floating over it.

The **model configuration page** gets the same flow. Its static
"Save N changes" button sat at the very bottom of three groups of rows, so
a pin changed in the top group was just as easy to lose. Same bar, same
Discard, and a failed save is reported beside the button that caused it
with the edit kept.

The settings page itself was also re-laid: three cards per row where the
grid arithmetic had silently only ever allowed two, Telegram and Projects
paired into one row, spinner buttons gone from numeric fields where they
sat on top of the digits.

### Planning turns that end for the right reasons

- **Bounded by silence, not by the clock.** A planning turn was killed at
  30 minutes while actively streaming — $2.50 spent, no plan saved. A
  duration ceiling selects against exactly the work it should protect.
  The watchdog now fires only when a turn produces *nothing* for 20
  minutes (adjustable above); duration is unbounded on purpose, and the
  budget ceiling remains the only thing that stops work abruptly.
- **Why it stopped is recorded.** The reason used to be a live WebSocket
  event and nothing else — refresh and it was gone. Outcomes are now
  classified (`completed / stopped / stalled / budget / error`), stored
  with the session, and shown when you open one that ended badly, with the
  note that the context is still there and the turn can be continued.
- **The re-read loop.** One session read `src/core/bot.js` 129 times,
  spent its whole $8 ceiling and produced nothing: planning shared the
  build task's summarization window, which was too small to hold the four
  files the question spanned, so it dropped what it had just read and read
  it again. Planning has its own window now, plus a per-turn per-file read
  ledger that warns at 6 reads and refuses at 14.
- **Live cost, and a UI that notices the turn ended.** Cost read $0 for
  the whole turn because it was banked only at the end; it is mirrored as
  it accrues now. And a half-open socket left the browser showing thinking
  bubbles forever — a liveness watchdog treats 70s of silence as death and
  reconnects, with `running` read back from the server.

### Fixes

- **The investigator subagent was never used.** 281 coder calls, 207
  test-writer, zero investigator, across every task since the router
  rework. Its delegation was written as a preference ("prefer the
  investigator for multi-file research") and the coordinator, holding
  `rg` itself, always declined. It is a rule now, with a trigger it can
  evaluate. Prompt-only — worth re-checking the per-role counts.
- **The public demo went down on two 429s a minute apart.** Its model was
  pinned to a single provider with fallbacks off, the one alias in the
  config with no router-level fallback either. Three layers now:
  preferred provider, any other provider of the same model, then a cheap
  proven tool-caller if the model is unavailable everywhere.
- **The router exited at startup instead of degrading** when the database
  was connected, because LiteLLM shells out to the `prisma` CLI to check
  migrations and could not find it outside the venv. The pm2 config puts
  the venv on `PATH`.
- **The mail agent's model spiralled on reasoning.** One "can you see
  starred mail" turn spent 40K reasoning tokens and $0.72 producing
  nothing visible, since reasoning streams as empty chunks. Reasoning
  effort is pinned low for that role: a tool-driven mail agent needs quick
  tool calls, not deep chains.

### Known limits

The half-open-socket gap fixed for planning streams still exists for task
streams; a task's UI can show it running after the socket has died.

### Requirements

Unchanged: Linux, Python 3.12+, Node 24+, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional. **740 Python tests, 168 frontend
tests** pass on this release.

## v0.2.0 — landing page, a frontend test suite, Node 24 (pre-release)

**2026-08-30**

Thirteen commits on top of v0.1.0. The headline is that the login screen
is no longer the front door, and the frontend is no longer untested.

### A public landing page

Anyone arriving from a shared link used to hit a password box for a
console they cannot enter. There is now a real page in front of it —
the pipeline, the review gate, the console, the controls the model cannot
override — built from the README's own copy, so the two cannot drift into
telling different stories. Sign-in moved to the top right; the link to the
source moved the other way, off the login card and onto the page where
someone who *cannot* sign in actually has somewhere to go.

The eight screenshots are the ones already in `docs/`, re-encoded to WebP:
2.4MB of PNG becomes 268KB, content-hashed by Vite so they inherit the
immutable cache year.

The page is indexable now (`noindex` dated from when this URL was nothing
but a password box), with a `meta description` and a `canonical` — the SPA
fallback answers 200 with the same shell for every path, so without one a
crawler can index an unbounded set of URLs that are all the same page.

### The frontend has tests

There was no test framework at all: every behaviour was guarded by nothing
but a typecheck. Adds vitest + Testing Library + jsdom, wired into CI
*ahead* of the build. **141 tests.** Branch coverage is 73% against 45% of
statements, and that gap is deliberate — the decision logic is covered;
the rest is chart and form markup where a test asserts little beyond
"React rendered".

Coverage is reported, not enforced. A threshold that fails CI on an
unrelated refactor teaches people to delete tests.

### Node 24

Node 20 left maintenance in April 2026, and its EOL had started to cost
something concrete: the test toolchain had to be pinned back a major
version each to keep supporting it. CI, the installer's prerequisite check
and the documented requirement all move to **24** together — CI testing
one version while the docs tell people to install another verifies
nothing.

**This is the one upgrade note.** If you are on Node 20, `install.sh` will
now refuse until you upgrade. Nothing else in this release requires action.

Re-verified end to end on the new floor: Debian 13 (Python 3.13.5, Node
24.20.0) against a real postgres:16 — `install.sh` completes, then 694
Python tests and 141 frontend tests pass in that same container.

### Fixes

- **Sidebar categories could not be collapsed while a task was running.**
  Expansion was `manuallyExpanded.has(cat) || searching || selectedCategory
  === cat`, a shape that can only ever *add* expansion — so clicking the
  header did nothing, with no indication why. Underneath it,
  `selectedCategory` resolved the category of a *running* task, which is
  filtered out of the category lists and shown in the Running group
  instead: selecting one opened a category it is not a member of.
- **A signed-out visitor was sent past the landing page to the login
  form**, because the opening `getMe()` 401 fired the session-expiry
  handler. An expired session still goes straight to the form.
- **The landing page's nav was declared sticky and never stuck.** The page
  carried `overflow-x: hidden` to stop horizontal scroll; setting one axis
  to `hidden` computes the other to `auto`, which makes the element a
  scroll container — and a scroll-container ancestor silently disables
  `position: sticky` on everything inside it. `overflow-x: clip` clips the
  same overflow without establishing one.
- **BalanceStrip crashed on a 200 with an unexpected body.** `!balance`
  passed for `{}`, then `.toFixed` threw — and it renders inside the
  Sidebar, so the ErrorBoundary blanked the whole console over a
  decorative credit strip.
- **`HEAD` returned 405 on every file at the dist root.** FastAPI's
  `@app.get` registers exactly the methods named, unlike Starlette's plain
  `Route`, which folds `HEAD` in — so a crawler sizing an `og:image`
  before fetching it got a 405.
- **Those files were served with `max-age=86400` and no way to
  revalidate.** They are `no-cache` now, with conditional requests
  answered properly: `FileResponse` sets etag and last-modified but never
  checks them, and only populates them when handed a `stat_result`, so
  both had to be wired up for a revalidation to cost a 304 rather than the
  whole file.
- A malformed WebSocket frame threw a bare `SyntaxError` out of
  `onmessage`. The stream survived either way, but the log said nothing
  about which socket produced it.
- The agent dashboard had `og:title` and `og:description` but no
  `og:image`, so its link preview was a `summary_large_image` with nothing
  to show.

### Requirements

Linux, Python 3.12+, **Node 24+**, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional.

## v0.1.0 — first public release (pre-release)

**2026-08-29**

The first public cut of 3D-Agent. It has run continuously on one deployment for
several months against three real repositories — a live trading bot, an
e-commerce monorepo and a Next.js site — but this is the first time anyone else
can install it. Treat it accordingly: see *Known limits* below.

### What it does

Give it a plain-English goal against a repository you've onboarded. It plans the
work, writes the code, runs that project's **real** test suite, and ships it —
with an independent review gate that must pass before anything merges.

- **Plan → build → verify → review → ship.** Every task runs in a git worktree
  of your live repo, on its own branch, inside a Docker container that can see
  only that worktree.
- **A second model reviews every diff** in an isolated checkout before a merge
  is possible, and optionally a human approves after that. Nothing merges on
  the agent's say-so.
- **Planning Chat** — a separate conversational mode for research and design
  that remembers what it learns and can hand a finished plan to the build
  pipeline.
- **Memory that compounds.** Completed tasks are consolidated into per-project
  memory; a cartographer keeps a structural map of each codebase current.
- **Budget ceilings per task**, so a runaway loop costs a known maximum.
- **Model routing by role.** Planner, coder, reviewer, summarizer and the rest
  are named aliases you repin from the dashboard, with live pricing, agentic
  benchmark standing and per-provider latency/uptime shown at the point of
  choice.

### Setting it up

- **`./install.sh`** takes a fresh clone to a running agent. It checks
  prerequisites, generates secrets in the formats the app actually requires,
  creates the database, builds the sandbox image and the dashboard, and is safe
  to re-run — which is also the upgrade path. `--dry-run` and `--yes` included.
- **Project onboarding from the dashboard** (Settings → Projects) or
  `scripts/add_project.py`. It inspects a directory, proposes a configuration,
  and asks you to confirm it.
- **[INSTALL.md](INSTALL.md)** is the full guide: requirements, configuration
  reference, troubleshooting for the real failure modes, and the update path.

### The safety posture, stated plainly

This runs a model that writes and executes code against your repositories. The
controls are the product, not an afterthought:

- Onboarding **proposes, you approve**. Anything that can't be verified arrives
  switched off with the reason attached. A test script that makes network calls
  is disabled by default — a suite that talks to a live service can *act* on
  production, and no static analysis distinguishes "hits a test server" from
  "hits your production system".
- Projects can only be onboarded from inside `AGENT_PROJECT_ROOTS`, judged after
  symlink resolution. The worktree location and project name are derived by the
  server, never accepted from a request.
- Check and build commands are matched against what the server itself proposed,
  so a client cannot introduce a command for the review or deploy service to
  run.
- Each project pushes with its **own deploy key**, scoped to one repository,
  wired to that repo's `core.sshCommand` alone.
- The agent cannot `git push` — that's on its blocked-command list.

See [SECURITY.md](SECURITY.md) for the full threat model, including which
capabilities are intended and which would be genuine vulnerabilities.

### Hardening before release

A full external review of the codebase ran before this tag; everything it
found at critical or high severity is fixed here, with a regression test each.
Two were serious enough to be worth naming:

- **Host code execution via git hooks.** Every git command runs on the host
  with its working directory inside the agent-writable worktree, and a
  worktree's `.git` is a pointer *file* living in that same writable tree.
  Rewriting it re-aimed git at an agent-controlled directory, hooks included,
  so the post-build commit executed agent-authored code as the server user.
  Hooks are now disabled on every invocation and the pointer is verified
  against server-owned config.
- **Sandbox mount escape.** The container's bind mounts were computed from
  state read out of the worktree — a `node_modules` symlink and the `.git`
  pointer — so the agent could choose what the host mounted into its own
  container (`ln -s /home node_modules` produced `-v /home:/home:ro`). Mount
  targets are now validated against the project's own paths.

Also fixed: alerts that ignored per-user repo scoping, a fail-open in user
creation, streams that dropped data on an unclean reconnect, three
"documented but broken" issues that would have hit the first fresh install,
and the absence of CI.

### Known limits

- **Pre-1.0, and field-tested on exactly one machine.** Expect rough edges on a
  different distro, Postgres version, or non-root install.
- **Linux host required.** The sandbox is Docker; there is no Windows path.
- **Remote access needs HTTPS or an SSH tunnel.** The session cookie is
  `Secure`, so plain HTTP works only on `localhost`/`127.0.0.1`. A LAN or VPN
  address over plain HTTP will drop the cookie and bounce you back to the login
  page. `install.sh` can set up nginx + Let's Encrypt for a domain.
- **Stack detection covers npm/pnpm/yarn and basic Python.** Go, Rust and Ruby
  projects onboard fine but arrive with no checks detected — you add commands by
  hand.
- **The sandbox image ships Node and Python only.** A project needing another
  toolchain needs the image extended.
- **No migrations story yet.** Upgrades are `git pull` plus `./install.sh`; the
  schema is created on first start and has not needed a migration path so far.
- **Costs real money.** Every task calls a paid model. Set
  `DEFAULT_BUDGET_USD` deliberately.

### Requirements

Linux, Python 3.12+, Node 20+, Docker, PostgreSQL 14+, and an OpenRouter API key
(the only paid dependency). pm2 optional.

### License

PolyForm Noncommercial 1.0.0 — source-available, not open source. Free for any
noncommercial use; commercial use needs a separate licence.
