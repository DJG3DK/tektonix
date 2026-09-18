import logoUrl from "./assets/tektonix-logo.png";
import shotPipeline from "./assets/shots/models2.webp";
import shotAnalytics from "./assets/shots/analytics2.webp";
import shotLedger from "./assets/shots/analytics3.webp";
import shotReviewer from "./assets/shots/models3.webp";
import shotSupport from "./assets/shots/models4.webp";
import shotUsers from "./assets/shots/users2.webp";
import shotPlanning from "./assets/shots/dashboard2.webp";
import "./LandingPage.css";

const REPO = "https://github.com/DJG3DK/tektonix";

// Where the newsletter form posts. Same origin as this page, so the form is a
// plain HTML POST with no fetch, no CORS and no JavaScript -- which is what
// keeps this page static. nginx routes it to site/server.
const SUBSCRIBE_URL = "/newsletter/subscribe";

// Who the newsletter and this page come from.
const OWNER_EMAIL = "danny@tektonix.io";

function GitHubMark() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true" width="15" height="15" fill="currentColor">
      <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82a7.42 7.42 0 0 1 2-.27c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8z" />
    </svg>
  );
}

/* The five stages a task moves through, as the outer graph actually runs them.
   "Review gate" is marked because it is the one that can send work backwards. */
const PIPELINE = [
  { label: "Plan", note: "write_todos" },
  { label: "Build", note: "sandboxed" },
  { label: "Verify", note: "real test suite" },
  { label: "Review gate", note: "2nd model", gate: true },
  { label: "Ship", note: "merge + deploy" },
];

const STAGES = [
  {
    kicker: "work",
    title: "It builds in a sandbox that can only see one repo",
    body: `The agent drives its own tool-calling loop against a Docker checkout with just the
      target repository mounted, so a shell command cannot reach your other projects or host
      secrets. It can delegate to two subagents: an investigator with no write or shell tools at
      all, and a test-writer that has to run the checks itself before it may report done.`,
  },
  {
    kicker: "verify_and_ship",
    title: "Its own “done” carries no authority",
    body: `The gate always re-runs your project's real typecheck, lint and test suite itself —
      the same commands you would run. Only a pass with a real diff becomes a commit, and only on
      the task's own branch. If the agent's todo list still has open items, the commit is held and
      the task is sent back to finish, with the remaining work named.`,
  },
  {
    kicker: "review",
    title: "A second model reviews the branch before a merge is possible",
    body: `The review unit is the branch plus its merge-base, fixed at the fork point rather than
      whatever the sandbox HEAD is now — comparing two moving HEADs produced inverted diffs,
      where a branch's additions read as deletions. READY merges and deploys. NEEDS_FIXES loops
      back to work carrying the findings.`,
  },
];

const FEATURES = [
  {
    img: shotLedger,
    alt: "Analytics — per-role model usage with call counts, tokens, latency, cost and cache rate",
    title: "Where the tokens actually went",
    body: `Every role's real usage, read from the router's own per-call ledger: calls, tokens,
      average latency, what it cost, and how much of it was served from cache. An expensive
      role is something you can see here rather than infer from the bill at the end of the
      month.`,
  },
  {
    img: shotReviewer,
    alt: "The commit reviewer's role, showing how many probed models meet its requirements",
    title: "Models are probed, not assumed",
    body: `The reviewer needs strict tool calling and a forced response shape, and most models
      do not have both. Each role lists the ones that actually passed its probes — 134 of 253
      on the last run — so you find out here rather than four minutes into a task.`,
  },
  {
    img: shotSupport,
    alt: "Support roles — classifier, summarizer, vision and cartographer, each with capability badges",
    title: "The support roles are separate on purpose",
    body: `Classifier, summarizer, vision and cartographer do not need what the coder needs, and
      paying coder prices for them is waste. Each says plainly what it requires — structured
      output only, plain completion, a large context window — and each is pinned on its own.`,
  },
  {
    img: shotUsers,
    alt: "Users — per-project access control and adding a user",
    title: "People get the projects they need, not all of them",
    body: `An account is scoped to named projects, and everything follows that scope: the task
      list, planning sessions, analytics. Admins see everything; a teammate added for one
      repository cannot start work against another.`,
  },
];

