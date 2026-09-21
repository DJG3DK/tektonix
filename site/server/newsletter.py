"""The newsletter signup behind tektonix.io.

Deliberately not part of the agent. It belongs to the landing page -- the same
reasoning that moved the page out of the product: a self-hosted Tektonix has
no business shipping an endpoint that writes to tektonix.io's mailing list.
`site/` is excluded from the release tarball, and this goes with it.

It does one thing: take a name and an address from the form on the landing
page and write them down. Nothing sends yet, on purpose.

No JavaScript at the other end
------------------------------
The landing page ships zero JavaScript, so this is a plain HTML form POST.
That shapes the whole interface:

  * form-encoded input, not JSON;
  * the answer is a 303 to a real page, because a status code with a JSON body
    is not something a browser would show anyone;
  * every outcome has a page, including failure, since there is no script to
    render an inline error.

A duplicate signup is a SUCCESS. The person's intent was "put me on the list",
they are on the list, and an error would send them away thinking it had not
worked.

On storing addresses
--------------------
Name, address, when, and a token for the unsubscribe link the first issue will
need. Addresses are never written to the log -- a log line is the easiest way
for a mailing list to leak, and this one would leak into a file that gets
tailed over someone's shoulder.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from psycopg_pool import AsyncConnectionPool

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("newsletter")

def _load_env_file(path: str) -> None:
    """Read KEY=value lines into the environment, without overwriting.

    A few lines of parsing rather than a dotenv dependency: this service
    exists to have very few. A missing file is fine -- the values can come
    from the process environment instead.
    """
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))
    except OSError:
        pass


_HERE = os.path.dirname(os.path.abspath(__file__))
# Its own file first, so the service is self-contained under pm2 rather than
# depending on whatever environment happened to start it.
_load_env_file(os.path.join(_HERE, ".env"))

# Its own table, and its own reason to exist. The DSN falls back to the box's
# existing database because that is what pg_dump already backs up
# (docs/backup.md); point it somewhere else and nothing here cares.
DSN = os.environ.get("NEWSLETTER_PG_DSN") or os.environ.get("LANGGRAPH_PG_DSN") or ""
SITE_URL = (os.environ.get("SITE_URL") or "https://tektonix.io").rstrip("/")
PORT = int(os.environ.get("NEWSLETTER_PORT") or 8300)

OK_URL = f"{SITE_URL}/subscribed/"
FAIL_URL = f"{SITE_URL}/subscribe-failed/"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS newsletter_subscribers (
    id SERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    -- For the unsubscribe link every issue has to carry. Minted now so the
    -- first send does not have to backfill one for everybody.
    unsubscribe_token TEXT UNIQUE NOT NULL,
    -- Nothing sends yet, so nothing is confirmed yet. Double opt-in needs a
    -- mail to confirm WITH; the column is here so adding it later is a write
    -- rather than a migration.
    confirmed_at TIMESTAMPTZ,
    unsubscribed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source TEXT
);
"""

# Deliberately loose. Address syntax is far stranger than any regex people
# write for it, and the only thing that truly validates an address is sending
# to it. This rejects the obviously-not-an-address and lets the rest through.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")

# A public HTML form with no CAPTCHA. Five signups from one IP in a minute
# is a person retrying; thirty is a bot filling the table. Same shape as
# agent/rate_limit.py (sliding window, X-Real-IP first) but local -- this
# service must not import the agent. Fail-open on its own errors so a
# limiter bug cannot take the form down. The answer is still a 303: there
# is no JavaScript to render a 429.
_SUBSCRIBE_MAX = 5
_SUBSCRIBE_WINDOW_S = 60
_subscribe_hits: dict[str, list[float]] = defaultdict(list)


def _client_ip(request: Request) -> str:
    # X-Real-IP only when a proxy we control set it (nginx overwrites the
    # header from $remote_addr). X-Forwarded-For is not consulted: without
    # that proxy it is the client's own string, and rotating it would be
    # the whole limiter. The socket peer is the fallback.
    real = request.headers.get("x-real-ip")
    if real:
        return real.strip()
    return request.client.host if request.client else "unknown"


