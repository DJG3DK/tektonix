# Installing Tektonix

Tektonix is self-hosted: it runs on a machine you control, works on repos on
that machine, and talks to exactly one paid service (OpenRouter). This guide
takes about 15 minutes, most of it waiting for dependencies.

> **Clone it, don't fork it.** Forking is for sending changes back upstream. To
> *run* Tektonix, clone it — every file that is specific to your deployment
> (`.env`, `projects.json`, `skills/local/`, the service overrides) is
> gitignored, so `git pull` brings you updates without ever touching your
> configuration.

---

## 1. What you need

| Requirement | Why |
|---|---|
| **Linux host** you control | The agent runs shell commands and manages git worktrees on this box |
| **Python 3.12+** | The agent itself |
| **Node 24+** | The dashboard build and the two review services |
| **ripgrep** (`rg`) | The planner's repo search (`apt install ripgrep`, `pacman -S ripgrep`, `dnf install ripgrep`); `install.sh` installs it where it can |
| **Docker** | Every command the agent runs happens inside a container. Without it, the first tool call of the first task fails |
| **PostgreSQL 14+** | Conversation checkpoints, memory, users. **Required** — without it the agent retries its connection pool forever and never starts serving. On Arch/CachyOS you must also `initdb` before first start; Debian/Ubuntu do that for you |
| **An OpenRouter API key** | The only paid dependency — [openrouter.ai/keys](https://openrouter.ai/keys) |
| **An SMTP account** *(optional)* | Only for password-reset codes. Skip it and reset via the database instead — see [§6a](#6a-email-smtp) |
| **pm2** *(optional)* | To run it as a managed service rather than in a terminal |
| **nginx + certbot** *(optional)* | Only if you want browser access on a public domain — see [§3a](#3a-reaching-it-from-another-machine). An SSH tunnel needs neither |

A small VPS is enough. The agent is not compute-heavy; the models run
elsewhere.

Verified on clean containers of **Debian 13** (Python 3.13, Node 24) and
**Arch** (Python 3.14, Node 26): `install.sh` completes and the full test
suite passes on both, against a real Postgres. `install.sh` detects `apt`,
`pacman` or `dnf`, and writes its nginx config to `sites-available` or
`conf.d` depending on the distro's layout.

Windows, macOS, or a one-command Docker install is a different path:
[docker/README.md](docker/README.md) and `install.ps1`. That bundle does
not include the review services yet.

---

## 2. Install

```bash
git clone https://github.com/DJG3DK/tektonix.git
cd tektonix
./install.sh
```

The installer asks three questions — your Postgres DSN, your OpenRouter key,
and an admin email — and derives everything else. It will:

- check every prerequisite, offer to install any that are missing, and stop
  with a specific message if one is still absent. Nothing is installed without
  being asked, and the answer defaults to no; `--yes` alone does not authorise
  it, because an unattended run should not quietly add a Node runtime and a
  database to a machine. Set `INSTALL_PREREQS=1` when that is what you want.
- generate `AUTH_SECRET_KEY` and a router master key **in the correct format**
  (see the footgun in §6)
- write `.env` and `services/model-router/.env` with `600` permissions
- create the database if it doesn't exist
- build both Python environments
- build the sandbox container image (includes headless Chromium, so the agent
  can look at frontends it builds)
- build the dashboard

It is safe to re-run: every step detects what already exists, and it never
overwrites an existing `.env` or `projects.json`.

```bash
./install.sh --dry-run     # show every action, change nothing
./install.sh --yes         # unattended; reads PG_DSN / OPENROUTER_API_KEY from the environment
```

---

## 3. Start it

```bash
# the model router first — everything resolves model aliases through it
services/model-router/venv/bin/uvicorn router.app:app --host 127.0.0.1 --port 4001

# then the agent (serves the dashboard itself; there is no separate frontend process)
.venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8100
```

Or under pm2:

```bash
pm2 start ecosystem.config.js
pm2 start services/model-router/ecosystem.config.js
pm2 save
```

Open **http://127.0.0.1:8100**.

> **The first admin password is printed once, to the server log, on first
> startup.** Capture it. If you miss it, delete the row from the `agent_users`
> table and restart to re-seed.

Admin accounts are required to set up TOTP 2FA on first login.

### 3a. Reaching it from another machine

By default the agent binds **`127.0.0.1:8100`** and is not reachable from
outside the host.

**The session cookie is issued with the `Secure` flag**, which makes this
choice binary rather than a matter of taste. Browsers only send a `Secure`
cookie over HTTPS — with one exception: `localhost` and `127.0.0.1` count as
trustworthy origins, so plain HTTP works there. Verified in Chromium:

| How you reach it | Result |
|---|---|
| `http://127.0.0.1:8100` (SSH tunnel) | works — cookie accepted |
| `https://your-domain` | works |
| `http://<LAN or VPN address>:8100` | **broken** — cookie silently dropped |

That last row is the trap. Logging in over a plain-HTTP LAN or Tailscale
address *appears* to work: the request succeeds, the browser discards the
cookie, and the next request bounces you back to the login page with no error
in the UI or the log. Don't run it that way.

So there are two supported options.

**1. SSH tunnel — nothing to configure, nothing exposed.**

```bash
ssh -L 8100:127.0.0.1:8100 you@your-server
```

Then open `http://127.0.0.1:8100` on your own machine. No domain, no
certificate, no open ports, and the console stays invisible to the internet.
For a single operator this is the right answer.

**2. A domain with HTTPS.** Needed for access from anywhere, from a phone, or
for more than one person. `install.sh` sets it up:

```bash
AGENT_DOMAIN=agent.example.com ./install.sh
```

or answer the **Remote access** prompt when running interactively. It will:

- check the domain resolves, and resolves *to this host* — asking for a
  certificate for a domain pointing elsewhere burns a Let's Encrypt rate-limit
  slot and fails with a confusing message about challenge validation;
- install nginx and certbot if missing;
- write `/etc/nginx/sites-available/3d-agent` with the settings this app needs
  (below);
- run `certbot --nginx`, which adds the TLS block, the HTTP→HTTPS redirect and
  an automatic renewal timer.

Re-running is safe — an existing vhost or certificate is left alone.

**Prerequisites:** a domain with an **A record pointing at this host**, and
ports **80 and 443** reachable. Port 80 must stay open after setup; renewals
use it.

#### What the proxy config has to get right

Two settings are not optional, and both fail in ways that look like an
application bug rather than a proxy problem:

- **WebSocket upgrade headers.** The dashboard streams task and planning output
  over WebSockets. Without `proxy_set_header Upgrade $http_upgrade;` and
  `proxy_set_header Connection "upgrade";`, the page loads normally and then
  never shows live output — which reads as "the agent is stuck".
- **Long read/send timeouts (`1800s`).** A planning turn or a build step can run
  for many minutes with no bytes crossing the connection. nginx's 60-second
  default kills those mid-run, and the task dies with nothing explaining why.
- **`client_max_body_size 64m`.** nginx caps request bodies at 1MB by default
  and rejects anything larger with its own 413 page, before the request reaches
  the app — so attaching a screenshot fails with "upload failed: 413" while the
  app's real limit (25MB per file) is never consulted.

One more, for correctness: use
`proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`. That variable
appends the real peer **last**, which is the hop the app's rate limiter reads.
A proxy that passes the client's own `X-Forwarded-For` through untouched lets a
caller spoof past the login rate limit.

#### If you publish it on a domain

The app has its own login, TOTP 2FA for admins, and auth rate limiting — but it
was built to sit behind something, not to *be* the perimeter. Add at least one
of:

- an nginx IP allowlist (`allow 203.0.113.4; deny all;`) if your address is
  stable;
- an identity proxy in front (Cloudflare Access, oauth2-proxy, `auth_basic`);
- fail2ban watching the nginx access log.

And keep `AGENT_PROJECT_ROOTS` narrow — see
[§5](#5-read-this-before-onboarding-a-project).

---

## 4. Add a project

The agent needs at least one project to work in. Either:

- **Dashboard** — Settings → Projects → enter an absolute path → review what
  was detected → Create.
- **CLI** — `.venv/bin/python scripts/add_project.py /path/to/your/repo`
  (add `--yes` for an unattended install).
- **New project** — when there is no repository yet. On the planner's
  "Plan a project" form, choose **New project…** in the Repo dropdown
  (admins only), give it a name and, if you have stored a GitHub token
  (§6b), tick **Create a private GitHub repo**. Headless:
  `.venv/bin/python scripts/new_project.py my-service --github`, which reads
  the token from `GITHUB_TOKEN` (or the variable named by `--token-env`),
  never from an argument.

The first two inspect the directory, propose a configuration, and let you
approve it. Read §5 before clicking through it. The third makes the
directory under the first `AGENT_PROJECT_ROOTS` entry, runs `git init` with
one commit on `main`, and provisions it with the recommended answers — there
is nothing to approve yet, because an empty repository has nothing to
detect. Its checks arrive on their own after its first merge (§9, *Checks
never run*).

Onboarding creates a **git worktree** of your repo under
`AGENT_SANDBOX_ROOT`. The agent works there on a per-task branch and never
commits to your working checkout.

### Push access (deploy keys)

The agent itself never pushes — `git push` is on its blocked-command list.
After you approve a merge, the review service pushes from your project's live
checkout, and that push is **best-effort**: if it cannot authenticate, the
merge and the deploy still succeed and your remote quietly stays behind.

So each project gets its own **deploy key** — an SSH key scoped to one
repository rather than your whole account. Expand a project under
Settings → Projects and use **Push access**:

1. **Generate key** — the server mints an ed25519 keypair. You never handle
   the private half.
2. Copy the public key it shows you.
3. On GitHub: repo → **Settings** → **Deploy keys** → **Add deploy key**,
   paste it, and tick **Allow write access**. Without that box the key can
   read but every push is rejected.
4. **Test connection** — this runs `git ls-remote` exactly as the push will,
   so a green result means the push will authenticate.

You can paste an existing private key instead. It must have **no passphrase**,
since nothing can type one during an unattended push; a passphrase-protected
key is rejected at paste time rather than failing at merge time.

A project created with **Create a private GitHub repo** ticked arrives with
all of this already done: the key is minted, registered on the new
repository with write access, and `origin` is the SSH URL. That first push
of `main` is made by the API process on the host, on your request — not by
an agent, which still cannot push.

Keys are stored under `keys/` with `0600` permissions (gitignored), and each
one is wired to a single repo via that repo's own `core.sshCommand` — so one
project's key never signs another's git operations. No endpoint ever returns a
private key.

If your `origin` is an **HTTPS** URL, an SSH deploy key cannot authenticate
it; switch the remote to SSH or configure a credential helper on the host. The
panel tells you which case you're in.

---

## 5. Read this before onboarding a project

The review step is a safety gate, not a formality.

**Detection proposes; you decide.** Anything the installer cannot verify
arrives switched **off**, with the reason attached. The important case is test
scripts that make network calls. A test suite that talks to a live service can
*act* on production — the deployment this was built on had a `test:routes`
script that POSTed real trade orders at a running bot. No static analysis can
tell "hits a test server" apart from "hits your production system", so those
scripts are flagged and disabled, and you enable them only after reading them.

If your repo defines a `test:review` script naming the suites that are safe in
a detached checkout, that is trusted over the aggregate `test` script.

**Containment.** Projects may only be onboarded from inside
`AGENT_PROJECT_ROOTS` (set in `.env`; the code's own default is `/home`, the
parent of every home directory — `install.sh` writes your home instead), judged
after symlink resolution. Keep it as narrow as your layout allows: onboarding
grants an agent write access to what it points at.

---

## 6. Configuration reference

Everything lives in `.env` (see `.env.example` for the full list).

| Variable | Notes |
|---|---|
| `LANGGRAPH_PG_DSN` | Postgres DSN for checkpoints, memory and users |
| `MODEL_ROUTER_URL` / `MODEL_ROUTER_KEY` | The router. `MODEL_ROUTER_KEY` **must equal** `MODEL_ROUTER_KEY` in `services/model-router/.env` |
| `AUTH_SECRET_KEY` | Encrypts TOTP secrets at rest. **Must decode to 16, 24 or 32 raw bytes.** `openssl rand -hex 32` produces 48 bytes and will *not* work — use `python -c "import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"`. Rotating it locks out every 2FA user permanently |
| `AGENT_PROJECT_ROOTS` | Colon-separated roots projects may be onboarded from |
| `AGENT_SANDBOX_ROOT` | Where per-project worktrees are created |
| `DEFAULT_BUDGET_USD` | Seeds the default per-task spend ceiling (live dial: Settings → Runtime limits) |
| `PLANNING_TURN_BUDGET_USD` | Seeds the per-turn ceiling for planning chat (default `4.0`; live dial: Settings → Runtime limits) |
| `REVIEW_CONTROL_SECRET` | Authorises merge/deploy calls from the agent to the review service. Generated by `install.sh` into **both** `.env` and `services/shared/.env` — the two sides read it from different files, and they must match. Unset means every merge is rejected |
| `GITHUB_TOKEN` | Optional. Fallback GitHub token for the PR tools and the GitHub inbox; the dashboard's **Settings → GitHub** stores per-project tokens encrypted and is the preferred place. See [§6b](#6b-github-optional) |
| `SMTP_HOST` / `PORT` / `USER` / `PASS` / `FROM` | Outbound mail for password-reset codes. Sending is optional — **the keys are not**. See [§6a](#6a-email-smtp) |

### 6c. Where every secret lives

Six files, one database, two directories. Nothing is duplicated except the two
pairs that must agree, and those are marked.

```
3d-agent/
├── .env                                   the AGENT's own secrets            600
│     LANGGRAPH_PG_DSN   AUTH_SECRET_KEY   SMTP_*   ADMIN_EMAIL
│     MODEL_ROUTER_KEY ─────────────────────────┐  must match ──┐
│     REVIEW_CONTROL_SECRET ───────────┐       │               │
│     GITHUB_TOKEN (optional fallback) │       │               │
│                                      │       │               │
├── services/                          │       │               │
│   ├── model-router/.env                │       │               │   600
│   │     OPENROUTER_API_KEY           │       │               │
│   │     MODEL_ROUTER_KEY ──────────┼───────┘               │
│   │     GATE_RP_ID / GATE_ORIGIN     │   (the passkey gate)  │
│   │                                  │                       │
│   ├── shared/.env                    │                       │   600
│   │     REVIEW_CONTROL_SECRET ───────┘  read by both Node services
│   │
│   └── commit-reviewer/review-secrets/<project>/…    700
│         copies of each project's own secret files, so its
│         checks can run inside a review worktree
│
├── keys/<project>.key                 per-project deploy keys  600 (dir 700)
│     also referenced from ~/.ssh/config as a host alias
│
├── projects.json                      NOT a secret: paths, checks, build steps
│
└── Postgres (LANGGRAPH_PG_DSN)
      GitHub inbox tokens, TOTP secrets, recovery codes
      — encrypted with AUTH_SECRET_KEY, so the database alone is not enough
```

**Check it rather than trusting it.** `scripts/doctor.py` verifies presence,
file modes, that `AUTH_SECRET_KEY` decodes to a usable length, and that both
pairs above actually match — comparing them by hash, never by printing them:

```bash
.venv/bin/python scripts/doctor.py            # everything
.venv/bin/python scripts/doctor.py --quiet    # only problems; exit 1 if any failed
```

It also checks each project's paths, the sandbox image, the built dashboard
and which pm2 apps are online. Run it after any change to configuration, and
after an upgrade.

## 6aa. Phone notifications (nothing to configure)

The dashboard installs as an app — Chrome on Android offers **Install app**,
iOS Safari's Share menu has **Add to Home Screen** — and the installed app can
receive push notifications: the same alerts Telegram carries, scoped to the
projects an account can see.

There is no setup step. The signing keypair (VAPID) is generated on first use
into `keys/vapid.json` and never needs rotating. Each person turns it on per
device under **Settings → Notifications**, because notification permission
belongs to a browser rather than to an account, and there is a **Send a test**
button for exactly the reason a silent failure here is invisible.

Two things to know:

* **iOS only delivers push to an app installed to the Home Screen** (16.4+).
  In an ordinary Safari tab the buttons would appear to work and nothing would
  ever arrive, so the panel detects that case and says so instead.
* **`keys/vapid.json` must be backed up with the database.** Every subscription
  was issued against it; restoring onto a box with a different one stops every
  notification with no error anywhere. See `docs/backup.md`.

Colour schemes live next door under **Settings → Appearance** — five of them,
saved per account, with a preview before you apply.

---

## 6a. Email (SMTP)

Email is used for **password-reset codes** and, if you switch it on under Settings → GitHub,
for the GitHub inbox's approve links. It is not used for task alerts (Telegram covers those), and
not for the first admin password — that is printed once to the server log on first startup.

**Dependency:** [`aiosmtplib`](https://pypi.org/project/aiosmtplib/), pinned in
`requirements.txt` and installed by `install.sh`. There is no system package to
install and no local mail server to run — the app talks SMTP directly to
whatever provider you point it at.

**The keys are required even if you never send email.** This trips people up,
so to be explicit — `agent/config.py` reads all five with `os.environ[...]`:

| `.env` state | Result at startup |
|---|---|
| Keys absent entirely | `KeyError: 'SMTP_HOST'` — the app will not start |
| `SMTP_PORT=` (blank) | `ValueError: invalid literal for int() with base 10: ''` |
| `SMTP_PORT=587`, the rest blank | Starts fine; email simply never sends |

`install.sh` writes the third form for you. If you're editing `.env` by hand
and don't want email, keep all five lines and leave everything except
`SMTP_PORT` empty.

**Provider requirements**

- **STARTTLS on port 587.** The mailer calls `aiosmtplib.send(..., start_tls=True)`,
  so implicit-TLS submission on port 465 will not work.
- **Use an app password, not your account password.** Gmail, Outlook and most
  providers reject the account password outright once 2FA is on.
- **Proton Mail** needs one of two setups, depending on your plan. Business
  plans can submit directly to `smtp.protonmail.ch:587` using an **SMTP token**
  generated in the admin panel (not your login password). Individual plans have
  no direct SMTP endpoint at all — you run **Proton Mail Bridge** on the same
  host and point the app at Bridge's local listener instead (typically
  `127.0.0.1`, port `1025`), which means Bridge has to be running for resets to
  work.

**Misconfiguration fails quietly, by design.** `POST /api/auth/forgot-password`
always returns `{"ok": true}`, whether or not the address belongs to a real
account — otherwise the response would tell an attacker which emails are
registered. A broken SMTP config lands in the same bucket: the user sees a
normal "check your email", and the real error goes to the server log. If a
reset code never arrives, look there for `password reset email failed to send`.

**Running without email at all** is fine, with one consequence: there is no
self-service password reset. If you lock yourself out, recover on the host —
delete the row from `agent_users` in Postgres and restart, and the app re-seeds
an admin account and prints a fresh password to the log.

**Files that are yours, not the project's** (all gitignored — they survive
`git pull`):

| Path | What it holds |
|---|---|
| `.env` | The agent's secrets |
| `services/model-router/.env` | The router's credentials (OpenRouter key, master key) |
| `services/shared/.env` | `REVIEW_CONTROL_SECRET` for the two Node services |
| `keys/` | Per-project deploy keys (mode 700) |
| `services/commit-reviewer/review-secrets/` | Copies of each project's secret files, so its checks can run |
| `projects.json` | Your projects (written by the wizard) |
| `services/model-router/config.yaml` | Your model pins (written by Settings → Models; seeded once from `config.example.yaml`) |
| `skills/local/` | Your own domain knowledge — see `skills/local/README.md` |
| `services/*/builtin-projects.local.js` | Optional review/deploy overrides — see the `.example` files |
| `memory/*.md` | Per-project memory the agent maintains |
| `backups/` | Database dumps from `scripts/backup.sh` — [docs/backup.md](docs/backup.md) |
| `frontend/dist/` | The built dashboard (shipped prebuilt in a release tarball) |

The full picture, including which of these must agree with each other, is
[§6c](#6c-where-every-secret-lives); `scripts/doctor.py` checks it.

---

### 6b. GitHub (optional)

Two things use GitHub: the planner and coder can **read pull requests** ("see PR 12 and fix the
audit issues"), and the **GitHub inbox** can pick up Dependabot PRs, security alerts, code
scanning (CodeQL) alerts, review comments and failing checks and turn them into tasks
(README → *GitHub inbox*).

1. Create a **fine-grained personal access token** on GitHub: *Settings → Developer settings →
   Personal access tokens → Fine-grained tokens*. Select the repositories the agent manages.
   Repository permissions:

   | permission | needed for |
   |---|---|
   | Metadata (read) | always |
   | Pull requests (read) | the PR tools, Dependabot PRs, review comments |
   | Contents (read) | reading the diff |
   | Dependabot alerts (read) | the security-alerts source |
   | Code scanning alerts (read) | the code-scanning (CodeQL) source |
   | Actions (read) *or* Checks (read) | the failing-checks source |
   | Administration (write) | creating a repository and registering its deploy key from **New project…** (a classic token needs the `repo` scope) |

   A fine-grained token reaches only the repositories chosen when it was made, and a
   repository that does not exist yet cannot be chosen — so a token meant for **New
   project…** needs *All repositories*, or the deploy-key step fails after the repository
   is created.

   Dependabot alerts must also be **enabled on the repository** (repo → Settings → Code security).
2. In the dashboard: **Settings → GitHub → Add token**. Give it a name, paste it, press **Test** —
   it reports who the token is and, per project, whether it can read alerts, code scanning and checks. Save.
   The token is stored encrypted with `AUTH_SECRET_KEY` and never shown again.
3. Set **Dashboard URL for links** to the address you open the dashboard at (for example
   `https://agent.example.com/v2`) so Telegram and email alerts carry clickable approve links.
4. Per project, set each source to **Propose** (approve link, nothing starts by itself) or
   **Auto** (starts within the project's cap). Either way the task goes through the review gate
   and your merge approval. Press **Poll now** to see the first items land in the **GitHub** tab.

The agent's project-to-repository mapping is read from each checkout's `origin` remote; SSH host
aliases from deploy keys (`git@github-myproject:owner/repo.git`) are understood. `GITHUB_TOKEN` in
`.env` remains as a fallback for projects without a stored token; you do not need it once tokens
live in Settings.

## 7. Optional: the review gate

The agent can ship code on its own. The review gate makes it prove itself
first: an independent model reviews every commit in an isolated worktree, runs
the project's real checks, and nothing merges until it passes.

```bash
node services/commit-reviewer/reviewer.js      # zero npm dependencies
cd services/agent-review && npm install && node server.js
```

Projects onboarded through the wizard are picked up automatically. See
`services/commit-reviewer/README.md`.

The review dashboard is served at `/_review/` on the same host as the
console. `install.sh` writes that location into a **new** nginx vhost and
injects `X-Review-Secret` there — the browser never holds the value, and
without the header "Check now", merge and restart all 401. If you already
have a vhost, add the location by hand:

```nginx
location /_review/ {
    proxy_pass         http://127.0.0.1:4100/;
    proxy_set_header   X-Review-Secret <the value in services/shared/.env>;
    proxy_set_header   Host $host;
    proxy_set_header   X-Forwarded-Proto $scheme;
}
```

**Both sides need `REVIEW_CONTROL_SECRET`,** and they read it from different
files: the agent from its own `.env`, the two Node services from
`services/shared/.env`. `install.sh` generates one value into both. If you set
it up by hand and they disagree — or it's missing — the build runs, the review
passes, and then the merge is rejected, after you've paid for the whole task.
The agent logs a warning at startup when it's unset.

It used to live in `services/model-router/.env`, because that file already
existed and both services already read it for the OpenRouter key. That made
the model proxy's config a secrets bus. The services still fall back to the
old path with a warning, so an upgrade without re-running the installer keeps
working; move the line to `services/shared/.env` (mode 600) and drop it from
the router's file.

---

## 8. Choosing models

Everything routes through named aliases (`agent-coder`, `agent-planner`,
`agent-reviewer`, …) defined in `services/model-router/config.yaml`.

That file is **yours**, not the repo's. `install.sh` copies it from
`config.example.yaml` on a fresh install and never touches it again, and it is
gitignored — the Models page rewrites it on every repin, so tracking it would
turn each model change into a diff and let an upgrade overwrite the pins you
chose. The example's pins are one deployment's answers on one day, not
recommendations: expect to change them, and keep your copy with your backups
(it is not in git to restore from).

Change them from **Settings → Models** in the dashboard, which shows each
model's price, agentic-arena standing and knowledge cutoff, and each provider's
latency, uptime and caching support. Model changes take effect at the next
router restart.

---

## 9. Troubleshooting

Each process answers a local health check that costs nothing and makes no
model call — start there:

```bash
curl -s 127.0.0.1:8100/api/health | python3 -m json.tool   # agent
curl -s 127.0.0.1:4100/health     | python3 -m json.tool   # merge + deploy
curl -s 127.0.0.1:4101/health     | python3 -m json.tool   # reviewer
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:4001/health/liveliness
```

Each returns 503 and names the failing dependency. For a specific symptom, see
[docs/runbooks/](docs/runbooks/).


**The first tool call of the first task fails.** The sandbox image isn't
built: `docker build -t tektonix-sandbox:latest docker/agent-sandbox/`

**Every model call 401s.** `MODEL_ROUTER_KEY` in `.env` doesn't match
`MODEL_ROUTER_KEY` in `services/model-router/.env`.

**The app won't start, complaining about the auth key.** `AUTH_SECRET_KEY`
doesn't decode to 16/24/32 raw bytes — see §6.

**The dashboard is blank.** The bundle wasn't built:
`cd frontend && npm ci && npm run build`, then restart the backend.

**Behind a reverse proxy: the page loads but live output never appears.** The
proxy is dropping the WebSocket upgrade. Add `proxy_set_header Upgrade
$http_upgrade;` and `proxy_set_header Connection "upgrade";` — see
[§3a](#3a-reaching-it-from-another-machine).

**"upload failed: 413" when attaching a file.** The proxy is rejecting the
body, not the app. Add `client_max_body_size 64m;` to the proxy location and
reload. You can tell which layer refused it: nginx returns an HTML error page,
the app returns JSON.

**Uploads then fail with 500 instead.** Raising the size limit can expose a
second problem underneath it: nginx buffers request bodies larger than
`client_body_buffer_size` to disk, and if its temp directory is not writable by
the worker user the request dies with a 500. Check the error log for
`open() "/var/lib/nginx/body/..." failed (13: Permission denied)`, then give
the worker user ownership — on Debian/Ubuntu:

```bash
sudo chown -R www-data:www-data /var/lib/nginx/body /var/lib/nginx/proxy
sudo systemctl reload nginx
```

(`grep ^user /etc/nginx/nginx.conf` tells you which user to use.)

**Behind a reverse proxy: long tasks die partway with no error.** The proxy's
read timeout is cutting an idle-but-live connection. nginx defaults to 60s;
this needs `proxy_read_timeout 1800s;`.

**A project won't onboard: "outside the configured project roots."** Its path
isn't under `AGENT_PROJECT_ROOTS`. Widen it deliberately, or move the repo.

**Notifications never arrive.** Check **Settings → Notifications** says *On for
this device* and press **Send a test**. If it reports the server could not
deliver, the problem is server-side rather than the phone's permission: look
for a line starting `push:` in the agent's log (`pm2 logs tektonix`). On iOS,
confirm you opened the app from the Home Screen icon and not from a Safari tab.

**Checks never run for a project.** Detection found no `typecheck`/`lint`/
`test` scripts, or the only test script was flagged as network-touching and
left disabled. Check Settings → Projects. A project created from **New
project…** starts with none, since there was nothing to detect; detection
re-runs on its own after the project's first merge lands and writes what it
finds into `projects.json` — the task log says which checks it wrote, or that
none are detectable yet. It only fills an empty list, so checks you set by
hand are never replaced.

**The app won't start with a `KeyError` or `ValueError` about `SMTP_*`.** All
five SMTP keys must be present in `.env` even when email is unused, and
`SMTP_PORT` must be a number. See [§6a](#6a-email-smtp).

**A password-reset code never arrives.** The endpoint returns success even when
sending fails, deliberately — check the server log for `password reset email
failed to send`. Usual causes: an account password where an app password or
token is required, or port 465 instead of 587.

---

## 9a. Backups

The database holds every task, plan, memory, account and encrypted GitHub
token. Set this up on day one, not after the first loss:

```bash
./scripts/backup.sh                       # writes backups/agent-<stamp>.dump
./scripts/verify_backup_restore.sh        # restores it into a scratch DB and checks it
```

Nightly:

```
30 3 * * * /home/3d-agent/scripts/backup.sh >> /home/3d-agent/data/backup.log 2>&1
```

Keep `.env` with the dump — `AUTH_SECRET_KEY` is what decrypts the 2FA secrets
and the stored GitHub tokens, and a restore without it locks every user out.
Full procedure, including what is *not* in the dump: [docs/backup.md](docs/backup.md).

## 9b. Installing from a release tarball

A git clone has no built dashboard (`frontend/dist` is generated, not
committed), so `install.sh` builds it, which needs Node on the server. A
release tarball ships it already built:

```bash
scripts/package_release.sh v0.5.0        # on a machine with Node
# -> dist/tektonix-v0.5.0.tar.gz  +  .sha256
```

On the server:

```bash
tar -xzf tektonix-v0.5.0.tar.gz && cd tektonix-v0.5.0
./install.sh          # finds frontend/dist and skips the Node build entirely
```

The tarball is `git archive` of HEAD plus `frontend/dist` and a `RELEASE.json`
receipt. It refuses to build if any `.env`, `*.key` or password file ends up
staged, and it contains no database, no backups and no `node_modules`.

Node is still required on the server for the two review services
(`agent-review`, `commit-reviewer`) — the tarball removes it only from the
dashboard build.

## 10. Updating

```bash
git pull
./install.sh          # re-runs safely; installs any new dependencies
```

Restart the agent afterwards. Your `.env`, projects, skills and memory are
untouched.

---

## License

PolyForm Noncommercial 1.0.0 — source-available, not open source. Use, modify
and share it freely for any noncommercial purpose. Commercial use requires a
separate license; open an issue to start that conversation.