const CONTROLS = [
  {
    title: "A budget ceiling per task",
    body: "Checked after every model call, on the coordinator and on every subagent, so a runaway loop costs a known maximum.",
  },
  {
    title: "The agent cannot git push",
    body: "Its shell runs in a throwaway container with no SSH key, no git credentials and no token, so a push has nothing to authenticate with — it fails at the remote, force or not. Merges happen through the gate, on the host, and each project pushes with its own deploy key, scoped to one repository.",
  },
  {
    title: "Work can arrive from GitHub, not only the composer",
    body: "The inbox polls Dependabot PRs, security and code-scanning alerts, review comments and failing checks. Propose sends an approve link; Auto starts the task. Every one still runs the review gate and keeps your merge approval.",
  },
  {
    title: "Approvals arrive inline",
    body: "When it hits a gated action, or calls ask_user to ask you something rather than guess, the request appears in the task stream and your answer goes back into the same paused thread.",
  },
  {
    title: "The server decides what runs, and where",
    body: "A project can only be onboarded from inside AGENT_PROJECT_ROOTS, judged after symlinks resolve, and its worktree location and name are derived rather than taken from the request. The commands the gate runs are matched against what the server itself proposed, so a client cannot introduce a new one for the review or deploy service to execute.",
  },
  {
    title: "No task is a dead end",
    body: "Escalated, stopped, out of budget, or orphaned by a backend restart — the full graph state lives in the checkpointer, so resuming continues the same thread instead of starting over.",
  },
];

