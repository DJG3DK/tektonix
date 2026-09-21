"""A project memory shaped like a real one, invented end to end.

Not a toy. The properties being tested here -- what the always-block picks,
what an index entry reads like, how many tokens the split actually saves --
are all properties of realistic prose, and a three-heading fixture would make
every one of them look better than it is. So this is the shape of the real
thing: a short preamble, a dozen `##` sections, two of them long enough that
they are most of the file, several small ones that are exactly the
fails-silently rules the operator's policy pins, headings with punctuation, a
duplicate heading, and a fenced code block containing its own `##` line.

The project is fictional (a storefront and a worker) and so is every fact in
it. This repository is public: tests/test_repo_hygiene.py enforces that no
real project of the maintainer's is named anywhere in the tree, and a
fixture that reads convincingly is exactly where a real name would otherwise
turn up.
"""

EXAMPLE_MEMORY = '''# Project memory: demo

The demo project is a storefront (React, Vite) with a Python worker behind it.
The worker reconciles the catalogue nightly and the storefront never talks to
the upstream supplier directly.

## How this project is laid out

The tree is two halves that share nothing but a JSON contract. `frontend/` is
the Vite app; everything it renders comes from `frontend/src/api/client.ts`,
which is the only module allowed to name a URL. `worker/` is the Python side:
`worker/reconcile.py` is the nightly entry point, `worker/catalogue.py` owns
the data model, and `worker/adapters/` holds one module per upstream supplier.

Adding a supplier means a new module under `worker/adapters/` plus a row in
`worker/adapters/__init__.py`'s registry. Nothing else -- the reconciler
discovers adapters through that registry and there is no second list to keep
in step.

The API contract lives in `contracts/catalogue.schema.json` and both halves
generate from it. If you change a field name, regenerate both sides in the
same commit; the storefront's types are checked in, so a half-done rename
typechecks locally and breaks in CI.

## Working in the sandbox

Build tasks run inside a container with the repo mounted at `/workspace` and
no network. That means three things in practice.

Installs do not work mid-task. `npm install` and `pip install` both fail with
a DNS error that reads like a broken package rather than a missing network.
Everything the tests need is already vendored; if something genuinely is not
there, say so rather than trying to fetch it.

The container's clock is UTC and the host's is not. Tests that compare a
rendered date against `date.today()` pass on the host and fail here.

Paths are the mount, not the host. `/workspace/worker/reconcile.py` is the
same file as `worker/reconcile.py`; the host path the dashboard shows does not
exist inside the container at all.

## Testing

`pytest` from the repo root runs the Python suite, but only through the
project venv: `.venv/bin/pytest`. A bare `pytest` resolves to a system
interpreter that cannot import `worker`, collects zero tests, and exits 0 --
a green run that tested nothing.

Frontend tests are `npm test --prefix frontend`, and they need
`frontend/src/setupTests.ts` to have run; vitest picks it up from
`vite.config.ts`, so a test file added outside `frontend/src/` gets no DOM and
fails on `document is not defined`.

Every test that touches the catalogue must use the `catalogue_fixture` in
`tests/conftest.py` rather than building rows by hand. The fixture pins the
schema version, and a hand-built row passes today and stops matching the
contract the next time a field is added.

## Conventions

These are the house rules. They are not style preferences; each one exists
because the opposite cost someone a day.

Money is integer cents, everywhere, in both halves. There is a `Money` type in
`worker/money.py` and a matching `formatCents` in
`frontend/src/format.ts`. Floats never touch a price: the storefront once
showed a total of 19.999999999 and the fix was not rounding, it was removing
the float.

Times are stored as UTC ISO-8601 strings with an explicit `Z`, converted for
display only. The worker writes them, the storefront formats them, and nothing
in between reinterprets them.

Every network call the worker makes goes through `worker/http.py`, which owns
the retry policy and the timeout. A module that reaches for `requests`
directly gets neither, and the failure mode is a nightly run that hangs until
the watchdog kills it.

Errors that a human will read carry the thing that failed, not the layer that
noticed. "supplier acme returned 503 for /v2/catalogue" is useful; "upstream
error" is not. The convention is that the message names the target and the
operation, and that the exception type is the one the caller can act on.

Logging goes through `worker/log.py`. Module-level `logging.getLogger`
elsewhere bypasses the JSON formatter and those lines never reach the
dashboard, so they look like silence rather than like output in the wrong
shape.

Functions that write files take a directory argument rather than reading one
from config at the point of use. The tests depend on it: every writer in the
worker is exercised against a tmp_path, and the one that read its destination
from the config module had to be tested by monkeypatching a global.

Naming: modules are singular nouns (`catalogue`, not `catalogues`), functions
that fetch are `fetch_*`, functions that compute from what is already in hand
are `build_*`. Adapters are named after the supplier, lowercase, no vendor
suffix -- `acme.py`, not `acme_adapter.py`.

React components live one per file, named the same as the file, default
export. Hooks live in `frontend/src/hooks/` and are the only place
`useEffect` is allowed to call the API client; a component that fetches
directly cannot be tested without a network stub and will not be reviewed.

CSS is modules (`Thing.module.css`) next to the component. There is no global
stylesheet beyond `frontend/src/reset.css`, and adding one is how two teams
end up overriding each other by specificity.

Commits are one change each, present tense, and they name the reason rather
than the diff. The commit that says "fix reconciler" is the one nobody can
find again six months later when the same bug comes back.

## Dependency remediation

This section is long because the work is fiddly and comes back every few
weeks when the scanner reports something.

The scanner runs on a schedule and opens one pull request per advisory. The
pull requests are not automatically correct: about a third of them bump a
package the worker pins deliberately, and merging those reintroduces a bug
this project already fixed by pinning.

Before touching any of them, read `worker/requirements.txt`'s comment block.
Every deliberate pin has a line saying which incident produced it. If the
advisory's fixed version is above the pin, the question is whether the
original incident still applies, and that question is answered by reading the
upstream changelog, not by running the tests -- the tests did not catch the
original bug either.

The image tooling is the usual offender. It is pinned below its latest major
because the newer one changes how it resolves colour profiles, and the
storefront's product shots came out washed out in a way no test asserts on.
If a scanner report forces that package up, regenerate the reference images
under `frontend/src/assets/` in the same pull request and look at them.

Transitive advisories are handled by a constraint, not by a direct
dependency. Adding a direct dependency on something the project does not
import means the next person cannot tell which imports are real.

When an advisory cannot be remediated -- no fixed version exists, or the fix
requires a major bump the project cannot take this quarter -- record it in
`docs/security-exceptions.md` with the date and the reason. An unrecorded
exception looks identical to an oversight the next time anyone audits it.

The javascript side is mechanical by comparison: `npm audit fix` is usually
right, the lockfile is committed, and the only rule is that the lockfile and
`package.json` move in the same commit.

## Name collisions to watch for

Two different `Catalogue` classes exist: `worker/catalogue.py::Catalogue` is
the data model, `worker/adapters/base.py::Catalogue` is the adapter-facing
protocol. They are not interchangeable and mypy will not tell you, because
the protocol is structural and the model satisfies it.

`format.ts` in the frontend and `worker/format.py` do unrelated things and
neither imports the other.

There are two `config` objects: the Vite one in `frontend/vite.config.ts` and
the worker's `worker/config.py`. A search for "config" returns both.

## Environment variables and secrets

The worker reads exactly four: `DEMO_DATABASE_URL`, `DEMO_SUPPLIER_KEY`,
`DEMO_LOG_LEVEL` and `DEMO_DRY_RUN`. They are read once in
`worker/config.py`; nothing else calls `os.environ` and nothing should.

A missing variable raises at import. A misspelt one does not -- it takes the
default and the run looks normal, which is how a whole night reconciled
against the wrong database.

`DEMO_DRY_RUN=1` makes the reconciler compute everything and write nothing.
Use it for any change to `worker/reconcile.py`; the dry run prints the same
summary the real run does.

Secrets never go in `frontend/`. Anything the browser can read is public, and
the supplier key is not. The storefront asks the worker, the worker holds the
key.

## The nightly reconciler

`worker/reconcile.py` runs at 03:00 UTC from cron. It pulls each adapter's
catalogue, diffs it against what is stored, and writes the changes in one
transaction.

The diff is by supplier SKU, not by our own id. Suppliers reuse their own
SKUs across seasons, so the diff also compares the season field, and two rows
with the same SKU and different seasons are two products rather than an
update.

Runs are idempotent. Running it twice in a row produces no second set of
changes, which is what makes it safe to re-run after a partial failure.

The run writes a summary to `data/reconcile.log`: counts added, updated,
removed, and the wall time per adapter. When a nightly run is reported as
slow, that file answers which adapter to look at before anything else.

A run that finds more than 20% of the catalogue removed aborts instead of
applying. A supplier returning an empty page looks exactly like a supplier
discontinuing their whole range, and the abort is what stands between the two.

## Deploying

The worker is a systemd unit; the storefront is static files behind the same
nginx. Order matters: build the frontend first, then restart the worker, then
reload nginx. Reloading nginx before the build finishes serves a half-written
bundle.

`scripts/deploy.sh` does all three in that order. It is the only supported
way to deploy and it takes no arguments.

## Billing figures come from the ledger

Any number quoted to the operator about supplier spend comes from
`data/ledger.jsonl`, which records what was actually invoiced. The rate table
in `worker/rates.py` is what the project expects to be charged, and it is
regularly wrong: suppliers apply volume discounts after the fact.

The two are reconciled monthly by `scripts/reconcile_ledger.py`. If they
disagree by more than a rounding cent, the ledger is right.

## Past incidents worth remembering

The double-charge of the spring sale came from a retry without an idempotency
key. `worker/http.py` now requires one for any non-GET, and the storefront
generates it. Removing that requirement to "simplify" the client is how it
comes back.

The empty-catalogue night, when a supplier's API returned 200 with an empty
body and the reconciler removed their entire range, is the incident the 20%
abort exists for.

The washed-out product shots came from an image library major bump, and the
reason the pin in `worker/requirements.txt` has a comment.

Here is the shape a migration note takes, and yes, it contains headings:

```markdown
## What changed
## What to do about it
```

Those are not sections of this file.

## Conventions

A second heading with the same title, from a later consolidation run that did
not notice the first. Left here deliberately: two sections can share a name,
and the second must not overwrite the first's file.
'''