# An IP is only pruned when it comes back, so without this the table keeps one
# entry per address that ever submitted the form -- for the life of the
# process, on a public page, where rotating the source address is the cheapest
# thing an abuser does. Swept opportunistically rather than on a timer: this
# service has no scheduler, and a sweep that runs when the table is already
# small is work nobody needed.
_SWEEP_AT = 10_000


def _sweep(now: float) -> None:
    for ip in [ip for ip, hits in _subscribe_hits.items()
               if not hits or now - hits[-1] >= _SUBSCRIBE_WINDOW_S]:
        del _subscribe_hits[ip]


def _subscribe_allowed(ip: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    try:
        if len(_subscribe_hits) >= _SWEEP_AT:
            _sweep(now)
        recent = [t for t in _subscribe_hits[ip] if now - t < _SUBSCRIBE_WINDOW_S]
        if len(recent) >= _SUBSCRIBE_MAX:
            _subscribe_hits[ip] = recent
            return False
        recent.append(now)
        _subscribe_hits[ip] = recent
        return True
    except Exception:  # noqa: BLE001 -- a limiter bug must not refuse a person
        return True


def reset_subscribe_limiter() -> None:
    """Tests only. The window is process-global."""
    _subscribe_hits.clear()


pool: AsyncConnectionPool | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if not DSN:
        logger.error("no NEWSLETTER_PG_DSN or LANGGRAPH_PG_DSN; signups will be refused")
        yield
        return
    pool = AsyncConnectionPool(DSN, min_size=1, max_size=4, kwargs={"autocommit": True}, open=False)
    await pool.open(wait=True)
    async with pool.connection() as conn:
        await conn.execute(_SCHEMA)
    logger.info("newsletter: ready on :%s", PORT)
    try:
        yield
    finally:
        await pool.close()


app = FastAPI(title="tektonix.io newsletter", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
async def health():
    if pool is None:
        return JSONResponse({"ok": False, "detail": "no database"}, status_code=503)
    try:
        async with pool.connection() as conn:
            await conn.execute("SELECT 1")
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "detail": type(e).__name__}, status_code=503)
    return {"ok": True, "service": "newsletter"}


@app.post("/subscribe")
async def subscribe(request: Request, name: str = Form(""), email: str = Form("")):
    """Take one signup. Always answers with a redirect, never a bare status."""
    if not _subscribe_allowed(_client_ip(request)):
        logger.info("newsletter: refused a signup (rate limit)")
        return RedirectResponse(FAIL_URL, status_code=303)

    name = " ".join(name.split())[:120]
    email = email.strip().lower()[:254]

    if not name or not _EMAIL.match(email):
        # No address in the log line: this is the one place a bad address
        # would otherwise be written down verbatim.
        logger.info("newsletter: rejected a signup (name=%s, address invalid=%s)",
                    bool(name), not _EMAIL.match(email))
        return RedirectResponse(FAIL_URL, status_code=303)

    if pool is None:
        logger.error("newsletter: no database, dropping a signup")
        return RedirectResponse(FAIL_URL, status_code=303)

    try:
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO newsletter_subscribers (email, name, unsubscribe_token, source) "
                "VALUES (%s, %s, %s, %s) "
                # Already on the list is not a failure -- see the module
                # docstring. Refresh the name in case they spelled it
                # differently, and clear an earlier unsubscribe, because
                # signing up again is a clear enough statement of intent.
                "ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name, unsubscribed_at = NULL",
                (email, name, secrets.token_urlsafe(24), "landing-page"))
    except psycopg.Error:
        logger.exception("newsletter: could not store a signup")
        return RedirectResponse(FAIL_URL, status_code=303)

    logger.info("newsletter: signup stored (now %s on the list)", await _count())
    return RedirectResponse(OK_URL, status_code=303)


async def _count() -> int:
    if pool is None:
        return 0
    try:
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM newsletter_subscribers WHERE unsubscribed_at IS NULL")
            row = await cur.fetchone()
        return int(row[0]) if row else 0
    except psycopg.Error:
        return 0


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT)