export function LandingPage() {
  return (
    <div className="landing">
      <header className="lp-nav">
        <div className="lp-nav-inner">
          {/* The lockup already reads "3D agent", so no wordmark beside it. */}
          <a className="lp-brand" href="#top" aria-label="Tektonix, back to top">
            <img src={logoUrl} alt="Tektonix" />
          </a>
          <nav className="lp-nav-links">
            <a href="#how">How it works</a>
            <a href="#console">Console</a>
            <a href="#controls">Controls</a>
          </nav>
          <div className="lp-nav-actions">
            <a className="lp-ghost" href={REPO} target="_blank" rel="noopener noreferrer">
              <GitHubMark />
              <span>GitHub</span>
            </a>
            <a className="lp-signin" href="#newsletter">
              Get updates
            </a>
          </div>
        </div>
      </header>

      <main id="top">
        <section className="lp-hero">
          <p className="lp-eyebrow">Self-hosted &middot; LangGraph &middot; deepagents &middot; FastAPI + React &middot; any model, via OpenRouter</p>
          <h1>
            An autonomous coding agent
            <br />
            that has to prove its work.
          </h1>
          <p className="lp-lede">
            Give it a plain-English goal against a repository you have onboarded. It plans the
            work, writes the code, runs that project&rsquo;s <em>real</em> test suite, and ships
            it &mdash; with an independent review gate that must pass before anything merges.
            Self-hosted on your own machine, against your own repositories, driving whichever
            models you pin through OpenRouter.
          </p>
          <div className="lp-cta">
            <a className="lp-btn lp-btn-primary" href={REPO} target="_blank" rel="noopener noreferrer">
              <GitHubMark />
              View the source
            </a>
            <a className="lp-btn lp-btn-quiet" href="#how">
              How a task runs
            </a>
          </div>

          <ol className="lp-pipeline" aria-label="Task pipeline">
            {PIPELINE.map((s, i) => (
              <li key={s.label} className={s.gate ? "is-gate" : undefined}>
                <span className="lp-pipe-index">{i + 1}</span>
                <span className="lp-pipe-label">{s.label}</span>
                <span className="lp-pipe-note">{s.note}</span>
              </li>
            ))}
          </ol>

          <figure className="lp-shot lp-shot-hero">
            <div className="lp-chrome" aria-hidden="true">
              <span /> <span /> <span />
            </div>
            <img src={shotPipeline} alt="Model configuration — the build pipeline roles, each with its own pinned model and live pricing" />
          </figure>
        </section>

        <section id="how" className="lp-section">
          <h2 className="lp-h2">How a build task runs</h2>
          <p className="lp-sub">
            Two nodes and a loop. Work produces a change; the gate decides whether it earns a
            merge, and is allowed to send it back.
          </p>
          <div className="lp-stages">
            {STAGES.map((s) => (
              <article key={s.kicker} className="lp-stage">
                <code className="lp-kicker">{s.kicker}</code>
                <h3>{s.title}</h3>
                <p>{s.body}</p>
              </article>
            ))}
          </div>
          <p className="lp-pullquote">
            &ldquo;The agent&rsquo;s own <em>done</em> carries no authority.&rdquo;
          </p>
        </section>

        <section id="console" className="lp-section">
          <h2 className="lp-h2">The console</h2>
          <p className="lp-sub">
            One React app, served by the backend itself. Live task and planning output arrives over
            WebSockets, with a REST snapshot on every reconnect &mdash; so a page opened mid-task
            shows real history instead of starting blank.
          </p>
          <figure className="lp-lead">
            <div className="lp-shot">
              <div className="lp-chrome" aria-hidden="true">
                <span /> <span /> <span />
              </div>
              <img
                src={shotAnalytics}
                alt="The dashboard — spend, outcomes and per-repo cost, with planning sessions and tasks grouped by category in the sidebar"
              />
            </div>
            <figcaption>
              Planning sessions and build tasks share one sidebar, grouped by the same category
              the classifier assigns and filterable per repo. Spend, outcomes and average fix
              cycles sit up front — the review gate&rsquo;s own cost counted separately, because
              the agent&rsquo;s budget and the gate&rsquo;s are different things. Remaining credit
              sits at the bottom and turns red under 15%, so running dry is something you see
              coming.
            </figcaption>
          </figure>
          <div className="lp-features">
            {FEATURES.map((f, i) => (
              <article key={f.title} className={`lp-feature${i % 2 ? " is-flipped" : ""}`}>
                <figure className="lp-shot">
                  <div className="lp-chrome" aria-hidden="true">
                    <span /> <span /> <span />
                  </div>
                  <img src={f.img} alt={f.alt} loading="lazy" decoding="async" />
                </figure>
                <div className="lp-feature-copy">
                  <h3>{f.title}</h3>
                  <p>{f.body}</p>
                </div>
              </article>
            ))}
          </div>
        </section>

        <section className="lp-section lp-planning">
          <div className="lp-planning-copy">
            <h2 className="lp-h2">Planning Chat</h2>
            <p className="lp-sub">
              A separate agent for research, design and scoping something before it gets built. No
              write or shell access to the real repo &mdash; research tools, a headless browser,
              read-only reads of your other projects, and a plan it can hand straight to the build
              pipeline.
            </p>
            <p className="lp-sub">
              Three seats, chosen automatically: an everyday model, a harder one a turn
              escalates into, and a frontend seat that sits ahead of that ladder so a UI
              plan is written by the model that will build it. Difficulty is classified
              fresh every turn, then sticky upward within a session &mdash; a short
              follow-up cannot quietly downgrade the model mid-plan.
            </p>
            <figure className="lp-shot lp-planning-shot">
              <div className="lp-chrome" aria-hidden="true">
                <span /> <span /> <span />
              </div>
              <img
                src={shotPlanning}
                alt="Starting a planning session — choosing the repository and the model route"
                loading="lazy"
                decoding="async"
              />
            </figure>
          </div>
          <ul className="lp-facts">
            <li>
              <strong>Memory that compounds</strong>
              <span>Completed tasks consolidate into per-project memory; a cartographer keeps a structural map of each codebase current.</span>
            </li>
            <li>
              <strong>A per-turn dollar ceiling</strong>
              <span>Planning used to run uncapped. It was the one agent with no budget, and the one that once spent $7 on a single 157-call turn.</span>
            </li>
            <li>
              <strong>Codebase-map first</strong>
              <span>The map is read before any directory walking. One read replaces a dozen exploratory listings.</span>
            </li>
          </ul>
        </section>

        <section id="controls" className="lp-section">
          <h2 className="lp-h2">What the model cannot override</h2>
          <p className="lp-sub">
            This runs a model that writes and executes code against your repositories. The controls
            are the product, not an afterthought.
          </p>
          <div className="lp-controls">
            {CONTROLS.map((c) => (
              <article key={c.title} className="lp-control">
                <h3>{c.title}</h3>
                <p>{c.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="lp-section lp-close">
          <h2 className="lp-h2">Run it yourself</h2>
          <p className="lp-sub">
            Two ways in. Both end at the same console, and both are safe to re-run &mdash; which
            is also the upgrade path.
          </p>
          <div className="lp-installs">
            <article className="lp-install">
              <code className="lp-kicker">docker compose</code>
              <h3>The bundle</h3>
              <p>
                Agent, database, router and the review services as one stack. Nothing to install
                but Docker itself, and it runs the same on Windows and macOS as it does on Linux.
              </p>
              <pre><code>{`cp docker/.env.example .env
docker compose up -d`}</code></pre>
              <p className="lp-reqs">Docker &middot; an OpenRouter API key</p>
            </article>
            <article className="lp-install">
              <code className="lp-kicker">./install.sh</code>
              <h3>On the host</h3>
              <p>
                Takes a fresh clone to a running agent &mdash; prerequisites, secrets, database,
                sandbox image and dashboard. Ask it three questions and it derives the rest.
                <code>--dry-run</code> prints every action without performing one;
                <code>--yes</code> reads its answers from the environment for an unattended
                build.
              </p>
              <pre><code>{`git clone ${REPO}.git
cd tektonix && ./install.sh`}</code></pre>
              <p className="lp-reqs">
                Linux &middot; Python 3.12+ &middot; Node 24+ &middot; Docker &middot;
                PostgreSQL 14+ &middot; an OpenRouter API key
              </p>
            </article>
          </div>
          <p className="lp-sub lp-install-note">
            A domain is optional either way &mdash; the agent binds to localhost, and an SSH
            tunnel reaches it without nginx or a certificate. Give the installer a domain and it
            will set up both.
          </p>
          {/* One action here, and it is the repo. The newsletter has its own
              section below; putting two calls to action in one block splits
              the attention of someone who has just finished reading. */}
          <div className="lp-cta">
            <a className="lp-btn lp-btn-primary" href={REPO} target="_blank" rel="noopener noreferrer">
              <GitHubMark />
              View the source
            </a>
          </div>
        </section>

        {/* A plain HTML form: method, action, two fields. No fetch, no
            handler, no JavaScript -- the browser posts it and follows the
            redirect the server answers with, which is what keeps this page
            static. site/server/newsletter.py is the other end. */}
        <section className="lp-section lp-news" id="newsletter">
          <h2 className="lp-h2">What changed, and what is coming</h2>
          <p className="lp-sub">
            Every release ships with a changelog that says what moved and, more usefully, why
            it had to. The newsletter is that changelog, plus what is being built next and
            what turned out to be a bad idea. No cadence promised beyond &ldquo;when there is
            something worth reading&rdquo;, and nothing else is ever sent to this list.
          </p>
          <form className="lp-news-form" method="post" action={SUBSCRIBE_URL}>
            <div className="lp-news-fields">
              <label className="lp-field">
                <span>Name</span>
                <input
                  type="text"
                  name="name"
                  autoComplete="name"
                  maxLength={120}
                  required
                  placeholder="Ada Lovelace"
                />
              </label>
              <label className="lp-field">
                <span>Email</span>
                <input
                  type="email"
                  name="email"
                  autoComplete="email"
                  maxLength={254}
                  required
                  placeholder="ada@example.com"
                />
              </label>
            </div>
            <button className="lp-btn lp-btn-primary lp-news-submit" type="submit">
              Sign up for the newsletter
            </button>
            <p className="lp-news-fine">
              Your name and address, stored to send you the newsletter and nothing else. Never
              sold, never shared.
            </p>
          </form>
        </section>
      </main>

      <footer className="lp-foot">
        <div className="lp-foot-brand">
          <img src={logoUrl} alt="Tektonix" />
        </div>
        <p>
          Source available under PolyForm Noncommercial 1.0.0 &mdash; free for any noncommercial
          use. Built and maintained by{" "}
          <a href={`mailto:${OWNER_EMAIL}`}>{OWNER_EMAIL}</a>.
        </p>
        <a href={REPO} target="_blank" rel="noopener noreferrer">
          <GitHubMark />
          github.com/DJG3DK/tektonix
        </a>
      </footer>
    </div>
  );
}