# Three more long sections, appended rather than inlined above so the fixture
# stays readable: a real memory of this age is mostly a few long sections, and
# a fixture where every section is short would make the split look better than
# it is.
EXAMPLE_MEMORY += '''
## Frontend patterns

State lives in one of three places and nowhere else. Server data is in the
query cache (`frontend/src/api/queries.ts`), which owns every fetch, every
retry and every invalidation. Form state is local to the form. Everything
genuinely global -- the signed-in operator, the theme -- is in
`frontend/src/AppContext.tsx`, which has four fields and is expected to keep
having about four.

There is no Redux here and there was, once. It went because every screen's
state turned out to be either server data that the cache already modelled
better or form state that was local by nature, and the store had become a
place where the two got copied into each other and drifted.

Data fetching is by key, and the key names the resource and its arguments:
`["catalogue", supplierId]`. Invalidation is by prefix, so writing a product
invalidates `["catalogue"]` and every supplier's list refetches. A component
that fetches with its own useEffect gets none of this and will be sent back.

Loading states are per-region, not per-page. A page that blanks itself while
one panel refetches reads as a crash to anyone using it, and the operator
uses this dashboard while the nightly run is happening.

Errors render in place with the operation named and a retry button that calls
the same query. There is no toast system and adding one has been discussed
twice; the conclusion both times was that an error that disappears on its own
is an error nobody acts on.

Tables are virtualised above 200 rows via `frontend/src/components/Rows.tsx`.
Below that, do not: the virtualiser breaks text selection and the catalogue
screens are read by people who copy SKUs out of them.

Dates render through `formatWhen` in `frontend/src/format.ts`, which prints a
relative time under a day old and an absolute one above. Both come from the
same UTC string the worker wrote; nothing in the frontend constructs a Date
from parts.

Icons are inline SVG components under `frontend/src/icons/`. No icon font, no
sprite sheet. The sprite sheet was removed when a build step started emitting
it after the HTML that referenced it.

Anything that takes longer than about 400ms gets an optimistic update or a
progress indication, and a mutation that cannot be made optimistic says what
it is waiting for.

## Data model and migrations

The catalogue is three tables: `products`, `variants` and `supplier_rows`.
`supplier_rows` is what the adapters write and it is append-only -- every
nightly run inserts the rows it saw, it never updates them. `products` and
`variants` are derived from it by `worker/catalogue.py::project_rows`.

That shape is deliberate and it is the second one this project has had. The
first updated products in place from each run, and when a supplier sent a bad
night there was nothing to reconstruct the previous state from. Append-only
rows mean any night can be replayed.

Migrations are plain SQL files under `worker/migrations/`, numbered, applied
in order by `worker/migrate.py`. They are applied on worker startup, inside a
transaction, and the version is recorded in a `schema_version` table.

A migration is never edited after it has run anywhere. Write another one.
Editing a migration that has already been applied means two databases with
the same version number and different schemas, and nothing detects it.

Every migration must be safe to apply while the storefront is serving.
Practically that means: add columns nullable, backfill in a separate step,
and only then add the constraint. A single-statement migration that rewrites
a large table takes a lock the storefront's reads will queue behind.

Down-migrations are not written. Restoring from the nightly dump is the
rollback path, and pretending otherwise has caused more damage than it
prevented: a down-migration that drops a column drops the data with it.

The dump is `scripts/dump.sh`, it runs before the reconciler at 02:45 UTC,
and it keeps fourteen days under `backups/`. Restoring is `scripts/restore.sh
<file>` and it refuses to run against a database with open connections.

Ids are UUIDv7, generated in the worker, never by the database. They sort by
creation time, which is what makes paging by id stable, and generating them
application-side means a row can be referenced before it is inserted.

## Working with the supplier APIs

Every adapter implements `fetch_catalogue(since)` and nothing else. The
reconciler owns the schedule, the retries and the diffing; an adapter's whole
job is turning one supplier's response into catalogue rows.

Suppliers paginate differently and all three of them are wrong in some way.
One returns a `next` cursor that is occasionally a full URL and occasionally
a token; `worker/adapters/acme.py` normalises it. One paginates by offset and
will happily return overlapping pages if the catalogue changes underneath the
walk, so its adapter deduplicates by SKU. One has no pagination at all and
returns everything, which is fine until it is not.

Rate limits are per-supplier and documented in each adapter's module
docstring, because they are not documented anywhere else -- they were
discovered by hitting them. `worker/http.py` enforces them with a token
bucket per host.

A 429 is retried with the supplier's own `Retry-After` when it sends one and
an exponential backoff when it does not. A 5xx is retried three times. A 4xx
that is not 429 is never retried: it means the request is wrong, and retrying
a wrong request three times just means three wrong requests.

Responses are cached to `data/supplier_cache/` for the duration of a run, so
a reconciler that fails halfway and is re-run does not re-fetch everything.
The cache is keyed by URL and cleared at the start of each run.

Supplier fields are mapped explicitly, field by field, in the adapter. There
is no generic mapper driven by a dict, and the one that existed was replaced:
when a supplier renamed a field, the generic version silently produced rows
with an empty description and nothing failed until a customer noticed.

Test adapters against the recorded fixtures under `tests/supplier_fixtures/`
rather than the live API. The fixtures were captured from real responses with
the keys removed, and they include the malformed pages each supplier is known
to emit.
'''
