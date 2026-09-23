"""FastAPI backend -- task creation/listing/state, WebSocket streaming of a
running task's graph execution. Checkpointer + store are opened once at
startup and shared for the app's lifetime (both are long-lived async context
managers, matching how langgraph-checkpoint-postgres expects to be used --
opening a fresh connection per request would be wasteful and race-prone).
"""

import asyncio
import html
import functools
import contextlib
import json
import logging
import os
import shutil
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

logger = logging.getLogger("tektonix")

import httpx
from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.staticfiles import NotModifiedResponse
from email.utils import parsedate_to_datetime as _parsedate
from pydantic import BaseModel, Field

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"

from agent import cartographer
from agent import paths
from agent import rate_limit
from agent.config import PROJECTS, load_config, require_server_config
from agent.observability import install_langsmith
from agent.outer_graph import build_outer_graph, initial_state, open_checkpointer, open_store, project_lock
from agent.graph import read_with_retry
from agent.routers import env_config as env_config_routes
from agent.routers import settings as settings_routes
from agent.routers import model_config as model_config_routes
from agent.routers import audit_store as _routers_audit_store
from agent.routers import analytics as analytics_routes
from agent.routers import push as push_routes
from agent.messages import add_message
from agent.tools.model_rates import warm_rates
from agent.classify import classify_task, TaskClassification, TEST_REMINDER_NOTE
from agent import runtime_settings
from agent import github_inbox, github_settings
from agent import audit
from agent import live_state
from agent.backends import backend_for_dsn
from agent.store_paging import recent_items
from agent import health as health_checks
from agent import episode_vectors, history_index
from agent import log_stream
from agent import plan_progress
from agent import planning_log
from agent.middleware.budget_guard import BudgetExceededError
from agent.frontend_route import RouteDecision, classify_frontend, normalize_override
from agent.planning_chat import build_planning_agent, classify_planning_difficulty, planning_thread_config, run_planning_turn, _translate_message as _translate_planning_message
from agent import auth
from agent.auth import SESSION_COOKIE_NAME, User, check_repo_access
from agent.notify import notify_operators, notify_operators_bg, send_telegram, task_alert, watch_services
from agent.mailer import send_plain_email

config = load_config()
# On the line after load_config, deliberately: the server-only variables are
# optional on Config so a local run does not need a mail server, and this is
# what puts the fail-fast boot back. It also refuses a DSN that is not
# Postgres -- see the function.
require_server_config(config)
install_langsmith(config)  # no-ops cleanly if LANGSMITH_TRACING isn't set -- see observability.py

# The live registries live in agent/live_state.py now: the routes that read
# them are moving into agent/routers/, and a route module cannot import back
# into this one without making the import a cycle. Aliased rather than
# re-declared so every existing reference in this file keeps working and --
# the part that matters -- keeps pointing at the SAME dict the routers hold.
_subscribers = live_state.subscribers
_running_tasks = live_state.running_tasks
_planning_subscribers = live_state.planning_subscribers
_running_planning_turns = live_state.running_planning_turns


def _log_warm_rates_failure(task: "asyncio.Task") -> None:
    """C-1: surface a warm_rates() startup failure instead of swallowing it.
    A failed rate warm means the hard budget ceiling has no data and would
    fail every task's first model call -- that must be visible in the log, not
    silent."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("CRITICAL: model-rate warm failed -- the budget ceiling has no rates: %r", exc)


async def _auto_resume_orphaned_tasks(startup_delay: float = 5.0) -> None:
    """Reconnect tasks orphaned by a restart, without operator action.

    resume_task's own docstring declares restarts routine ("this pm2 process
    gets restarted routinely to deploy fixes") and its orphan branch resumes
    from the checkpoint with no replanning -- but nothing ever CALLED that
    path automatically, so every deploy stranded any in-flight build until a
    human noticed the silence and clicked Resume (2026-08-27: the operator
    watched a stalled screener build for an hour and asked why). This is the
    missing last mile: at startup, find every task whose Store status says
    "running" while nothing drives it, and restart its driver exactly the
    way the endpoint's orphan branch does (same +40 iteration headroom, no
    budget change -- the operator already approved this task's budget).

    Deliberately NOT resumed:
      - escalated tasks (they are waiting for a human by design),
      - "stopped" (the operator chose that),
      - a task that made NO checkpoint progress since its last auto-resume
        (auto_resume_ckpt marker): if resuming it did nothing once, a boot
        loop of retries would amplify a poison task instead of surfacing it.
        It stays orphaned for a human, exactly as before this existed.
    """
    await asyncio.sleep(startup_delay)  # let startup settle; not correctness-critical
    graph = app.state.graph
    for repo in PROJECTS:
        try:
            items = await recent_items(app.state.store, ("tasks", repo), 50)
        except Exception:  # noqa: BLE001 -- one repo's failure must not strand the others
            logger.exception("auto-resume: task scan failed for %s", repo)
            continue
        for item in items:
            meta = item.value
            task_id = meta.get("task_id") or item.key
            # "queued" as well as "running": a task orphaned by a restart
            # while it was waiting for the project lock has exactly the same
            # problem -- a status the store believes and no process behind it.
            if meta.get("status") not in ("running", "queued") or task_id in _running_tasks:
                continue
            try:
                thread_config = {"configurable": {"thread_id": task_id}}
                checkpoint = await graph.aget_state(thread_config)
                values = checkpoint.values if checkpoint else None
                if not values or values.get("escalated"):
                    continue
                # Progress marker: outer checkpoint id PLUS the inner work
                # thread's latest checkpoint ts. The outer id alone is wrong
                # -- it stays frozen for an ENTIRE work pass (often 30+ min),
                # so two restarts inside one long pass would read as "no
                # progress" and wrongly strand a perfectly healthy task. The
                # inner thread checkpoints every step, so real work always
                # moves this marker.
                gen = values.get("inner_thread_generation", 0)
                inner_id = f"{task_id}:work:g{gen}" if gen else f"{task_id}:work"
                inner_ts = ""
                try:
                    inner_snap = await app.state.checkpointer.aget_tuple(
                        {"configurable": {"thread_id": inner_id}})
                    inner_ts = (inner_snap.checkpoint.get("ts") or "") if inner_snap else ""
                except Exception:  # noqa: BLE001 -- marker precision degrades, resume still works
                    logger.exception("auto-resume: inner-thread read failed for %s", task_id)
                outer_id = (checkpoint.config or {}).get("configurable", {}).get("checkpoint_id", "")
                ckpt_ts = f"{outer_id}|{inner_ts}"
                if meta.get("auto_resume_ckpt") == ckpt_ts:
                    logger.error(
                        "auto-resume: task %s made no progress since its last auto-resume -- "
                        "leaving it for the operator (possible poison task)", task_id)
                    continue
                await graph.aupdate_state(thread_config, {
                    "task_id": task_id,
                    "budget_usd": values["budget_usd"],
                    "max_iterations": values.get("max_iterations", 40) + 40,
                })
                await write_task_meta(app.state.store, repo, task_id,
                                      auto_resume_ckpt=ckpt_ts)
                _running_tasks[task_id] = asyncio.create_task(
                    _stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
                )
                logger.warning("auto-resume: reconnected orphaned task %s (%s) after restart", task_id, repo)
                _notify_bg(task_alert(
                    "auto_resumed", repo, values.get("goal", ""), values.get("cost_so_far"),
                    "The server restarted mid-run; the task reconnected automatically and is working again."),
                    repo=repo)
            except Exception:  # noqa: BLE001 -- one task's failure must not strand the others
                logger.exception("auto-resume: failed to reconnect task %s", task_id)


async def _drain_planning_turns(timeout: float = 15.0) -> None:
    """Cancel every in-flight planning turn and WAIT for its teardown.

    Called from lifespan shutdown while the store/checkpointer pools are
    still open -- the whole point. The tasks' own CancelledError handlers do
    the actual banking; this just guarantees they get to run against a live
    pool instead of racing process exit.
    """
    tasks = list(_running_planning_turns.values())
    if not tasks:
        return
    logger.info("shutdown: draining %d in-flight planning turn(s)", len(tasks))
    for t in tasks:
        t.cancel()
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    for t in pending:  # pragma: no cover -- only a wedged teardown lands here
        logger.error("shutdown: planning turn %r did not tear down within %.0fs", t.get_name(), timeout)


_INITIAL_PASSWORD_PATH = Path(__file__).resolve().parent.parent / ".initial-admin-password"


def _store_initial_password(password: str) -> None:
    """The first admin password, encrypted with AUTH_SECRET_KEY (the same
    AES-GCM construction as the TOTP secrets) into a 0600 file beside the
    repo. `scripts/show_initial_password.py` decrypts it once for the
    operator. Neither the log nor the disk ever holds it in clear."""
    try:
        enc = auth._encrypt_totp_secret(config, password)
        _INITIAL_PASSWORD_PATH.touch(mode=0o600, exist_ok=True)
        _INITIAL_PASSWORD_PATH.chmod(0o600)
        _INITIAL_PASSWORD_PATH.write_text(enc + "\n")
    except Exception:  # noqa: BLE001 -- the account exists either way; say so in the log
        logger.exception("could not store the initial admin password at %s", _INITIAL_PASSWORD_PATH)


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with open_checkpointer(config) as checkpointer, open_store(config) as store, auth.open_auth_pool(config) as auth_pool:
        app.state.checkpointer = checkpointer
        app.state.store = store
        app.state.auth_pool = auth_pool
        generated_password = await auth.seed_admin_if_none(auth_pool, config.admin_email)
        if generated_password:
            # Only ever printed once, the very first time this deployment
            # has zero users -- must_change_password=True forces a real
            # password to replace this on first login, so it's not a
            # standing secret sitting in the log after that.
            # Not logged (CodeQL py/clear-text-logging-sensitive-data): logs
            # are copied, shipped and grepped, and a password in one is a
            # password in every copy. Written once to a 0600 file beside the
            # repo instead; the log says where.
            _store_initial_password(generated_password)
            logger.warning(
                "Seeded initial admin account %s. Its one-time password is stored encrypted; "
                "run `.venv/bin/python scripts/show_initial_password.py` to read it "
                "(must be changed on first login).", config.admin_email,
            )
        # Both checkpointer and store passed to .compile() -- store isn't
        # actually read via LangGraph's own node-kwarg injection here (see
        # outer_graph.py's own comment: work_node/verify_and_ship_node use
        # app_config/pg_store, bound directly via functools.partial,
        # specifically to avoid that injection path), but passing it is
        # still correct/harmless and keeps this graph object consistent with
        # idiomatic LangGraph usage for anything else that might introspect it.
        app.state.graph = build_outer_graph(config, checkpointer, store).compile(
            checkpointer=checkpointer, store=store
        )
        # Auto-approve became per-project on 2026-09-11. An account that had
        # the switch on before that keeps the behaviour it had, with the
        # scope written down -- see backfill_auto_approve_repos for why this
        # is a backfill rather than a silent default.
        try:
            scoped = await auth.backfill_auto_approve_repos(auth_pool, list(PROJECTS))
            if scoped:
                logger.info("auto-approve: scoped %d pre-existing account(s) to %d project(s)",
                            scoped, len(PROJECTS))
        except Exception as e:  # noqa: BLE001 -- never block startup on a migration
            logger.error("auto-approve backfill failed (accounts stay unscoped): %s", e)

        # The keyword index over past tasks, on the auth pool rather than a
        # fourth pool against the same database. It migrates itself, and a
        # failure here costs history search and nothing else -- install_for
        # never raises.
        app.state.history_index = await history_index.install_for(config, pool=auth_pool)

        # The second retrieval leg, registered against the store that was
        # actually opened rather than against the config: a store opened
        # before an operator turned embeddings on carries no vector index,
        # and a leg registered over it would answer every query with the
        # most recently updated episodes while looking exactly like semantic
        # search. Nothing else changes -- search_history asks the registry.
        app.state.vector_leg = episode_vectors.install(store)

        # Stored runtime limits, before anything can build an agent with them.
        await runtime_settings.load(app.state.store)
        await github_settings.load(app.state.store)

        # No analytics pre-warm any more: those three panels read this box's
        # own logs now (agent/metrics.py), which is a file scan measured in
        # milliseconds rather than a paged LangSmith query that could exceed
        # nginx's proxy timeout. The caches, their locks and their
        # serve-stale-while-revalidate dance went with the scans.
        #
        # The rate table still warms: model_rates.estimate_cost's rate
        # table load includes a synchronous network call to OpenRouter's
        # pricing endpoint -- pre-warming it here means that blocking call
        # happens in a background thread before any real task needs a cost
        # estimate, not inline on the event loop the first time one does.
        # C-1: warm_rates() reads model-router/config.yaml; if that raises (bad
        # path, malformed yaml) the budget ceiling silently does not exist.
        # A bare create_task swallows the exception, so attach a done-callback
        # that surfaces it loudly at startup instead of at every task's first
        # model call.
        rates_warm_task = asyncio.create_task(warm_rates())
        rates_warm_task.add_done_callback(_log_warm_rates_failure)
        # Reconnect any build task a restart orphaned -- see the function's
        # own docstring. Backgrounded so startup never blocks on it.
        auto_resume_task = asyncio.create_task(_auto_resume_orphaned_tasks())
        # Service-restart alerts (operator request 2026-08-28): a router or
        # bot restarting mid-task is exactly the kind of event that used to
        # be discovered by watching a silent screen. The agent backend
        # itself is excluded from the poll (its restart resets this watcher)
        # and announces itself with the startup line below instead.
        service_watch_task = asyncio.create_task(watch_services(auth_pool))
        # GitHub inbox poller (Settings -> GitHub). Sleeps until a project
        # switches a source on; see _github_poll_loop.
        github_poll_task = asyncio.create_task(_github_poll_loop())
        notify_operators_bg(auth_pool, "🔄 agent backend restarted (deploys land this way; "
                            "orphaned tasks auto-resume, planning turns re-send)")
        yield
        github_poll_task.cancel()
        service_watch_task.cancel()
        auto_resume_task.cancel()
        # Drain in-flight planning turns BEFORE this `async with` block exits
        # and closes the Postgres pools. Left to the runtime, these tasks are
        # cancelled during asyncio.run() cleanup -- AFTER the pools are gone --
        # so the cancel-path teardown that banks the turn's plan/cost/title
        # (_bank_planning_turn) dies on its own store write. Confirmed live
        # 2026-08-27: a pm2 restart during a planning turn logged "failed to
        # persist planning progress" from exactly that ordering, and the
        # session's real spend (router-billed) was lost from the ledger.
        # Cancelling here, while the pools are still open, lets each turn's
        # CancelledError handler finish its banking write. The 15s ceiling is
        # generous -- banking is one store read + one write.
        await _drain_planning_turns(timeout=15.0)
        rates_warm_task.cancel()


app = FastAPI(lifespan=lifespan)
# On state from the moment the app exists, NOT in lifespan: the seams under
# agent/routers/ read config off the running app, and a TestClient exercises
# routes without ever entering lifespan. Setting it there would make every
# such test fail on a missing attribute rather than on anything real.
app.state.config = config


class _AppShim:
    """Just enough of a Request for routers.audit_store: it reads
    `request.app.state`, and the routes still in this module have the app
    itself rather than a request in scope."""

    app = app
# CORS was allow_origins=["*"] with a comment claiming nginx tightened it in
# production. nginx sets no CORS headers at all, so nothing did -- the comment
# described a control that did not exist. Real exposure was limited (credentials
# were never allowed, and the session cookie is SameSite=strict so it is not
# sent cross-site anyway), but a wildcard on an authenticated app is not
# something to leave sitting behind a false comment.
#
# This app serves its own frontend, so same-origin requests do not use CORS at
# all and the correct production value is "no origins". Only a split dev setup
# (Vite on its own port) needs any, via CORS_ALLOW_ORIGINS.
if config.cors_allow_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_allow_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Per-seam routers (agent/routers/). server.py is past 5,600 lines and is
# being split one seam at a time, never in one pass -- see docs/todo.md and
# docs/playbooks/README.md. tests/test_route_inventory.py is what makes each
# move safe: it pins every route's path, method and auth dependency, so a
# seam that moves either looks identical from outside or fails the snapshot.
# Included here, after the middleware and before the routes that are still in
# this file, so the order a request passes through is unchanged.
app.include_router(push_routes.router)
app.include_router(analytics_routes.router)
app.include_router(model_config_routes.router)
app.include_router(settings_routes.router)
app.include_router(env_config_routes.router)


# audit M-9: response security headers (defence-in-depth behind React's escaping,
# on an app that renders model-produced text throughout). CSRF still rests on the
# SameSite=strict session cookie -- these add the layers a grep found entirely
# missing. style-src allows 'unsafe-inline' because the Vite/React build emits
# inline styles; connect-src 'self' covers the same-origin REST + WebSocket.
_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)


from starlette.responses import JSONResponse as _JSONResponse
from datetime import UTC
from datetime import datetime as _dt


@app.middleware("http")
async def _body_size_limit(request, call_next):
    # audit M-13: reject an over-large request by its Content-Length before the
    # body is read, so no endpoint (uploads OR unbounded JSON goal/message/
    # attachments) can be handed an arbitrarily large body. The ceiling is sized
    # for the maximum legitimate upload batch; JSON bodies sit far below it.
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > REQUEST_BODY_MAX_BYTES:
                return _JSONResponse(
                    {"detail": f"request body exceeds {REQUEST_BODY_MAX_BYTES // (1024*1024)}MB"},
                    status_code=413,
                )
        except ValueError:
            return _JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
    elif "chunked" in request.headers.get("transfer-encoding", "").lower():
        # A chunked request carries no Content-Length, so the check above sees
        # nothing and the body streams in unbounded -- one request can push this
        # single-process app into swap or OOM.
        #
        # Buffer it ourselves up to the same ceiling and replay it downstream.
        # An earlier attempt raised from inside a wrapped receive() instead;
        # that breaks the ASGI contract -- the exception surfaces from anyio as
        # "object _BodyTooLarge can't be used in 'await' expression" and takes
        # 30 unrelated tests with it. Buffering keeps the contract intact, and
        # the memory it costs is bounded by the cap, which is the whole point.
        body = b""
        while True:
            message = await request.receive()
            if message.get("type") == "http.disconnect":
                return _JSONResponse({"detail": "client disconnected"}, status_code=400)
            body += message.get("body", b"")
            if len(body) > REQUEST_BODY_MAX_BYTES:
                return _JSONResponse(
                    {"detail": f"request body exceeds {REQUEST_BODY_MAX_BYTES // (1024*1024)}MB"},
                    status_code=413,
                )
            if not message.get("more_body", False):
                break

        async def _replay() -> dict:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay

    return await call_next(request)


@app.middleware("http")
async def _security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", _CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    return response


# ---------------------------------------------------------------------------
# Auth -- username/password + TOTP 2FA (agent/auth.py). Every route below
# this point that touches a repo-scoped resource calls check_repo_access
# explicitly once `repo` is known (never uniform enough in shape -- query
# param, request body field, or an existing task/session's own stored repo
# -- for one FastAPI dependency to cover safely). Analytics and Model
# Configuration are admin-only outright (require_admin), not repo-scoped --
# they're operator/global concerns (aggregate spend across every project,
# which LLM model each pinned role uses), not something a restricted
# per-project account should see or change.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str
    password: str


class Verify2FARequest(BaseModel):
    temp_token: str
    code: str


class Setup2FARequest(BaseModel):
    password: str | None = None


class Confirm2FARequest(BaseModel):
    code: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class ForgotPasswordRequest(BaseModel):
    email: str


class ResetPasswordRequest(BaseModel):
    email: str
    code: str
    new_password: str


class CreateUserRequest(BaseModel):
    email: str
    password: str
    role: str
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool = False
    auto_approve_repos: list[str] | None = None


class UpdateAutoApproveRequest(BaseModel):
    auto_approve_commands: bool
    # Which projects it covers. Required when turning it ON: a switch whose
    # blast radius nobody chose should not be the widest one.
    repos: list[str] | None = None


class UpdateUserAccessRequest(BaseModel):
    allowed_repos: list[str] | None = None
    auto_approve_commands: bool | None = None
    auto_approve_repos: list[str] | None = None


def _validated_auto_repos(target: User, repos: list[str] | None, *, turning_on: bool) -> list[str] | None:
    """The projects an auto-approve switch may cover, or None to leave the
    stored scope alone.

    Turning it ON must name projects. The alternative -- an empty or absent
    list meaning "everywhere" -- is exactly the inheritance this scoping
    exists to stop: a second account handed the switch would silently get it
    for production as well as for the sandbox it was meant for.
    """
    if repos is None:
        if turning_on and not (target.auto_approve_repos or []):
            raise HTTPException(400, (
                "auto mode needs the projects it covers -- send `repos` with at least one, "
                "so turning it on cannot quietly mean every project"))
        return None
    unknown = [r for r in repos if r not in PROJECTS]
    if unknown:
        raise HTTPException(400, f"unknown project(s): {', '.join(sorted(unknown))}")
    denied = [r for r in repos if not target.can_access(r)]
    if denied:
        raise HTTPException(403, f"{target.email} has no access to: {', '.join(sorted(denied))}")
    if turning_on and not repos:
        raise HTTPException(400, "auto mode with no projects does nothing -- name at least one")
    return repos


def _user_public(user: User) -> dict:
    return {
        "id": user.id, "email": user.email, "role": user.role,
        "allowed_repos": user.allowed_repos, "totp_enabled": user.totp_enabled,
        "must_change_password": user.must_change_password,
        "require_totp_setup": user.role == "admin" and not user.totp_enabled,
        "auto_approve_commands": user.auto_approve_commands,
        "auto_approve_repos": user.auto_approve_repos or [],
        "require_merge_review": user.require_merge_review,
        # None until the account picks one; the frontend maps that to the
        # default rather than the server writing the default into every row.
        "theme": user.theme or auth.DEFAULT_THEME,
    }


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE_NAME, token, max_age=auth.SESSION_TTL_SECONDS,
        httponly=True, samesite="strict", secure=True, path="/",
    )
# Both live in agent/auth.py now: a route module under agent/routers/ cannot
# import them from here without making the import a cycle. Re-exported rather
# than redefined so require_full_auth stays the SAME object -- the route
# inventory identifies a guard by its __name__, and every test that overrides
# it does so through app.dependency_overrides, which is keyed by identity.
_forced_screen_block = auth.forced_screen_block
require_full_auth = auth.require_full_auth


# audit M-32: asyncio keeps only a WEAK reference to a bare create_task, so a
# fire-and-forget background refresh could be garbage-collected mid-run and
# silently never happen. Hold a strong reference until the task finishes, and
# log any exception it raised (bare create_task also swallows those).
_background_tasks = live_state.background_tasks   # see agent/live_state.py


def _spawn_background(coro, label: str) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)

    def _done(t: asyncio.Task) -> None:
        _background_tasks.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning("background task %s failed: %r", label, t.exception())

    task.add_done_callback(_done)


@app.get("/api/health")
async def health():
    """Can this process do its job right now, and if not, which dependency is
    missing? Postgres, the router, the sandbox image, the review secret --
    each checked for real, none of them a model call (see agent/health.py).

    Public on purpose: a monitoring box, or a second person with curl, has no
    session. Nothing here is a secret -- a configured secret reports `true`,
    never its value, and the payload carries how many projects are onboarded
    but never which, since a private repo's name is the one thing in it that
    describes its owner rather than this process.

    503 when any check fails, so a probe that only reads the status code is
    still correct. There was no health route at all until 2026-09-11: every
    restart check in this repo's own history curled /api/health and got the
    SPA's index.html with a 200, which proved only that uvicorn was serving
    static files.
    """
    # auth_pool is the same Postgres this deployment keeps everything in, and
    # it is a real pool with a liveness check on checkout -- so one SELECT 1
    # through it answers for the checkpointer and store too.
    payload = await health_checks.collect(
        getattr(app.state, "auth_pool", None), config.router_base_url, PROJECTS,
    )
    return _JSONResponse(payload, status_code=200 if payload["ok"] else 503)


@app.post("/api/auth/login")
async def login(req: LoginRequest, response: Response, request: Request):
    rate_limit.check_rate_limit(request, "login")  # audit H-7
    row = await auth.get_user_by_email(app.state.auth_pool, req.email.strip().lower())
    # audit M-1: run argon2 on both branches so an unknown email takes the same
    # time as a real one (no user-enumeration timing oracle).
    if not row:
        auth.verify_password_absent()
        raise HTTPException(401, "invalid email or password")
    if not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(401, "invalid email or password")
    rate_limit.clear_rate_limit(request, "login")
    if row["totp_enabled"]:
        temp_token = await auth.create_pending_2fa(app.state.auth_pool, row["id"])
        return {"requires_2fa": True, "temp_token": temp_token}
    token = await auth.create_session(app.state.auth_pool, row["id"])
    _set_session_cookie(response, token)
    return {"requires_2fa": False, "user": _user_public(auth._row_to_user(row))}


@app.post("/api/auth/2fa/verify")
async def verify_2fa(req: Verify2FARequest, response: Response, request: Request):
    rate_limit.check_rate_limit(request, "verify-2fa")  # audit H-7
    pending = await auth.resolve_pending_2fa(app.state.auth_pool, req.temp_token)
    if not pending:
        raise HTTPException(401, "2FA challenge expired -- log in again")
    ok = await auth.verify_totp_or_recovery(app.state.auth_pool, config, pending["user_id"], req.code.strip())
    if not ok:
        raise HTTPException(400, "invalid code")
    rate_limit.clear_rate_limit(request, "verify-2fa")
    token = await auth.create_session(app.state.auth_pool, pending["user_id"])
    _set_session_cookie(response, token)
    row = await auth.get_user_by_id(app.state.auth_pool, pending["user_id"])
    return {"user": _user_public(auth._row_to_user(row))}


@app.post("/api/auth/logout")
async def logout(response: Response, agent_session: str | None = Cookie(default=None)):
    if agent_session:
        await auth.revoke_session(app.state.auth_pool, agent_session)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"ok": True}


@app.post("/api/auth/forgot-password")
async def forgot_password(req: ForgotPasswordRequest, request: Request):
    rate_limit.check_rate_limit(request, "reset-request")  # audit H-7
    # Always {"ok": true} regardless of whether the email matches a real
    # account -- auth.request_password_reset itself silently no-ops for an
    # unknown email; the point is not letting the response tell an attacker
    # which emails are registered users.
    try:
        await auth.request_password_reset(app.state.auth_pool, config, req.email)
    except Exception:  # noqa: BLE001 -- an SMTP hiccup must not turn into "this email doesn't exist" info leakage either
        logger.exception("password reset email failed to send for %s", req.email)
    return {"ok": True}


@app.post("/api/auth/reset-password")
async def reset_password_endpoint(req: ResetPasswordRequest, request: Request):
    rate_limit.check_rate_limit(request, "reset-password")  # audit H-7
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    ok = await auth.reset_password(app.state.auth_pool, req.email, req.code, req.new_password)
    if not ok:
        raise HTTPException(400, "invalid or expired code")
    return {"ok": True}


@app.get("/api/auth/me")
async def get_me(user: User = Depends(auth.get_current_user)):
    return _user_public(user)


@app.post("/api/auth/change-password")
async def change_password_endpoint(req: ChangePasswordRequest, user: User = Depends(auth.get_current_user)):
    row = await auth.get_user_by_id(app.state.auth_pool, user.id)
    if not auth.verify_password(req.current_password, row["password_hash"]):
        raise HTTPException(401, "current password is incorrect")
    error = auth.validate_password_strength(req.new_password)
    if error:
        raise HTTPException(400, error)
    await auth.change_password(app.state.auth_pool, user.id, req.new_password)
    return {"ok": True}


@app.post("/api/auth/2fa/setup")
async def setup_2fa(req: Setup2FARequest = Setup2FARequest(), user: User = Depends(auth.get_current_user)):
    # audit H-3: start_totp_setup clears totp_enabled as it writes the new
    # secret, so an attacker with a live session could silently DISABLE 2FA by
    # hitting this endpoint -- bypassing /2fa/disable, which explicitly refuses
    # for admins. Re-authenticate with the password before re-initiating setup
    # when 2FA is already enabled. First-time setup (2FA off) needs no password:
    # the session already proves who they are, and there is nothing to protect.
    if user.totp_enabled:
        row = await auth.get_user_by_id(app.state.auth_pool, user.id)
        if not req.password or not auth.verify_password(req.password, row["password_hash"]):
            raise HTTPException(403, "current password required to re-initialize 2FA")
    secret, uri = await auth.start_totp_setup(app.state.auth_pool, config, user.id)
    return {"secret": secret, "uri": uri}


@app.post("/api/auth/2fa/confirm")
async def confirm_2fa(req: Confirm2FARequest, user: User = Depends(auth.get_current_user)):
    codes = await auth.confirm_totp_setup(app.state.auth_pool, config, user.id, req.code.strip())
    return {"recovery_codes": codes}


class Disable2FARequest(BaseModel):
    password: str = ""


@app.post("/api/auth/2fa/disable")
async def disable_2fa_endpoint(req: Disable2FARequest,
                               user: User = Depends(require_full_auth)):
    """Removing a second factor is exactly the action a stolen session would
    want, so it is not something a session alone should authorise.

    Two changes over the original: require_full_auth rather than
    get_current_user (a half-authenticated session must not reach this at
    all), and the current password, matching what /2fa/setup already demands
    to RE-initialise. Enabling 2FA needs no password because the session
    already proves identity and there is nothing yet to protect; disabling it
    destroys a protection, which is the asymmetry.
    """
    if user.role == "admin":
        raise HTTPException(403, "2FA cannot be disabled on the admin account")
    row = await auth.get_user_by_id(app.state.auth_pool, user.id)
    if not req.password or not auth.verify_password(req.password, row["password_hash"]):
        raise HTTPException(403, "current password required to disable 2FA")
    await auth.disable_totp(app.state.auth_pool, user.id)
    return {"ok": True}


def _audit_store():
    """This module's caller-side wrapper around routers.audit_store.

    Two copies of this existed once the first seams moved out -- one here
    reading `app.state` directly, one there taking a request -- which is one
    definition of "where does an audit write get its store" too many. The
    router package owns it; this passes the app in so the routes still in
    this file read it the same way.
    """
    return _routers_audit_store(_AppShim)


# ---------------------------------------------------------------------------
# GitHub integration: settings, inbox, approve links, poller
# (agent/github_settings.py, agent/github_inbox.py)
# ---------------------------------------------------------------------------


@app.get("/api/github/inbox")
async def github_inbox_list(repo: str | None = None, user: User = Depends(require_full_auth)):
    repos = [repo] if repo else [r for r in PROJECTS if user.can_access(r)]
    if repo:
        check_repo_access(user, repo)
    items = []
    for r in repos:
        items.extend((await github_inbox.list_items(app.state.store, r)).values())
    items.sort(key=lambda i: i.get("updated_at", 0), reverse=True)
    return {"items": items, "last_poll": _github_last_poll}


class InboxActionRequest(BaseModel):
    days: float | None = None    # snooze length


async def _github_open_auto_count(repo: str) -> int:
    """Auto-created tasks that are still running or parked on a human."""
    n = 0
    for it in await recent_items(app.state.store, ("tasks", repo), 100):
        v = it.value
        if v.get("origin") == "github" and v.get("status") in ("running", "escalated", "awaiting_approval", "awaiting_merge"):
            n += 1
    return n


# The statuses that mean an item is still being handled -- which is NOT the
# same as "a task is running right now", and getting that wrong is what put a
# finished piece of work back in the inbox asking to be done again.
#
#   running / queued / awaiting_*   -- in flight
#   escalated                       -- in the operator's list with a Resume
#                                      button; re-proposing underneath it
#                                      would queue the same work twice
#   done                            -- FINISHED. The fix is merged, or sitting
#                                      in a pull request waiting for a human.
#                                      The alert stays open on GitHub until
#                                      the scanner re-runs and says otherwise,
#                                      and that delay is not a reason to ask
#                                      for the work again.
#
# What is left -- stopped, error, or an id that no longer exists -- is a task
# that ended without delivering, and that is the only case where the item
# genuinely needs to go back in the queue.
_TASK_HANDLED_STATUSES = (
    "running", "queued", "awaiting_approval", "awaiting_merge", "escalated", "done",
)


async def _github_live_tasks(repo: str) -> set[str]:
    """Which of this project's tasks still count as handling their item.

    github_inbox.decide uses it to decide whether a `task_created` item should
    go back to the queue. Returning an empty set is a real answer ("nothing is
    handling anything"); the inbox only keeps items stuck when it is handed
    None, which is why a failure here re-raises rather than pretending.
    """
    live = set()
    for it in await recent_items(app.state.store, ("tasks", repo), 100):
        v = it.value
        if v.get("status") in _TASK_HANDLED_STATUSES:
            live.add(v.get("task_id") or it.key)
    return live


async def _github_create_task(repo: str, goal: str, budget: float, route: str) -> str:
    """A task GitHub asked for, not a person.

    ONE invariant, and it is the one that matters: merge review is always
    required, whatever any user preference says. Nothing an inbox task does
    reaches the default branch without the operator approving the merge. Auto
    inbox + merge-review-off is what would turn this into an unattended merge
    bot; the README promises Auto "keeps the operator's final merge approval",
    and this line is where that promise is kept.

    Auto-approve of gated file/shell actions, by contrast, now follows the
    operator's own per-project switch (auth.repo_auto_approves). It used to be
    hard-coded False here on the reasoning that nobody typed these goals --
    which sounded right and worked badly. Observed 2026-09-13 on Dependabot
    alert #2, a CRITICAL Next.js RCE the inbox started by itself: the task
    parked at awaiting_approval on
    `"eslint-config-next": "16.2.12" -> "16.3.5"`, because editing
    package.json trips the sensitive-path gate. A dependency bump touches the
    manifest, the lockfile and sometimes a workflow, so it asks once per file
    -- and the operator had Auto on for this very project. A security fix
    that cannot change a version string unattended is not safer, it is just
    slower to land, and the gate that actually guards the repo (merge review)
    is untouched by this.

    Scoped to admin accounts and to projects that account listed, so the
    switch still cannot be widened by a non-admin preference, and it fails
    closed if the accounts cannot be read.
    """
    auto = await auth.repo_auto_approves(app.state.auth_pool, repo)
    out = await _start_task(
        goal, repo, budget, route,
        auto_approve_commands=auto,
        # Never from a preference. See this function's docstring.
        require_merge_review=True,
        # Nobody typed this goal and nobody is watching it start, so it gets
        # the narrowest scope there is: its own repository. Reading another
        # project is for a person asking for it.
        reference_repos=[],
        origin="github",
    )
    return out["task_id"]


async def _github_notify(text: str, repo: str) -> None:
    settings = github_settings.current()
    if settings["notify"].get("telegram", True):
        await notify_operators(app.state.auth_pool, text, repo)
    if settings["notify"].get("email"):
        to = settings["notify"].get("email_to") or config.admin_email
        try:
            await send_plain_email(config, to, f"[Tektonix] GitHub inbox: {repo}", text)
        except Exception:  # noqa: BLE001 -- best-effort, like every alert
            logger.exception("github inbox: email to %s failed", to)


async def _github_act(repo: str, key: str, action: str, *, nonce: str | None = None,
                      days: float | None = None, actor: str = "signed link") -> dict:
    """Approve / dismiss / snooze one inbox item. `nonce` is set when the
    request came through a signed link and must match the item's current
    nonce, which is what makes a link single-use.

    `actor` is who the audit log will name. It defaults to the link because
    that is the honest answer for the path with no session behind it:
    clicking an approve link in Telegram starts a real task, and the log
    would otherwise show a task appearing with nobody having started it."""
    items = await github_inbox.list_items(app.state.store, repo)
    item = items.get(key)
    if not item:
        raise HTTPException(404, "that item is no longer in the inbox")
    if nonce is not None and item.get("approval_nonce") != nonce:
        raise HTTPException(409, "this link was already used")
    if action == "approve":
        if item.get("state") == "task_created" and item.get("task_id"):
            return {"ok": True, "already": True, "task_id": item["task_id"], "item": item}
        if item.get("state") not in ("proposed", "snoozed", "seen"):
            raise HTTPException(409, f"item is {item.get('state')}; nothing to approve")
        task_id = await github_inbox.create_task_for_item(item, github_settings.current(), config, _github_create_task)
        item.update({"state": "task_created", "task_id": task_id, "reason": "approved by operator", "approval_nonce": None})
    elif action == "dismiss":
        item.update({"state": "dismissed", "reason": "dismissed by operator", "approval_nonce": None})
    elif action == "snooze":
        until = time.time() + max(0.05, float(days or 1.0)) * 86400
        item.update({"state": "snoozed", "snoozed_until": until, "reason": f"snoozed until {time.strftime('%Y-%m-%d', time.gmtime(until))}"})
    else:
        raise HTTPException(400, "unknown action")
    await github_inbox.put_item(app.state.store, item)
    await audit.record(
        _audit_store(), actor=actor, action=f"inbox.{action}", target=f"{repo}/{key}",
        detail=(item.get("title") or "")[:160],
        extra={"task_id": item.get("task_id")} if item.get("task_id") else None,
    )
    return {"ok": True, "item": item, "task_id": item.get("task_id")}


@app.post("/api/github/inbox/{repo}/{key}/{action}")
async def github_inbox_action(repo: str, key: str, action: str, req: InboxActionRequest | None = None,
                              user: User = Depends(require_full_auth)):
    if repo not in PROJECTS:
        raise HTTPException(404, "unknown repo")
    check_repo_access(user, repo)
    return await _github_act(repo, key, action, days=(req.days if req else None), actor=user.email)


@app.post("/api/github/poll")
async def github_poll_now(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    return {"results": await _github_poll_once()}


_APPROVE_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tektonix · GitHub inbox</title>
<style>body{font:16px/1.5 system-ui,sans-serif;background:#0f1220;color:#e6e8f0;margin:0;padding:24px}
.card{max-width:560px;margin:8vh auto;background:#181c30;border:1px solid #2a3050;border-radius:12px;padding:24px}
h1{font-size:18px;margin:0 0 12px}p{margin:8px 0}.muted{color:#9aa3c0}.err{color:#ff8a8a}
button{font:inherit;font-weight:700;border:0;border-radius:8px;padding:12px 18px;cursor:pointer;margin-top:12px}
.go{background:#3fb950;color:#06210c}.no{background:#2a3050;color:#e6e8f0;margin-left:8px}</style></head>
<body><div class="card">{body}</div></body></html>"""


def _approve_html(body: str) -> Response:
    from fastapi.responses import HTMLResponse
    return HTMLResponse(_APPROVE_PAGE.replace("{body}", body))


# Fixed wording, looked up by reason: nothing an exception carries reaches
# the page (CodeQL py/stack-trace-exposure). verify_approval raises
# ValueError with one of these reason codes as its message.
_LINK_PROBLEMS = {
    "malformed": "this link is malformed",
    "invalid": "this link is not valid for this deployment",
    "expired": "this link has expired; open the GitHub inbox in the dashboard instead",
    "unknown": "this link asks for an unknown action",
}
_ACT_PROBLEMS = {
    404: "that item is no longer in the inbox",
    409: "this link was already used, or the item was already handled",
    0: "the request could not be completed; open the GitHub inbox in the dashboard",
}


def _link_problem(e: ValueError) -> str:
    return _LINK_PROBLEMS.get(str(e), _LINK_PROBLEMS["malformed"])


def _esc(s: str) -> str:
    return html.escape(str(s or ""))


@app.get("/api/github/approve")
async def github_approve_page(t: str = ""):
    """The link from Telegram/email. Shows what would happen and a button;
    the button POSTs. A GET never acts -- messengers fetch links for previews."""
    try:
        data = github_inbox.verify_approval(config, t)
    except ValueError as e:
        return _approve_html(f"<h1>Link problem</h1><p class=err>{_link_problem(e)}</p>")
    items = await github_inbox.list_items(app.state.store, data["r"])
    item = items.get(data["k"])
    if not item:
        return _approve_html("<h1>Gone</h1><p class=muted>That item is no longer in the inbox.</p>")
    if item.get("approval_nonce") != data["n"]:
        state = item.get("state")
        return _approve_html(f"<h1>Already handled</h1><p class=muted>This item is <b>{_esc(state)}</b>"
                             + (f" (task {_esc(item.get('task_id', '')[:8])})" if item.get("task_id") else "") + ".</p>")
    verb = "Start a task for" if data["a"] == "approve" else "Dismiss"
    budget = github_settings.project_settings(github_settings.current(), data["r"])["budget_usd"]
    body = (f"<h1>{verb} this?</h1><p><b>{_esc(item.get('title'))}</b></p><p class=muted>{_esc(item.get('summary'))}</p>"
            f"<p class=muted>{_esc(data['r'])} · {_esc(item.get('kind'))}"
            + (f" · budget ${budget:.2f}, through the normal review gate" if data["a"] == "approve" else "") + "</p>"
            # action="" posts back to this same URL, whatever prefix nginx serves it under.
            f"<form method=post action=\"\"><input type=hidden name=t value=\"{_esc(t)}\">"
            f"<button class=go type=submit>{'Approve and start' if data['a'] == 'approve' else 'Dismiss'}</button></form>")
    return _approve_html(body)


@app.post("/api/github/approve")
async def github_approve_submit(request: Request):
    form = await request.form()
    t = str(form.get("t") or "")
    try:
        data = github_inbox.verify_approval(config, t)
        result = await _github_act(data["r"], data["k"], data["a"], nonce=data["n"])
    except ValueError as e:
        return _approve_html(f"<h1>Link problem</h1><p class=err>{_link_problem(e)}</p>")
    except HTTPException as e:
        return _approve_html(f"<h1>Not done</h1><p class=err>{_ACT_PROBLEMS.get(e.status_code, _ACT_PROBLEMS[0])}</p>")
    if data["a"] == "approve":
        tid = result.get("task_id") or ""
        return _approve_html(f"<h1>Task started</h1><p>{_esc(result['item'].get('title'))}</p>"
                             f"<p class=muted>Task {_esc(tid[:8])} is running on {_esc(data['r'])}. It will ask for your merge approval when the review is ready.</p>")
    return _approve_html(f"<h1>Dismissed</h1><p class=muted>{_esc(result['item'].get('title'))}</p>")


# On app.state, not a module global: the GitHub settings route that sets it
# lives in agent/routers/settings.py now, and a router cannot import back
# from this module without making the import a cycle. The poller below still
# reaches it by the same name.
_github_poll_wake = asyncio.Event()
app.state.github_poll_wake = _github_poll_wake
# On app.state for the same reason as github_poll_wake: the inbox route that
# reads it lives in agent/routers/github.py now, and a router cannot import
# back from this module without making the import a cycle. The poller below
# writes both.
_github_last_poll: dict | None = None
app.state.github_last_poll = None


async def _github_poll_once() -> list[dict]:
    global _github_last_poll
    results = await github_inbox.poll_all(
        app.state.store, config,
        create_task=_github_create_task, notify=_github_notify, open_auto_count=_github_open_auto_count,
        live_tasks=_github_live_tasks,
    )
    _github_last_poll = {"at": time.time(), "results": results}
    app.state.github_last_poll = _github_last_poll
    return results


# The poller stays in this module: it reaches _start_task, the notifier and
# the auto-count, which is most of the server. What the /api/github/poll
# route needs is only the ability to TRIGGER it, so the function is put where
# a router can find it rather than the router pulling the machinery across.
app.state.github_poll_once = _github_poll_once
# Same reasoning, narrower interface: approving an inbox item starts a real
# task, and task creation is _start_task and everything under it. The inbox
# routes get the one function rather than the machinery.
app.state.github_create_task = _github_create_task


async def _github_poll_loop(startup_delay: float = 20.0) -> None:
    """Runs forever; polls every poll_interval_min while any project has a
    source switched on, and wakes early when settings change."""
    await asyncio.sleep(startup_delay)
    while True:
        settings = github_settings.current()
        interval = max(2, int(settings.get("poll_interval_min", 10))) * 60
        if github_settings.enabled_projects(settings):
            try:
                await _github_poll_once()
            except Exception:  # noqa: BLE001 -- the loop must survive anything
                logger.exception("github inbox: poll pass failed")
        try:
            await asyncio.wait_for(_github_poll_wake.wait(), timeout=interval)
        except TimeoutError:
            pass
        _github_poll_wake.clear()


class RemoveProjectRequest(BaseModel):
    # What to do with everything the agent LEARNED about this project.
    memory: Literal["archive", "delete"] = "archive"
    # And what to do with the checkout. `keep` is the default and the rule the
    # removal module is built around; `delete` is for a repository Tektonix
    # cloned by itself and the operator never wanted, and is refused unless
    # the server can show that nothing would be lost by it.
    files: Literal["keep", "delete"] = "keep"


class UpdateThemeRequest(BaseModel):
    theme: str


class UpdateMergeReviewRequest(BaseModel):
    require_merge_review: bool


@app.post("/api/auth/me/merge-review")
async def set_own_merge_review(req: UpdateMergeReviewRequest, user: User = Depends(require_full_auth)):
    """Self-service for the same reason auto-approve is: turning the final
    look OFF removes a review the operator was doing for their own benefit,
    not a safety property someone else depends on -- the independent review
    service still gates every merge regardless. Captured onto each task at
    creation, so flipping this never changes a task already in flight."""
    await auth.update_require_merge_review(app.state.auth_pool, user.id, req.require_merge_review)
    await audit.record(_audit_store(), actor=user.email, action="settings.merge_review",
                       target=user.email,
                       detail="on" if req.require_merge_review else "off")
    return {"ok": True, "require_merge_review": req.require_merge_review}


@app.post("/api/auth/me/theme")
async def set_own_theme(req: UpdateThemeRequest, user: User = Depends(require_full_auth)):
    """The account's colour scheme. Self-service and unaudited: it grants
    nothing and reveals nothing, and an audit line per colour change would
    bury the entries that matter."""
    try:
        await auth.update_theme(app.state.auth_pool, user.id, req.theme)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "theme": req.theme}


@app.post("/api/auth/me/auto-approve")
async def set_own_auto_approve(req: UpdateAutoApproveRequest, user: User = Depends(require_full_auth)):
    """Self-service, deliberately not admin-only: this grants no capability
    the account doesn't already have -- every action it stops prompting for
    could be approved by hand, one at a time, by this same user today. It
    only removes the clicking. The destructive-command subset stays gated no
    matter what this is set to (see deep_agent.py's interrupt_on_for), which
    is what makes self-service reasonable rather than a way to switch off
    the safety net.
    """
    repos = _validated_auto_repos(user, req.repos, turning_on=req.auto_approve_commands)
    await auth.update_auto_approve(app.state.auth_pool, user.id, req.auto_approve_commands, repos)
    await audit.record(_audit_store(), actor=user.email, action="settings.auto_approve",
                       target=user.email,
                       detail=("on for " + ", ".join(repos) if req.auto_approve_commands and repos
                               else "on" if req.auto_approve_commands else "off"))
    return {"ok": True, "auto_approve_commands": req.auto_approve_commands,
            "auto_approve_repos": repos if repos is not None else (user.auto_approve_repos or [])}


class TelegramSettingsRequest(BaseModel):
    bot_token: str | None = None  # None/empty clears; masked sentinel keeps existing
    chat_id: str | None = None


@app.get("/api/auth/me/telegram")
async def get_telegram_settings_endpoint(user: User = Depends(require_full_auth)):
    """Masked: reports whether a token is configured, never the token."""
    return await auth.get_telegram_settings(app.state.auth_pool, user.id)


@app.post("/api/auth/me/telegram")
async def set_telegram_settings_endpoint(req: TelegramSettingsRequest, user: User = Depends(require_full_auth)):
    token = (req.bot_token or "").strip()
    chat_id = (req.chat_id or "").strip()
    if token == "__unchanged__":
        # The Settings page never receives the stored token back (masked
        # endpoint above), so "save" with an untouched token field must not
        # blank a working credential -- the sentinel keeps it.
        existing = await auth.get_telegram_settings(app.state.auth_pool, user.id)
        if existing["configured"]:
            await auth.update_telegram_chat_only(app.state.auth_pool, user.id, chat_id or None)
            return await auth.get_telegram_settings(app.state.auth_pool, user.id)
        token = ""
    await auth.update_telegram(app.state.auth_pool, user.id, token or None, chat_id or None)
    return await auth.get_telegram_settings(app.state.auth_pool, user.id)


@app.post("/api/auth/me/telegram/test")
async def test_telegram_endpoint(user: User = Depends(require_full_auth)):
    """Sends a real message to THIS user's configured chat so the operator can
    verify the token/chat pair before trusting it with real alerts."""
    row = await auth.get_telegram_raw(app.state.auth_pool, user.id)
    if not row:
        raise HTTPException(400, "telegram is not configured -- save a bot token and chat id first")
    token, chat_id = row
    ok = await send_telegram(token, chat_id, task_alert(
        "done", "tektonix", "Test alert from the dashboard settings page",
        0.00, "If you can read this, task alerts will reach you here."))
    if not ok:
        raise HTTPException(502, "telegram rejected the send -- check the bot token and chat id (and that you have messaged the bot once)")
    return {"ok": True}


@app.get("/api/auth/users")
async def list_users_endpoint(user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    rows = await auth.list_users(app.state.auth_pool)
    return [_user_public(auth._row_to_user(r)) for r in rows]


@app.post("/api/auth/users", status_code=201)
async def create_user_endpoint(req: CreateUserRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "role must be 'admin' or 'user'")
    # audit H7: can_access() treats allowed_repos=None as UNRESTRICTED for any
    # role, and this field defaults to None -- so POST with {"role": "user"}
    # and no repo list minted an account that could reach every project. The
    # sibling PATCH endpoint already rejects this; the create path did not.
    if req.role != "admin" and req.allowed_repos is None:
        raise HTTPException(
            400, "a non-admin user needs an explicit allowed_repos list "
                 "(use [] for no access); omitting it would grant every repo")
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    error = auth.validate_password_strength(req.password)
    if error:
        raise HTTPException(400, error)
    if await auth.get_user_by_email(app.state.auth_pool, req.email.strip().lower()):
        raise HTTPException(409, "a user with this email already exists")
    row = await auth.create_user(
        app.state.auth_pool, req.email.strip().lower(), req.password, req.role, req.allowed_repos,
        must_change_password=True,
    )
    if req.auto_approve_commands:
        # A brand-new account cannot be handed a blanket switch: it is scoped
        # to the projects it was just granted, and an admin account (whose
        # allowed_repos is None, meaning everything) must name them.
        scope = req.auto_approve_repos if req.auto_approve_repos is not None else req.allowed_repos
        if not scope:
            raise HTTPException(400, (
                "auto mode for a new account needs the projects it covers -- send "
                "`auto_approve_repos`, or create the account with `allowed_repos`"))
        unknown = [r for r in scope if r not in PROJECTS]
        if unknown:
            raise HTTPException(400, f"unknown project(s): {', '.join(sorted(unknown))}")
        await auth.update_auto_approve(app.state.auth_pool, row["id"], True, list(scope))
        row = {**row, "auto_approve_commands": True, "auto_approve_repos": sorted(set(scope))}
        await audit.record(_audit_store(), actor=user.email, action="settings.auto_approve",
                           target=row["email"],
                           detail="on at account creation for " + ", ".join(sorted(set(scope))))
    return _user_public(auth._row_to_user(row))


@app.patch("/api/auth/users/{user_id}")
async def update_user_access_endpoint(user_id: int, req: UpdateUserAccessRequest, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if req.allowed_repos:
        for r in req.allowed_repos:
            if r not in PROJECTS:
                raise HTTPException(400, f"unknown repo {r!r}")
    target = await auth.get_user_by_id(app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    # allowed_repos is meaningless for admin (always full access already);
    # auto_approve_commands is orthogonal to repo scope and applies to any
    # role, admin included -- it's the one most likely to want it.
    if req.allowed_repos is not None:
        if target["role"] == "admin":
            raise HTTPException(400, "the admin account always has full access")
        await auth.update_user_access(app.state.auth_pool, user_id, req.allowed_repos)
    if req.auto_approve_commands is not None or req.auto_approve_repos is not None:
        target_user = auth._row_to_user(target)
        enabled = (req.auto_approve_commands if req.auto_approve_commands is not None
                   else target_user.auto_approve_commands)
        repos = _validated_auto_repos(target_user, req.auto_approve_repos, turning_on=enabled)
        await auth.update_auto_approve(app.state.auth_pool, user_id, enabled, repos)
        # An admin granting someone else the right to skip prompts is the
        # single most consequential thing on the Users panel, and the person
        # it is granted to has no other way to learn who did it.
        await audit.record(
            app.state.store, actor=user.email,
            action="settings.auto_approve_repos" if req.auto_approve_repos is not None
            else "settings.auto_approve",
            target=target["email"],
            detail=("on for " + ", ".join(repos)) if enabled and repos
            else ("on" if enabled else "off"))
    return {"ok": True}


@app.delete("/api/auth/users/{user_id}")
async def delete_user_endpoint(user_id: int, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    if user_id == user.id:
        raise HTTPException(400, "cannot delete your own account")
    target = await auth.get_user_by_id(app.state.auth_pool, user_id)
    if not target:
        raise HTTPException(404, "user not found")
    if target["role"] == "admin":
        raise HTTPException(400, "cannot delete the admin account")
    await auth.delete_user(app.state.auth_pool, user_id)
    return {"ok": True}


UPLOADS_DIRNAME = ".uploads"
UPLOAD_MAX_BYTES = 25 * 1024 * 1024
# audit M-13: bound the number of files per upload and the absolute request
# body. Without these, `files: list[UploadFile]` was unbounded and a Content-
# Length ceiling existed nowhere (so JSON bodies were unbounded too).
UPLOAD_MAX_FILES = 20
REQUEST_BODY_MAX_BYTES = UPLOAD_MAX_FILES * UPLOAD_MAX_BYTES + 8 * 1024 * 1024
UPLOAD_KINDS = {
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".webp": "image", ".gif": "image",
    ".pdf": "pdf",
    ".csv": "text", ".tsv": "text", ".txt": "text", ".json": "text", ".md": "text",
    ".xlsx": "sheet", ".xls": "sheet",
}

_GIT_EXCLUDES_PATH = Path(__file__).resolve().parent.parent / ".agent-git-excludes"


def _ensure_uploads_ignored(repo_root: str) -> None:
    """Uploads live inside the sandbox repo (so the agent's /workspace tools
    reach them) but must never enter a commit/review. A repo-external git
    excludes file (core.excludesFile) keeps them invisible to git without
    touching the project's own .gitignore -- zero diff, nothing for the
    reviewer to see."""
    import subprocess
    if not _GIT_EXCLUDES_PATH.exists():
        _GIT_EXCLUDES_PATH.write_text(f"{UPLOADS_DIRNAME}/\n")
    subprocess.run(["git", "-C", repo_root, "config", "core.excludesFile", str(_GIT_EXCLUDES_PATH)], check=False)


@app.post("/api/uploads")
async def upload_files(repo: str, files: list[UploadFile] = File(...), user: User = Depends(require_full_auth)):
    """Store operator attachments in the repo's sandbox under .uploads/<batch>/
    and return a manifest for the task goal. PDFs get a sibling .txt with the
    extracted text so the (text-only) coding models can read them directly;
    images are consumed via the agent's describe_image tool."""
    if repo not in PROJECTS:
        raise HTTPException(404, f"unknown repo {repo!r}")
    check_repo_access(user, repo)
    repo_root = PROJECTS[repo]["sandbox"]
    # audit M-34: _ensure_uploads_ignored is synchronous (Path.exists,
    # write_text, subprocess.run) -- run it off the event loop.
    await asyncio.to_thread(_ensure_uploads_ignored, repo_root)
    batch = uuid.uuid4().hex[:8]
    batch_dir = Path(repo_root) / UPLOADS_DIRNAME / batch
    batch_dir.mkdir(parents=True, exist_ok=True)

    # audit M-13: bound the file count before touching any of them.
    if len(files) > UPLOAD_MAX_FILES:
        raise HTTPException(413, f"too many files ({len(files)}); limit is {UPLOAD_MAX_FILES}")

    manifest = []
    for f in files:
        name = Path(f.filename or "file").name  # strip any path components
        ext = Path(name).suffix.lower()
        kind = UPLOAD_KINDS.get(ext)
        if kind is None:
            raise HTTPException(415, f"unsupported file type {ext!r} ({name})")
        # audit M-13: stream to disk in chunks with a running counter, aborting
        # (and deleting the partial file) the moment it exceeds the cap -- the
        # old `await f.read()` materialized the whole file in memory first.
        dest = batch_dir / name
        written = 0
        with dest.open("wb") as out:
            while True:
                chunk = await f.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > UPLOAD_MAX_BYTES:
                    out.close()
                    dest.unlink(missing_ok=True)
                    raise HTTPException(413, f"{name} exceeds {UPLOAD_MAX_BYTES // (1024*1024)}MB")
                out.write(chunk)
        rel = f"{UPLOADS_DIRNAME}/{batch}/{name}"
        entry = {"path": rel, "kind": kind, "bytes": written}
        if kind == "pdf":
            # audit M-34: pypdf full-text extraction is CPU-bound for seconds on
            # a large PDF -- run it in a thread so it doesn't stall the loop.
            def _extract_pdf(dest_path: str, out_path: str) -> int:
                import pypdf
                reader = pypdf.PdfReader(dest_path)
                text = "\n\n".join((page.extract_text() or "") for page in reader.pages)
                Path(out_path).write_text(text, encoding="utf-8")
                return len(reader.pages)
            try:
                pages = await asyncio.to_thread(_extract_pdf, str(dest), str(batch_dir / f"{name}.txt"))
                entry["extracted_text"] = f"{rel}.txt"
                entry["pages"] = pages
            except Exception as e:  # noqa: BLE001 -- a scanned/encrypted pdf shouldn't fail the upload
                entry["extracted_text"] = None
                logger.info("uploads: text extraction failed for %s: %s", entry.get("name"), e)
                entry["note"] = "text extraction failed -- possibly scanned; no text layer"
        manifest.append(entry)
    return {"repo": repo, "files": manifest}


def _attachments_note(attachments: list[dict]) -> str:
    """The goal suffix that tells the agent what was attached and how to
    consume each kind -- written for the model, not the human."""
    lines = ["", "", "--- ATTACHED FILES (operator-provided, in the repo workspace) ---"]
    for a in attachments:
        kind = a.get("kind")
        path = a.get("path", "(unknown path)")
        if kind == "image":
            lines.append(f"- {path} (image): view it with the describe_image tool -- ask it specific questions if needed.")
        elif kind == "pdf" and a.get("extracted_text"):
            lines.append(f"- {path} (PDF, {a.get('pages', '?')} pages): extracted text at {a.get('extracted_text')} -- read that with your read tool.")
        elif kind == "pdf":
            lines.append(f"- {path} (PDF): no text layer could be extracted ({a.get('note', '')}).")
        else:
            lines.append(f"- {path} ({kind}): read it directly with your read tool (or bash for large/structured files).")
    lines.append("These are reference inputs, not part of the codebase -- never commit them or copy them into the repo.")
    return "\n".join(lines)


class AttachmentEntry(BaseModel):
    # audit M-33: attachments were `list[dict]`, entirely unvalidated, and
    # _attachments_note indexed a['path'] unconditionally -- so {"kind":"image"}
    # (no path) was an unhandled KeyError -> 500, and the raw values landed in
    # the goal text the model reads. A real model rejects a malformed entry at
    # the API boundary with a 422 instead.
    kind: str
    path: str
    pages: int | None = None
    extracted_text: str | None = None
    note: str | None = None


class CreateTaskRequest(BaseModel):
    # audit M-33: goal was accepted empty/whitespace (send_planning_message
    # already rejected that -- the two endpoints were inconsistent), and
    # budget_usd had no bounds so 0 tripped the guard on the first call and a
    # negative value was accepted straight into AgentState.
    # 80k, not 20k: a Build Now hands the whole plan document over as the goal,
    # and a full-frontend restyle plan came in at 20,364 chars on 2026-09-09 --
    # every click returned 422 and the planning view had nowhere to show it.
    # Still bounded; a plan past 80k is a document, not a task.
    goal: str = Field(min_length=1, max_length=80_000)
    repo: str
    budget_usd: float | None = Field(default=None, gt=0, le=1000)
    attachments: list[AttachmentEntry] | None = None  # manifest entries from /api/uploads
    # "auto" decides from category/paths/keywords (agent/frontend_route.py);
    # "frontend"/"general" is the operator overriding that.
    route: Literal["auto", "frontend", "general"] = "auto"


class SendMessageRequest(BaseModel):
    text: str


# Ceiling on a single resume top-up. Not a policy about total spend --
# just a bound on one request, so a typo or a hostile value cannot remove
# the budget ceiling in one call.
_MAX_BUDGET_TOPUP_USD = 100.0


class ResumeTaskRequest(BaseModel):
    additional_budget_usd: float
    message: str | None = None


def _check_budget_topup(delta: float) -> None:
    """Reject a resume top-up outside [0, _MAX_BUDGET_TOPUP_USD]. Zero is a
    valid delta: the dashboard's resume panel only shows the budget field when
    the task is nearly out of money and sends 0 otherwise (a merge failure or
    an operator Stop has nothing to do with cost), and the escalated branch of
    resume_task already words its note for "no budget added". Rejecting 0
    (as this did until 2026-09-09) left every such resume stuck on a 400 with
    no field on screen to fix."""
    if not (0 <= delta <= _MAX_BUDGET_TOPUP_USD):
        raise HTTPException(
            400, f"additional_budget_usd must be between 0 and ${_MAX_BUDGET_TOPUP_USD:.2f}")


class ApprovalRequest(BaseModel):
    decision: Literal["approve", "reject", "respond"]
    message: str | None = None  # only meaningful for a reject -- explains why to the model


class CreatePlanningSessionRequest(BaseModel):
    repo: str
    route: Literal["auto", "frontend", "general"] = "auto"


class PlanningMessageRequest(BaseModel):
    text: str
    attachments: list[dict] | None = None  # manifest entries from /api/uploads


_SUBSCRIBER_QUEUE_MAX = 2000


def _publish(task_id: str, event: dict) -> None:
    # Every entry carries a content-derived id and every event a monotonic
    # seq (agent/log_stream.py). Together they let the browser open its
    # socket before hydrating, buffer what arrives meanwhile, and merge the
    # two sources afterwards without losing or duplicating a line.
    if event.get("execution_log"):
        event["execution_log"] = log_stream.stamp(event["execution_log"])
        # The event counter is per task and lives as long as the task's live
        # log does: evicting one without the other leaks a counter per task
        # for the life of the process.
        _live_log_append(_live_task_log, task_id, event["execution_log"],
                         on_evict=_task_event_seq.forget)
        # ...and the durable copy, batched (see planning_log.Recorder).
        rec = _task_recorders.get(task_id)
        if rec is not None:
            due = False
            for entry in event["execution_log"]:
                due = rec.add(entry) or due
            if due:
                _flush_task_log_bg(rec)
    if event.get("type") != "ping":
        event["seq"] = _task_event_seq.next(task_id)
    for q, _ws in _subscribers.get(task_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # audit M-34: a reader this far behind is effectively gone; drop
            # rather than grow memory without bound. Its socket teardown will
            # remove it shortly.
            logger.warning("dropping event for a stalled task %s subscriber", task_id)


# The outer graph's own AgentState has no `plan`/`current_step_index` keys at
# all -- write_todos (deepagents' own planning tool, living in the inner
# deep-agent thread) is the plan, and `latest_todos` (a plain snapshot copied
# into the outer state at the end of each "work" pass, see work.py) is the
# closest equivalent. Translated here into the PlanStep[] shape the
# frontend's PlanTracker renders. `result`/`verified` have no todo-level
# equivalent in this design (verify_and_ship gates the whole task, not a
# per-step independently-checked claim) -- always False/None; the frontend
# doesn't currently render either field regardless.
_TODO_STATUS_MAP = {"pending": "pending", "in_progress": "in_progress", "completed": "done"}


def _todos_to_plan(todos: list | None) -> list[dict] | None:
    if todos is None:
        return None
    return [
        {
            "id": str(i),
            "description": t.get("content", ""),
            "status": _TODO_STATUS_MAP.get(t.get("status"), "pending"),
            "result": None,
            "verified": False,
        }
        for i, t in enumerate(todos)
    ]


def _state_snapshot_for_frontend(values: dict) -> dict:
    """Used by get_task's REST snapshot (the hydrate path useTaskStream.ts
    calls on every connect/reconnect) -- without this translation, a page
    load/reconnect would show an empty plan until the next live "todos"
    custom event happened to arrive, since the raw checkpoint dict has
    `latest_todos`, not `plan`, and the frontend only reads the latter.
    """
    return {**values, "plan": _todos_to_plan(values.get("latest_todos")), "current_step_index": None}


def _apply_plan_fallback(snapshot: dict | None, meta_value: dict) -> dict | None:
    """The plan strip reads the LIVE mirror when there is one. The checkpoint's
    latest_todos is written only when a work pass RETURNS; the todos handler
    mirrors every todos event into the task meta as it happens. So mid-pass
    the mirror is never older than the checkpoint, and after a pass ends the
    two agree -- there is no moment at which the checkpoint is fresher.

    Two live reports drove this. 2026-08-28: mid-pass the checkpoint had no
    list yet and the strip vanished on every refresh, so the mirror was added
    as a fallback. 2026-09-09: a resumed task's coordinator wrote a new
    3-item list, the strip showed it, and a refresh snapped back to the
    15-item list from the pass before -- because "a checkpointed plan always
    wins" preferred the stale one. Mirror first, checkpoint when there is no
    mirror (tasks from before the mirror existed)."""
    if snapshot is None:
        return None
    mirrored = meta_value.get("latest_todos")
    if mirrored:
        return {**snapshot, "plan": _todos_to_plan(mirrored)}
    return snapshot


def _final_status(values: dict) -> str:
    """Terminal task status once a _stream_graph run's own astream loop
    ends -- "awaiting_approval" is a real third resting state alongside
    escalated/done (see deep_agent.py's INTERRUPT_ON, outer_graph.py's
    _route_after_verify), checked before the escalated/done fallback since
    a task can be both not-escalated and not-done: paused on a human-in-
    the-loop decision.
    """
    if values.get("escalated"):
        return "escalated"
    if values.get("pending_approval"):
        return "awaiting_approval"
    if values.get("pending_merge_approval"):
        # Review READY, merge parked on the operator's final look at the diff.
        return "awaiting_merge"
    return "done"


@contextlib.contextmanager
def _claim_run_slot(registry: dict, key: str, already_running: str):
    """Reserve a slot in a run registry BEFORE the handler's awaits.

    The registries below are plain dicts guarded by `if key in registry:
    raise 409`. That check sat at the top of each handler and the real
    assignment came several awaits later -- and the event loop switches at
    every await, so two concurrent requests could both pass the check and
    both create a driver for the same thread. The unconditional
    `registry.pop(key)` in the driver's finally then orphaned whichever one
    survived.

    A dict write with no await between it and the check IS atomic here, so
    the fix is to reserve immediately: place a None placeholder, then let the
    handler replace it with the real Task. Both consumers of these registries
    do `registry.get(key)` and treat a falsy value as "not running", which is
    exactly right for the reservation window -- there is genuinely nothing to
    cancel yet.

    The slot is released if the handler raises, or if it returns without ever
    assigning a task; otherwise a rejected request would strand the key and
    the task could never be started again.
    """
    if registry.get(key) is not None or key in registry:
        raise HTTPException(409, already_running)
    registry[key] = None
    try:
        yield
    except BaseException:
        registry.pop(key, None)
        raise
    if registry.get(key) is None:
        registry.pop(key, None)


async def _read_task_meta(store, repo: str, task_id: str):
    """The stored meta, or None. Swallows a read failure on purpose: every
    caller here is mirroring display state, and a store hiccup must not break
    the stream it is decorating."""
    try:
        return await store.aget(("tasks", repo), task_id)
    except Exception:  # noqa: BLE001
        logger.exception("task meta read failed for %s", task_id)
        return None


async def write_task_meta(store, repo: str, task_id: str, **updates) -> dict:
    """The single writer for a task's Store record.

    Every caller used to hand-build the entire dict, so a key one site
    remembered and another forgot was silently deleted by the next write.
    That is not hypothetical:

      * audit H-20 -- the error path was a full overwrite carrying neither
        cost_so_far nor escalation_reason, so a failed task erased the spend
        already mirrored mid-pass and reported $0.00 in stats and analytics;
      * the "stopped" path dropped escalation_reason outright, so stopping a
        task that had escalated lost the reason it escalated for.

    Read-merge-write, so unspecified keys are PRESERVED and forgetting one can
    no longer delete it. Pass an explicit None to clear a field deliberately.
    task_id and repo are always stamped -- they identify the record.
    """
    existing = await store.aget(("tasks", repo), task_id)
    record = dict(existing.value) if existing else {}
    record.update(updates)
    record["task_id"] = task_id
    record["repo"] = repo
    record.setdefault("created_at", time.time())
    await store.aput(("tasks", repo), task_id, record)
    return record


async def _stream_graph(task_id: str, repo: str, goal: str, budget_usd: float, graph_input, category: str | None = None,
                        route: str | None = None, route_reason: str | None = None) -> None:
    """Shared by a fresh task (graph_input = the initial state) and a resume
    (graph_input = None, meaning "continue from the last checkpoint" -- the
    standard LangGraph resume pattern). Everything after that point --
    streaming updates out over the WS, updating the Store, closing out
    status -- is identical either way.

    `category` is only ever passed explicitly on a fresh task (create_task
    classifies the goal once, up front). On a resume/approve call, it's left
    None here and recovered from the task's own already-stored meta below --
    a resume must never reclassify or lose the original category.
    """
    store = app.state.store
    graph = app.state.graph

    # audit H-19: read the stored meta ONCE up front and preserve the original
    # created_at across every terminal write below. Previously each write
    # hardcoded time.time(), so a resume reset the timestamp and list_tasks /
    # get_analytics (which sort and bucket by created_at) attributed a task to
    # its completion day instead of its start day.
    existing_meta = await store.aget(("tasks", repo), task_id)
    _existing_val = existing_meta.value if existing_meta else {}
    original_created_at = _existing_val.get("created_at")
    if category is None:
        category = _existing_val.get("category") or "other"
    if route is None:
        route = _existing_val.get("route") or "general"
        route_reason = _existing_val.get("route_reason")
    # metadata/tags -- standard RunnableConfig fields LangChain's tracer
    # automatically attaches to every run generated within this invocation,
    # so a trace is filterable to this specific task in the LangSmith UI
    # rather than only to the project as a whole.
    thread_config = {
        "configurable": {"thread_id": task_id},
        "metadata": {"task_id": task_id, "repo": repo},
        "tags": [repo],
    }
    # From here on every streamed entry is also written down durably.
    _start_task_recorder(task_id, repo)

    # cost_so_far here is Store-only display state -- hardcoding 0.0
    # unconditionally meant a resume briefly showed $0.00 in the
    # sidebar/stats for real work that already cost real money, until the
    # run finished and overwrote it with the true total. graph_input is
    # None specifically on a resume (see this function's own docstring), so
    # that's exactly when to carry the existing total forward instead. Note
    # the outer checkpoint's own cost_so_far is NOT always correct: it only
    # updates when a work pass fully returns, so a task cancelled mid-pass
    # has a stale value here until the CancelledError handler below patches
    # it back to the real total.
    starting_cost = 0.0
    if graph_input is None:
        existing = await graph.aget_state(thread_config)
        if existing and existing.values:
            starting_cost = existing.values.get("cost_so_far", 0.0)

    async def _mark(status: str) -> None:
        """Write this task's status and tell anyone watching."""
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
            status=status, created_at=original_created_at or time.time(),
            cost_so_far=starting_cost, route=route, route_reason=route_reason,
        )
        _publish(task_id, {"type": "status", "status": status})

    # "queued", not "running", while another task holds this project.
    #
    # One task per project is a hard constraint -- they share one worktree,
    # and two of them editing it would interleave their commits (agent/graph.py's
    # project_lock). But the status was written BEFORE the lock was taken, so
    # a queued task was indistinguishable from a working one: the sidebar
    # showed "Running", the log showed nothing, the spend showed nothing, and
    # the only way to tell was to notice it had been like that for a while.
    # With 59 open alerts on one project, that is a state an operator now
    # reaches by doing the obvious thing twice.
    await _mark("queued")

    # Declared here (not just inside the loop below) so the CancelledError
    # handler can always read the latest live-tracked cost, even if
    # cancellation lands before the astream loop yields anything.
    last_meta_cost = starting_cost

    try:
        # The DSN, not pg_dsn: it is what project_lock dispatches on, and on
        # this deployment it is the same Postgres string it always was. So
        # this is still a Postgres advisory lock rather than an object in
        # this process, and a second worker or an overlapping restart still
        # cannot run two tasks on one worktree (agent/graph.py).
        async with project_lock(repo, config.dsn, on_wait=lambda: _mark("queued")):
            # The project is ours now, so this is the first honest moment to
            # say "running". A task that never waited passes through both
            # marks in a millisecond and the dashboard only ever sees the
            # second one.
            await _mark("running")
            # stream_mode=["updates", "custom"] (not just "updates") -- the
            # graph's own "work" node is a single StateGraph node that
            # manually drives a whole inner deep-agent run inside itself
            # (see work.py), so a plain "updates" stream would yield exactly
            # one event for the entire work pass, arriving only once it's
            # fully done. work_node's own get_stream_writer() calls emit
            # "custom" events (todos/log_entry) as each inner model turn or
            # tool call actually happens, including subagent-delegated ones
            # (tagged "work:<subagent_name>") -- these are what provide real
            # live streaming here.
            #
            # durability="sync" is the right tradeoff for this outer graph
            # specifically: "async" (the library default) checkpoints in the
            # background while the next step runs, which means a crash
            # between a step completing and its checkpoint write landing
            # could lose that last step. work<->verify_and_ship transitions
            # happen at most every few seconds to minutes (bounded by real
            # LLM/subprocess work, not by this), so the extra confirm-write
            # latency is negligible -- unlike the deep agent's own inner
            # astream_events() call (work.py), which stays on the library
            # default deliberately, since that graph's steps are much
            # higher-frequency (many rapid LLM/tool turns per single outer
            # "work" pass) and durability there isn't independently
            # controllable per-node anyway.
            async for mode, payload in graph.astream(
                graph_input, thread_config, stream_mode=["updates", "custom"], durability="sync"
            ):
                if mode == "custom":
                    if payload.get("type") == "todos":
                        # Merged against what this task has already finished,
                        # not taken as the whole truth: write_todos replaces
                        # the list, and a model updating its plan often writes
                        # only what is LEFT. Unmerged, the step strip fell
                        # from 6/12 to 0/6 mid-task while 23 files of real
                        # work sat in the worktree, and the operator read that
                        # as a loop (2026-09-12). See agent/plan_progress.py.
                        merged = plan_progress.merge_todos(
                            (_todos_meta.value or {}).get("latest_todos")
                            if (_todos_meta := await _read_task_meta(store, repo, task_id)) else None,
                            payload.get("todos"),
                        )
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "plan": _todos_to_plan(merged),
                        })
                        # Mirror into the Store meta, same pattern (and same
                        # reason) as the live cost mirror below: the outer
                        # checkpoint's latest_todos is only written when a
                        # work pass RETURNS -- a pass can run 30+ minutes, and
                        # until then the plan existed only as this ephemeral
                        # event. A refresh or task-switch mid-pass rebuilt
                        # from the checkpoint and the step strip came back
                        # empty (reported live 2026-08-28). Display state
                        # only; nothing enforcement-related reads it.
                        try:
                            if _todos_meta:
                                await store.aput(("tasks", repo), task_id,
                                                 {**_todos_meta.value, "latest_todos": merged})
                        except Exception:  # noqa: BLE001 -- a display mirror must never break the stream
                            logger.exception("todos mirror write failed for %s", task_id)
                    elif payload.get("type") == "log_entry":
                        entry = payload["entry"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": entry["node"],
                            "execution_log": [entry],
                        })
                    elif payload.get("type") == "cost":
                        # Live mid-pass cost from work.py's tracker (see
                        # _consume_values there) -- without this, cost only
                        # updates on outer node boundaries, and a single work
                        # pass can run well past ten minutes showing $0.00 the
                        # whole time. Mirrored into the Store meta too,
                        # throttled to >= $0.02 moves, so the tasks sidebar
                        # (which reads meta, not the stream) tracks as well.
                        # Display-only either way -- budget enforcement reads
                        # the tracker/checkpoint, never this.
                        live_cost = payload["cost_so_far"]
                        _publish(task_id, {
                            "type": "node_update",
                            "node": "work",
                            "cost_so_far": live_cost,
                        })
                        if live_cost - last_meta_cost >= 0.005:
                            meta_item = await store.aget(("tasks", repo), task_id)
                            if meta_item:
                                last_meta_cost = live_cost
                                await write_task_meta(store, repo, task_id, cost_so_far=live_cost)
                    continue

                # mode == "updates": one event per outer node ("work" or
                # "verify_and_ship") once its whole pass completes -- carries
                # the fields custom events don't (cost_so_far, escalated,
                # review_gate_result). Only forward keys the node's own
                # return dict actually included (not update.get(key, default))
                # -- verify_and_ship.py's own loop_back/no-diff/escalated-guard
                # paths each return a deliberately sparse dict (e.g. a
                # checks-failed loop-back never touches "escalated" at all),
                # and defaulting a missing key to False/None here would ship
                # a stale/wrong value over the wire instead of just omitting
                # the field (which the frontend already treats as "unchanged"
                # via its own `?? previousValue` merge in useTaskStream.ts).
                for node_name, update in payload.items():
                    if not isinstance(update, dict):
                        continue
                    node_payload: dict = {"type": "node_update", "node": node_name}
                    for key in (
                        "execution_log", "cost_so_far", "escalated", "escalation_reason",
                        "review_gate_result", "pending_approval", "committed_sha",
                    ):
                        if key in update:
                            node_payload[key] = update[key]
                    if "latest_todos" in update:
                        node_payload["plan"] = _todos_to_plan(update["latest_todos"])
                    _publish(task_id, node_payload)
                    # Keep the Store meta's display cost current per pass, not
                    # just at terminal transitions -- otherwise the tasks list
                    # (which reads meta, unlike the task view's
                    # checkpoint-backed hydrate) can show cost frozen at
                    # whatever the last terminal write recorded during a long
                    # multi-round run, wrongly suggesting the budget tracker
                    # had stalled. Enforcement never reads this -- the ceiling
                    # checks the checkpoint's own cost_so_far -- this is
                    # purely so the visible number tracks reality.
                    if "cost_so_far" in update:
                        last_meta_cost = update["cost_so_far"]
                        meta_item = await store.aget(("tasks", repo), task_id)
                        if meta_item:
                            await write_task_meta(store, repo, task_id, cost_so_far=update["cost_so_far"])

        final = await graph.aget_state(thread_config)
        values = final.values
        status = _final_status(values)
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=values.get("budget_usd", budget_usd),
            category=category, status=status, created_at=original_created_at or time.time(),
            cost_so_far=values.get("cost_so_far", 0.0),
            escalation_reason=values.get("escalation_reason"),
            # Where the work went, for a project that ships as a pull request.
            # On the task record rather than only in the step log, because a
            # PR is work waiting for a person and a link nobody can find is a
            # link nobody follows.
            pull_request_url=values.get("pull_request_url"),
        )
        _publish(task_id, {
            "type": "status",
            "status": status,
            "escalation_reason": values.get("escalation_reason"),
            "pending_approval": values.get("pending_approval"),
            "pull_request_url": values.get("pull_request_url"),
        })
        # Telegram: every rest state IS the actionable moment -- escalated,
        # waiting on an approval, waiting on the merge look, or done.
        _detail = None
        if status == "escalated":
            _detail = values.get("escalation_reason")
        elif status == "awaiting_approval":
            _pa = values.get("pending_approval") or {}
            _detail = _pa.get("description") if isinstance(_pa, dict) else str(_pa)
        elif status == "awaiting_merge":
            _pm = values.get("pending_merge_approval") or {}
            _sha = str(_pm.get("sha", ""))[:12] if isinstance(_pm, dict) else ""
            _detail = f"commit {_sha} passed review -- approve the merge in the dashboard"
        _alert_task_status(task_id, status, repo, goal, values.get("cost_so_far"), _detail)
    except asyncio.CancelledError:
        # The operator's Stop button (/stop below) cancels this task
        # directly. CancelledError is a BaseException, not an Exception, so
        # it never reaches the broad handler below -- without this branch a
        # stopped task's Store status would stay stuck on "running" forever,
        # identical-looking to a genuinely orphaned task with no way to tell
        # the two apart.
        #
        # cost_now uses last_meta_cost, not a fresh checkpoint read: the
        # outer checkpoint's cost_so_far only updates when a work pass fully
        # returns, so cancelling mid-pass (the common case -- Stop almost
        # always interrupts an actively-running pass) leaves it stale at
        # whatever it was before this pass started. last_meta_cost is kept
        # current throughout the pass (both from work.py's live per-call
        # "cost" events and from each node's own completed-pass update), so
        # it reflects real spend right up to the moment of cancellation.
        # Also patched back into the checkpoint itself (not just the Store
        # meta) so a future resume's BudgetTracker starts counting from the
        # real total instead of silently under-billing against the budget
        # ceiling.
        cost_now = last_meta_cost
        # audit M-34: every write in this handler is best-effort. A raising or
        # slow store.aput must NOT replace the CancelledError -- doing so 500'd a
        # Stop that actually worked and left the UI unsure whether it took. We
        # always re-raise CancelledError at the end regardless of these writes.
        try:
            await graph.aupdate_state(thread_config, {"cost_so_far": cost_now})
        except Exception:
            pass
        try:
            # escalation_reason is no longer named here -- and no longer lost.
            # The merge preserves whatever the record already carried.
            await write_task_meta(
                store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
                status="stopped", created_at=original_created_at or time.time(),
                cost_so_far=cost_now,
            )
            _publish(task_id, {"type": "status", "status": "stopped"})
        except Exception:  # noqa: BLE001
            logger.exception("stopped-status write failed for task %s (cancellation still honored)", task_id)
        raise
    except Exception as e:  # noqa: BLE001 -- deliberately broad: any failure here must still flip status away from "running"
        # str(e) alone is close to useless for some exception types -- a bare
        # KeyError's str() is just the missing key's repr with zero context
        # on where it was raised. Full traceback to the log; the Store/UI
        # still only need the short message, that's what the user sees.
        logger.error("task %s failed: %s", task_id, str(e))
        logger.error(traceback.format_exc())
        # audit H-20 fixed this path by naming cost_so_far and escalation_reason
        # explicitly after a full overwrite had erased them. The merge now makes
        # that structural rather than remembered.
        await write_task_meta(
            store, repo, task_id, goal=goal, budget_usd=budget_usd, category=category,
            status="error", created_at=original_created_at or time.time(),
            error=str(e), cost_so_far=last_meta_cost,
        )
        _publish(task_id, {"type": "status", "status": "error", "error": str(e)})
        _alert_task_status(task_id, "error", repo, goal, last_meta_cost, str(e)[:400])
    finally:
        _publish(task_id, {"type": "closed"})
        _running_tasks.pop(task_id, None)
        # Land whatever is still buffered. Awaited, not backgrounded: this is
        # the last moment the tail of the run exists anywhere.
        rec = _task_recorders.pop(task_id, None)
        if rec is not None:
            try:
                await rec.flush()
            except Exception:  # noqa: BLE001 -- never fail a task over its transcript
                logger.debug("task transcript tail not written for %s", task_id)


async def _run_task(
    task_id: str, goal: str, repo: str, budget_usd: float, category: str,
    auto_approve_commands: bool = False,
    require_merge_review: bool = True,
    route: str = "general",
    route_reason: str | None = None,
    reference_repos: list[str] | None = None,
) -> None:
    state = initial_state(
        task_id=task_id, goal=goal, repo=repo, budget_usd=budget_usd,
        auto_approve_commands=auto_approve_commands,
        require_merge_review=require_merge_review,
        reference_repos=reference_repos,
        route=route, route_reason=route_reason,
    )
    await _stream_graph(task_id, repo, goal, budget_usd, state, category=category,
                        route=route, route_reason=route_reason)


_read_with_retry = read_with_retry


async def _resolve_task_repo(task_id: str) -> str | None:
    """A handful of task endpoints (message/stop) only ever needed task_id
    before per-user repo access existed -- this looks up which repo a task
    belongs to from its own checkpoint state, the same source resume/
    approve/get/delete already read `repo` from directly."""
    checkpoint = await app.state.graph.aget_state({"configurable": {"thread_id": task_id}})
    if not checkpoint or not checkpoint.values:
        return None
    return checkpoint.values.get("repo")


@app.get("/api/repos")
def list_repos(user: User = Depends(require_full_auth)):
    return [r for r in PROJECTS if user.can_access(r)]


# ---------------------------------------------------------------------------
# Planning chat -- a conversational research/design-consulting session
# (agent/planning_chat.py), distinct from a build task: no plan/execute/
# verify graph, no budget ceiling, no write/edit/bash access to the repo.
# Its own Store namespace ("planning", repo) holds lightweight session meta
# (repo, created_at, updated_at, title, plan_markdown); the full
# conversation itself lives in the same Postgres checkpointer tasks use,
# under thread_id f"planning:{session_id}" (see planning_thread_config).
# "Build Now" in the frontend does NOT call anything here -- it just calls
# the existing POST /api/tasks with the saved plan_markdown as the goal,
# reusing the real build system entirely as-is.
# ---------------------------------------------------------------------------


# Live-log buffers (2026-08-28): the detailed stream entries (chat bubbles,
# tool chips) previously existed ONLY as in-flight WS events -- the durable
# sources hold much less (a task's checkpoint keeps per-pass summaries; a
# planning thread's messages get REWRITTEN by summarization), so a refresh or
# task switch mid-run swapped a rich live view for a skeleton. Each publisher
# now also appends its log entries here, and the hydrate endpoints return
# whichever source is fuller. In-process by design: it makes refresh/switch
# lossless while the server lives, costs no store churn, and after a backend
# restart the durable sources are still the fallback they always were.
_LIVE_LOG_MAX_ENTRIES = 3000   # matches the frontend's MAX_LOG_ENTRIES cap
_LIVE_LOG_MAX_KEYS = 12        # LRU-ish: enough for every concurrently-viewed run
_live_task_log: dict[str, list] = {}

# The durable half of that buffer. `_live_task_log` dies with the process, and
# on 2026-09-14 that is exactly what happened when the operator asked why a
# task "got lost": the answer needed the transcript, and all that survived was
# a 123-character stub in execution_log plus a clean worktree. Planning
# sessions got this on 2026-09-12 (agent/planning_log.py); build tasks are the
# ones that run for two hours and delegate seven subagents, so they needed it
# more.
_task_recorders = live_state.task_recorders   # see agent/live_state.py


def _start_task_recorder(task_id: str, repo: str) -> None:
    """Created where the repo is actually known -- _publish only has a task id,
    and a transcript filed under the wrong project is worse than none."""
    store = getattr(app.state, "store", None)
    if store is None:
        return
    _task_recorders[task_id] = planning_log.Recorder(
        repo, task_id, store, namespace=planning_log.TASK_NAMESPACE,
        detail_cap=planning_log.TASK_DETAIL_CAP)


def _flush_task_log_bg(rec: "planning_log.Recorder") -> None:
    """Fire and forget: a transcript must never delay the run it describes."""
    try:
        task = asyncio.create_task(rec.flush())
        task.add_done_callback(lambda t: t.exception())
    except Exception:  # noqa: BLE001
        pass
# Monotonic per-task event ids for the socket-first hydrate (log_stream.py).
_task_event_seq = log_stream.SeqCounter()
_live_planning_log: dict[str, list] = {}


def _live_log_append(book: dict, key: str, entries: list, on_evict=None) -> None:
    buf = book.get(key)
    if buf is None:
        while len(book) >= _LIVE_LOG_MAX_KEYS:
            evicted = next(iter(book))
            book.pop(evicted)
            if on_evict is not None:
                on_evict(evicted)
        buf = book[key] = []
    buf.extend(entries)
    if len(buf) > _LIVE_LOG_MAX_ENTRIES:
        del buf[: len(buf) - _LIVE_LOG_MAX_ENTRIES]


def _fuller_log(buffered: list | None, durable: list | None) -> list:
    """The hydrate rule: MERGE the two sources by entry id, durable first.

    This was "whichever list is longer", which cannot merge: a durable list
    that is longer but older replaced newer live entries, and a shorter one
    was discarded even when it held entries the buffer never had (everything
    before this process started). Identity comes from the entry's own content
    -- see agent/log_stream.py."""
    return log_stream.merge(durable, buffered)


# Last Telegram-alerted (status, detail) per task, in-process: a resumed task
# re-enters _stream_graph and re-derives the same rest state; the operator
# needs ONE phone buzz per distinct stop, not one per stream cycle.
_last_task_alert: dict[str, tuple] = {}


def _notify_bg(text: str, repo: str | None = None) -> None:
    """The one door alerts leave through: resolves the auth pool defensively
    so an alert can NEVER break the code path it decorates -- app.state has
    no auth_pool during unit tests and the earliest startup moments, and
    accessing a missing State attribute raises.

    audit H1: `repo` scopes the fan-out. Alert bodies carry the repo name, a
    goal excerpt and up to 1500 characters of failure detail, so a recipient
    restricted to one project must not receive another's. repo=None means an
    infrastructure alert (a service restart) and goes to admins only.
    """
    pool = getattr(app.state, "auth_pool", None)
    if pool is None:
        return
    notify_operators_bg(pool, text, repo)


def _alert_task_status(task_id: str, status: str, repo: str, goal: str, cost: float | None, detail: str | None) -> None:
    """Telegram alert for a task's rest state -- deduped, best-effort."""
    if status in ("running", "queued", "stopped"):
        # running is noise; queued is the same noise arriving earlier (it is
        # a normal step on the way to running, not an event); stopped is the
        # operator's own Stop button -- alerting someone about the button
        # they just pressed is spam.
        return
    key = (status, str(detail or "")[:120])
    if _last_task_alert.get(task_id) == key:
        return
    _last_task_alert[task_id] = key
    _notify_bg(task_alert(status, repo, goal, cost, detail), repo=repo)


# A planning turn is bounded by SILENCE, not by duration.
#
# It used to be `wait_for(..., timeout=1800)`: a flat 30-minute ceiling on the
# whole turn. That measures the wrong thing. A turn that is reading files and
# calling tools is working, and the hard questions -- the ones worth asking --
# are exactly the ones that take longest. On 2026-08-30 a live turn was killed
# at 30 minutes while actively streaming; it had cost $2.50 and produced no
# saved plan, so the operator got nothing for the half hour.
#
# What actually indicates a fault is no output at all: the model call hung, the
# provider stopped responding, a tool never returned. Every log entry and cost
# event this turn emits is a heartbeat, so the watchdog below fires only when
# those stop. Duration is unbounded on purpose -- the BUDGET is the ceiling
# that stops work, and it is the only one that should.
# Value lives in runtime_settings so it is adjustable without a restart.
_STALL_POLL_S = 15.0


class PlanningStalled(Exception):
    """No output from a planning turn for the configured stall window."""


async def _await_with_stall_watchdog(task: "asyncio.Task", heartbeat: dict, stall_s: float):
    """Await `task`, cancelling it only if `heartbeat['at']` stops advancing.

    Raises PlanningStalled -- deliberately NOT CancelledError, so the caller's
    cancellation handler keeps meaning "the operator pressed Stop" and this
    lands in the error path that banks cost and the partial draft.
    """
    # Poll faster than the window rather than on a fixed tick: the interval
    # is the worst-case delay between going silent and noticing, so it should
    # scale with the window instead of being a constant that happens to suit
    # one value of it.
    poll = max(0.05, min(_STALL_POLL_S, stall_s / 4))
    while True:
        done, _ = await asyncio.wait({task}, timeout=poll)
        if done:
            return task.result()
        idle = time.monotonic() - heartbeat["at"]
        if idle >= stall_s:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            raise PlanningStalled(
                f"no output for {int(idle // 60)}m — the turn produced "
                f"{heartbeat['events']} events, then went silent"
            )


# Live planning spend is mirrored into the Store as it accrues, the same way a
# build task's is. Without it the session row holds cost_usd from the LAST
# completed turn until this one banks, so any hydrate mid-turn -- a page
# reload, a reconnect -- overwrote the live figure on screen with a stale 0.00
# and left it there. Reported live 2026-08-31 on a turn that went on to spend
# $8.11 while the dashboard read $0 the whole way.
_PLANNING_COST_MIRROR_MIN_DELTA = 0.005


async def _mirror_planning_cost(store, repo: str, session_id: str, cost: float) -> None:
    """Display only. Budget enforcement reads the tracker, never this."""
    try:
        item = await store.aget(("planning", repo), session_id)
        if item:
            await store.aput(("planning", repo), session_id, {**item.value, "cost_usd": cost})
    except Exception:  # noqa: BLE001 -- a display mirror must never break the turn
        logger.exception("could not mirror planning cost for %s", session_id)


# One per session being streamed. The live buffer above dies with the process;
# this survives it, which is the whole point (agent/planning_log.py).
_planning_recorders: dict[str, planning_log.Recorder] = {}


def _publish_planning(session_id: str, event: dict) -> None:
    if event.get("type") == "log_entry" and event.get("entry"):
        _live_log_append(_live_planning_log, session_id, [event["entry"]])
        recorder = _planning_recorders.get(session_id)
        if recorder is not None and recorder.add(event["entry"]):
            # Batched: a busy turn publishes several entries a second, and a
            # store write per entry would be write amplification for
            # telemetry. Backgrounded so the stream never waits on it.
            _spawn_background(recorder.flush(), f"planning_log_flush:{session_id}")
    for q, _ws in _planning_subscribers.get(session_id, []):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("dropping event for a stalled planning %s subscriber", session_id)  # audit M-34


@app.post("/api/planning/sessions", status_code=201)
async def create_planning_session(req: CreatePlanningSessionRequest, user: User = Depends(require_full_auth)):
    if req.repo not in PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(PROJECTS)}")
    check_repo_access(user, req.repo)
    session_id = uuid.uuid4().hex
    await app.state.store.aput(("planning", req.repo), session_id, {
        "session_id": session_id, "repo": req.repo, "created_at": time.time(),
        "updated_at": time.time(), "title": None, "plan_markdown": None, "cost_usd": 0.0,
        "archived": False, "category": None,
        "route_override": normalize_override(req.route),
    })
    return {"session_id": session_id, "repo": req.repo}


@app.get("/api/planning/sessions")
async def list_planning_sessions(repo: str | None = None, user: User = Depends(require_full_auth)):
    store = app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await _read_with_retry(lambda r=r: store.asearch(("planning", r), limit=100))
        items.extend(item.value for item in results)
    items.sort(key=lambda s: s.get("updated_at", 0), reverse=True)
    return items


async def _find_planning_meta(session_id: str):
    """Session meta is stored per-repo (("planning", repo)), but the id
    routes here carry no repo -- cheap enough to check the handful of
    configured repos rather than also threading repo through every URL."""
    for repo in PROJECTS:
        item = await app.state.store.aget(("planning", repo), session_id)
        if item:
            return repo, item.value
    return None, None


@app.get("/api/planning/sessions/{session_id}")
async def get_planning_session(session_id: str, user: User = Depends(require_full_auth)):
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    agent, _plan_ref, _tracker = await build_planning_agent(
        config, repo, app.state.checkpointer, app.state.store, starting_cost=meta.get("cost_usd", 0.0),
        session_id=session_id,
    )
    thread_config = planning_thread_config(session_id, repo)
    checkpoint = await agent.aget_state(thread_config)
    messages = (checkpoint.values.get("messages") or []) if checkpoint and checkpoint.values else []
    log = [e for e in (_translate_planning_message(m) for m in messages) if e]
    # Three sources, each lossy in its own way. The checkpoint loses whatever
    # summarization compacted away; the live buffer loses everything when the
    # process exits; the durable transcript loses only what fell off its cap.
    # Merged by entry id, so a reader gets the union rather than whichever one
    # happens to be longest.
    log = _fuller_log(_live_planning_log.get(session_id), log)
    log = _fuller_log(await planning_log.load(app.state.store, repo, session_id), log)
    return {"meta": meta, "log": log, "running": session_id in _running_planning_turns}


@app.post("/api/planning/sessions/{session_id}/archive")
async def archive_planning_session(session_id: str, user: User = Depends(require_full_auth)):
    """Closes out a planning conversation without deleting it -- its full
    history/plan stays reachable (same as an "archived" task), it just drops
    out of the sidebar's default active list. Hit from the "New Plan"
    button once the operator is done with the current plan, whether or not
    they actually built from it."""
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    await app.state.store.aput(("planning", repo), session_id, {**meta, "archived": True})
    return {"ok": True}


@app.delete("/api/planning/sessions/{session_id}")
async def delete_planning_session(session_id: str, user: User = Depends(require_full_auth)):
    """Remove a planning conversation entirely -- history, plan and all.

    Archiving keeps a session reachable, which is right for one you might
    revisit. This is for the ones you would not: a conversation abandoned
    part-way, or work that was done another way, where there is nothing worth
    keeping and leaving it listed is clutter. Mirrors DELETE /api/tasks/{id},
    including refusing while a turn is in flight -- stop it first, same as
    every other state-changing action here.
    """
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)

    with _claim_run_slot(_running_planning_turns, session_id,
                         "planning session is processing a message"):
        await app.state.store.adelete(("planning", repo), session_id)
        # The conversation itself lives in the shared checkpointer under a
        # namespaced thread id (planning_thread_config), not under the bare
        # session id -- deleting only the Store record would leave every
        # message orphaned in the checkpoints table.
        await app.state.checkpointer.adelete_thread(f"planning:{session_id}")
        # The durable transcript too: a delete means gone, not gone from the
        # two lossy copies while the readable one survives.
        await planning_log.forget(app.state.store, repo, session_id)
        # In-process mirrors, or a later session reusing the id would inherit
        # this one's log.
        _live_planning_log.pop(session_id, None)
        _planning_recorders.pop(session_id, None)
        for _q, ws in _planning_subscribers.pop(session_id, []):
            try:
                await ws.close(code=4000, reason="planning session deleted")
            except Exception:  # noqa: BLE001 -- a dead socket must not fail the delete
                pass
    return {"ok": True}


async def _bank_planning_turn(
    session_id: str,
    repo: str,
    plan_markdown: str | None,
    spent: float | None,
    text: str | None = None,
    outcome: str | None = None,
    outcome_detail: str | None = None,
    brief: dict | None = None,
    new_project: dict | None = None,
) -> None:
    """Persist whatever a turn earned before it ended -- used by the two
    abnormal exits (operator Stop, and an exception), which both used to
    throw work away that the session had genuinely already paid for.

    A crashed turn still HAS a plan if the model called save_plan before the
    crash: run_planning_turn returns plan_ref["markdown"], and a crash means
    it never returned, so that draft lives nowhere else. The old error path
    didn't look at it, so the operator saw a red error AND lost the plan the
    model had just written. The cancel path had the same hole.

    Same PRESERVE-never-clobber rule as the success path: a None plan leaves
    the stored one alone (a turn can add or replace a plan, never remove
    one), and cost is banked because the spend is real either way.

    `outcome` is WHY the turn ended, persisted rather than only streamed.
    Until now the reason existed solely as a live WebSocket event: an operator
    who was not watching that exact second, or who refreshed, was left with a
    stream that simply stopped. The Telegram alert was the only durable record
    of the cause, which is not a reasonable thing to require. One of:

        completed  the turn finished and returned a plan
        stopped    the operator pressed Stop
        stalled    no output for the configured stall window
        budget     the per-turn dollar ceiling was reached
        error      anything else, with the message in outcome_detail

    `text` seeds a missing title the same way the success path does. Titles
    used to be set only on success, so a session whose FIRST turn failed sat
    in the sidebar as a permanent None -- both timed-out sessions on
    2026-08-27 did exactly that. No classify_task call here (the success path
    does one for the category): teardown after a failure is the wrong moment
    for another model call, and a title alone is what the sidebar needs.
    """
    try:
        item = await app.state.store.aget(("planning", repo), session_id)
        if not item:
            return
        meta = {**item.value, "updated_at": time.time(), "turn_active": False}
        if plan_markdown is not None:
            meta["plan_markdown"] = plan_markdown
        if spent is not None:
            meta["cost_usd"] = spent
        if brief is not None:
            meta["brief"] = brief  # same PRESERVE rule: a turn can add or replace a brief, never remove one
        if new_project is not None:
            meta["new_project"] = new_project  # a proposal made before the crash is still the operator's to answer
        if text and not meta.get("title"):
            meta["title"] = text[:60]
        if outcome is not None:
            meta["last_outcome"] = outcome
            meta["last_outcome_detail"] = outcome_detail
            meta["last_outcome_at"] = time.time()
        await app.state.store.aput(("planning", repo), session_id, meta)
    except Exception:  # noqa: BLE001 -- teardown must never raise over the failure it is cleaning up after
        logger.exception("failed to persist planning progress for session %s", session_id)
    # However the turn ended -- completed, stopped, stalled, out of budget or
    # crashed -- its last entries belong on disk. This is the one path every
    # ending goes through, which is why the flush lives here rather than in
    # each of them.
    recorder = _planning_recorders.pop(session_id, None)
    if recorder is not None:
        await recorder.flush()


async def _run_planning_turn_bg(session_id: str, repo: str, text: str, attachments: list[dict] | None = None,
                                allowed_repos: list[str] | None = None, is_admin: bool = False,
                                actor: str | None = None) -> None:
    try:
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        starting_cost = meta_item.value.get("cost_usd", 0.0) if meta_item else 0.0
        # Classified fresh per turn -- a conversation can drift from easy
        # design chat into a real bug report mid-session, and the model
        # should follow that. Classified on the clean, operator-typed text
        # -- not the attachments note appended below, same reasoning as
        # create_task's own goal/attachments split.
        #
        # STICKY UPWARD (2026-08-28): escalation is per-turn, de-escalation
        # never happens within a session. A HARD session's continuation
        # nudges are short by nature ("continue", "also check X") and
        # classify EASY on their text alone -- which flipped a session's
        # model mid-plan: half a plan was written by the HARD pin and
        # half by the EASY pin after a nudge (operator report; a restart
        # exposed it, but any short follow-up triggers the same flip). Once
        # a session has needed the hard model, its context IS the hard
        # problem -- every later turn reasons over that same context, so the
        # floor ratchets up and stays. A fresh session starts the ladder
        # over.
        difficulty = await classify_planning_difficulty(text, config)
        _prior_difficulty = (meta_item.value.get("difficulty") if meta_item else None) or "EASY"
        if _prior_difficulty == "HARD":
            difficulty = "HARD"
        # Frontend route (agent/frontend_route.py): the operator's override on
        # the session wins; otherwise decided from this message and, like
        # difficulty, sticky once a session has gone frontend -- the whole
        # context is frontend work from then on.
        #
        # The session's own category is passed in once it has one. It is the
        # strongest signal the router has (a model read the whole request),
        # and it did not reach this call at all: the route was decided on the
        # first message's keywords and then never revisited, so the storefront
        # HDR-lighting session on 2026-09-15 settled into `ui-styling` and
        # kept planning on the general seat for every turn after. A session
        # has no category until it first saves a plan (see the categorising
        # block below and its comment on why that is deliberate), so this is
        # None on turn one and real from then on -- the keywords still have to
        # carry the first turn.
        _meta_val = meta_item.value if meta_item else {}
        _route_decision = classify_frontend(text, _meta_val.get("category"), _meta_val.get("route_override"))
        if _meta_val.get("route") == "frontend" and not _meta_val.get("route_override"):
            _route_decision = RouteDecision("frontend", _meta_val.get("route_reason") or "earlier turn")
        route = _route_decision.route
        # turn_active/turn_started_at make an in-flight planning turn
        # STORE-VISIBLE, like a running task. Deploy tooling used to infer
        # idleness from tasks + router quiet, and a >90s gap inside one long
        # model call read as idle -- a restart landed mid-plan (the incident
        # that also exposed the difficulty flip above). Cleared on every
        # ending path; the timestamp lets a reader judge a marker that a
        # hard-killed process could not clear. There is no longer a turn
        # ceiling to compare against (see PLANNING_STALL_TIMEOUT_S) -- a turn
        # is bounded by silence and by budget, not by elapsed time, so treat
        # a marker as stale on the same stall basis rather than a fixed age.
        if meta_item:
            await app.state.store.aput(("planning", repo), session_id, {
                **meta_item.value, "difficulty": difficulty,
                "route": route, "route_reason": _route_decision.reason,
                "turn_active": True, "turn_started_at": time.time(),
            })
        # Seed the turn with the draft this session already has. The agent (and
        # its plan_ref) is rebuilt per turn, so without this the model cannot see
        # its own previous plan and every turn starts from a blank one.
        _prior = await app.state.store.aget(("planning", repo), session_id)
        _prior_plan = _prior.value.get("plan_markdown") if _prior else None
        # The brief too: written by save_brief on an earlier turn, it is what
        # keeps a follow-up message from forcing a fresh brief-first round.
        _prior_brief = _prior.value.get("brief") if _prior else None
        agent, plan_ref, tracker = await build_planning_agent(
            config, repo, app.state.checkpointer, app.state.store,
            starting_cost=starting_cost, difficulty=difficulty,
            existing_plan=_prior_plan,
            allowed_repos=allowed_repos,  # audit H-2
            existing_brief=_prior_brief,
            route=route,
            is_admin=is_admin,  # gates create_project; not derivable from allowed_repos
            actor=actor,
            session_id=session_id,  # joins this seat's retrieval events to the conversation
        )
        thread_config = planning_thread_config(session_id, repo)
        # Circuit breaker: llm_for_role's own per-call timeout (plus
        # ChatOpenAI's default retries) can still leave a turn hanging for
        # 10+ minutes with zero log_entry published and no exception ever
        # raised if the underlying call genuinely never returns (confirmed
        # live 2026-08-23 -- a planning turn sat with no checkpoint, no log
        # line beyond entering astream_events, and no error surfaced for
        # over 10 minutes). Without this, that reads to the operator as "the
        # agent is stuck" with nothing to even look at. agent-planning-chat's
        # own per-call timeout is 450s (reasoning_effort="high" genuinely
        # needs that long -- see llm_for_role), and a real turn can involve
        # several tool-calling round trips, so this outer ceiling has to
        # clear several such calls comfortably; it exists purely so the
        # except Exception below always fires eventually instead of never.
        message_text = text + _attachments_note(attachments) if attachments else text
        opening = {
            "kind": "user", "summary": text[:200], "detail": text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        _live_log_append(_live_planning_log, session_id, [opening])
        # The durable transcript starts here, with the operator's own message:
        # a turn read back later is unintelligible without the thing that
        # started it.
        recorder = _planning_recorders.setdefault(
            session_id, planning_log.Recorder(repo, session_id, app.state.store))
        recorder.add(opening)
        # Every published event doubles as the watchdog's heartbeat.
        _heartbeat = {"at": time.monotonic(), "events": 0}
        # Live cost is mirrored into the Store as it accrues, the same way a
        # build task's is (see the "cost" branch of run_task). Without it the
        # session row holds cost_usd from the LAST completed turn until this
        # one banks, so hydrating mid-turn -- any page reload, any reconnect --
        # overwrote the live figure on screen with a stale 0.00 and left it
        # there. Reported live 2026-08-31 on a turn that went on to spend
        # $8.11 while the dashboard read $0 throughout.
        _mirror = {"cost": starting_cost}
        _mirror_tasks: set[asyncio.Task] = set()

        def _publish_and_beat(ev):
            _heartbeat["at"] = time.monotonic()
            _heartbeat["events"] += 1
            # cost events arrive pre-shaped ({"type": "cost", ...}); log
            # entries need wrapping -- route on the shape.
            is_cost = isinstance(ev, dict) and ev.get("type") == "cost"
            if isinstance(ev, dict) and ev.get("type") == "ping":
                # A heartbeat from the turn (nothing worth showing this tick).
                # The beat above is the point; forward it as a bare ping,
                # which the client already ignores, and never as a log entry
                # -- wrapped, it reached the page as an entry with no text
                # and the error boundary took the whole view down (2026-09-09).
                _publish_planning(session_id, {"type": "ping"})
                return
            _publish_planning(
                session_id,
                ev if is_cost else {"type": "log_entry", "entry": ev},
            )
            if not is_cost or not isinstance(ev.get("cost_usd"), (int, float)):
                return
            cost = float(ev["cost_usd"])
            # Throttled HERE rather than inside the coroutine: the check is
            # synchronous, so two cost events in the same tick cannot both
            # decide to write.
            if cost - _mirror["cost"] < _PLANNING_COST_MIRROR_MIN_DELTA:
                return
            _mirror["cost"] = cost
            # Fire-and-forget: a store write must never block the stream, and
            # a lost mirror costs freshness, not correctness -- the
            # authoritative figure is still banked when the turn ends. The
            # strong reference is required; asyncio holds only a weak one, so
            # a bare create_task can be collected mid-flight.
            _t = asyncio.create_task(
                _mirror_planning_cost(app.state.store, repo, session_id, cost)
            )
            _mirror_tasks.add(_t)
            _t.add_done_callback(_mirror_tasks.discard)

        _turn_task = asyncio.create_task(
            run_planning_turn(
                agent, plan_ref, thread_config, message_text,
                _publish_and_beat,
                tracker=tracker,
            )
        )
        plan_markdown = await _await_with_stall_watchdog(
            _turn_task, _heartbeat, runtime_settings.value("planning_stall_timeout_s")
        )
        # PRESERVE, never clobber. `plan_markdown` is only what THIS turn's
        # save_plan call produced, and the system prompt deliberately does not
        # ask the model to re-save every turn ("call this once the plan is
        # genuinely ready, and again any time it meaningfully changes").
        # Writing it unconditionally meant any later turn that merely discussed
        # something erased a plan the session had already earned -- the operator
        # sees several good rounds and then no plan to send to Build, with no
        # error anywhere. A turn can add or replace a plan, never remove one.
        effective_plan = plan_markdown if plan_markdown is not None else _prior_plan
        meta_item = await app.state.store.aget(("planning", repo), session_id)
        # A create_project proposal rides the same shared plan_ref as the plan
        # and the brief: the tool can only return text to the model, so this
        # is the one way the server learns of it. Same PRESERVE rule -- a turn
        # that proposed nothing keeps the proposal the operator has not yet
        # answered (only the confirm/dismiss route clears it).
        new_project = plan_ref.get("new_project") or (meta_item.value.get("new_project") if meta_item else None)
        if meta_item:
            meta = {
                **meta_item.value, "updated_at": time.time(),
                "plan_markdown": effective_plan, "cost_usd": tracker.total_cost,
                "turn_active": False,
                "brief": plan_ref.get("brief") or meta_item.value.get("brief"),
                "new_project": new_project,
            }
            if not meta.get("title"):
                meta["title"] = text[:60]
            # Categorised when the session first has a SAVED PLAN, not on its
            # first message.
            #
            # Categorising early made the sidebar move the session mid-read:
            # the operator sent a message, the reply came back ending in a
            # question, and at that same moment the session jumped out of the
            # ungrouped list into a category group -- landing next to an older
            # session on the same subject that already had a plan. The screen
            # flicked, the question went unread, and the neighbouring "Build
            # now" button read as "my plan is ready" (operator report,
            # 2026-08-29). Nothing was actually mis-saved; the movement itself
            # was the misinformation.
            #
            # Tying the move to the plan makes it mean something: a session
            # settles into a category exactly when it becomes buildable, so
            # movement in the sidebar is a signal rather than noise. It also
            # stops paying the classifier for sessions that never produce a
            # plan. Same fixed taxonomy as a build task (agent/classify.py).
            if effective_plan and not meta.get("category"):
                classification = await classify_task(text, config)
                meta["category"] = classification.category
            await app.state.store.aput(("planning", repo), session_id, meta)
        await _bank_planning_turn(
            session_id, repo, None, None, outcome="completed"
        )
        _publish_planning(session_id, {
            "type": "turn_complete", "plan_markdown": effective_plan, "cost_usd": tracker.total_cost,
            "new_project": new_project,
        })
    except asyncio.CancelledError:
        # Operator pressed Stop, or the process is shutting down. The spend is
        # real either way, so bank it against the session rather than losing it,
        # and keep whatever plan the session already had -- a stopped turn must
        # never be the thing that erases a plan.
        logger.info("planning turn cancelled for session %s", session_id)
        # `tracker` is created partway through the try, so a cancel that lands
        # early leaves it unbound -- fall back to the cost the session already
        # had rather than raising NameError out of a cancellation handler.
        _t = locals().get("tracker")
        spent = _t.total_cost if _t is not None else locals().get("starting_cost", 0.0)
        # `plan_ref` is bound partway through the try, same as `tracker` -- a
        # cancel landing before build_planning_agent leaves both unbound.
        _ref = locals().get("plan_ref") or {}
        await _bank_planning_turn(
            session_id, repo, _ref.get("markdown"), spent, text=text, outcome="stopped",
            brief=_ref.get("brief"), new_project=_ref.get("new_project"),
        )
        _publish_planning(session_id, {"type": "stopped", "cost_usd": spent})
        raise
    except Exception as e:  # noqa: BLE001 -- must surface to the client, never die silently in the background
        logger.exception("planning turn failed for session %s", session_id)
        _t_alert = locals().get("tracker")
        _notify_bg(task_alert(
            "planning_error", repo, text, _t_alert.total_cost if _t_alert is not None else None, str(e)[:400]),
            repo=repo)
        # The spend up to the failure is just as real as a cancelled turn's,
        # and this path banked NEITHER it nor the draft -- a turn that crashed
        # after save_plan lost the plan and under-reported the session's cost.
        _t = locals().get("tracker")
        _ref = locals().get("plan_ref") or {}
        # Distinguish the three ways a turn can end badly. They mean very
        # different things to an operator -- "the ceiling you set was reached"
        # is not a fault, "it went silent" is, and "it raised" is a third
        # thing again -- and lumping them under one red banner is what made
        # the last stall unreadable without opening Telegram.
        if isinstance(e, PlanningStalled):
            _outcome = "stalled"
        elif isinstance(e, BudgetExceededError):
            _outcome = "budget"
        else:
            _outcome = "error"
        await _bank_planning_turn(
            session_id, repo, _ref.get("markdown"),
            _t.total_cost if _t is not None else None,
            text=text, outcome=_outcome, outcome_detail=str(e)[:500],
            brief=_ref.get("brief"), new_project=_ref.get("new_project"),
        )
        _publish_planning(
            session_id,
            {"type": "error", "outcome": _outcome, "message": str(e)},
        )
    finally:
        _publish_planning(session_id, {"type": "closed"})
        _running_planning_turns.pop(session_id, None)


@app.post("/api/planning/sessions/{session_id}/stop")
async def stop_planning_turn(session_id: str, user: User = Depends(require_full_auth)):
    """Cancels the in-flight planning turn and waits for teardown before
    responding -- same contract as stop_task, and for the same reason: firing
    cancel() and returning immediately would report "stopped" to the UI while a
    web_search or a 450s reasoning call is still in flight.

    A planning turn can legitimately run for many minutes (agent-planning-chat
    runs at reasoning_effort=high), so without this the only way to end one was
    to wait it out or restart the service -- and restarting kills the turn with
    no record, leaving the UI waiting on a turn that no longer exists.

    Fails CLOSED on repo resolution, same as stop_task: an authorization check
    that no-ops when it cannot reach its input is not a check.
    """
    repo, _meta = await _find_planning_meta(session_id)
    if not repo:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    turn = _running_planning_turns.get(session_id)
    if not turn:
        raise HTTPException(409, "planning session is not processing a message")
    turn.cancel()
    try:
        await turn
    except asyncio.CancelledError:
        pass
    return {"ok": True}


@app.post("/api/planning/sessions/{session_id}/message", status_code=202)
async def send_planning_message(session_id: str, req: PlanningMessageRequest, user: User = Depends(require_full_auth)):
    with _claim_run_slot(_running_planning_turns, session_id, "planning session is already processing a message"):
        repo, meta = await _find_planning_meta(session_id)
        if not meta:
            raise HTTPException(404, "planning session not found")
        check_repo_access(user, repo)
        if not req.text.strip():
            raise HTTPException(400, "message text is required")
        _running_planning_turns[session_id] = asyncio.create_task(
            _run_planning_turn_bg(session_id, repo, req.text.strip(), req.attachments,
                                  allowed_repos=user.allowed_repos,
                                  # role, not allowed_repos: None there means admin OR legacy unscoped
                                  is_admin=user.role == "admin", actor=user.email)
        )
        return {"ok": True}


class NewProjectDecisionRequest(BaseModel):
    decision: Literal["confirm", "dismiss"]
    # Overrides the proposal's `github` when the operator changes the box on
    # the confirm card; None keeps what the agent recorded.
    github: bool | None = None
    token_name: str | None = None


async def _move_planning_session(session_id: str, old_repo: str, new_repo: str, meta: dict) -> dict:
    """Re-home a session's two Store rows -- meta at ("planning", repo) and
    the durable transcript at ("planning_log", repo) -- under the new repo.
    The checkpoint thread id is f"planning:{session_id}" with no repo in it
    (planning_thread_config), so the conversation itself needs nothing.

    Copy first, delete after: a crash between the two leaves a duplicate the
    next confirm can overwrite, never a session with no row anywhere.
    """
    store = app.state.store
    new_meta = {**meta, "repo": new_repo, "new_project": None, "updated_at": time.time()}
    await store.aput(("planning", new_repo), session_id, new_meta)
    log_item = await store.aget((planning_log.NAMESPACE, old_repo), session_id)
    if log_item and log_item.value:
        await store.aput((planning_log.NAMESPACE, new_repo), session_id, log_item.value)
    await store.adelete(("planning", old_repo), session_id)
    await planning_log.forget(store, old_repo, session_id)
    return new_meta


@app.post("/api/planning/sessions/{session_id}/new-project")
async def decide_planning_new_project(session_id: str, req: NewProjectDecisionRequest,
                                      user: User = Depends(require_full_auth)):
    """Answer the confirm card a create_project tool call put on the session.

    The planner cannot create anything itself (agent/tools/planning_tools.py):
    it records a proposal in the session meta and the operator answers it
    here. Confirm runs the SAME code as POST /api/projects/create -- one
    provisioning path, whichever door the project came through -- and then
    moves the session onto the new repo so the conversation continues there
    with the new project's memory and sandbox. A failed create leaves the
    session where it is and answers 200 with the step list, so the card can
    show which step died and stay up for a retry.
    """
    auth.require_admin(user)
    repo, meta = await _find_planning_meta(session_id)
    if not meta:
        raise HTTPException(404, "planning session not found")
    check_repo_access(user, repo)
    if req.decision == "dismiss":
        # A plain meta write; a turn ending later re-reads the row and
        # preserves what it finds, so this needs no run slot.
        meta = {**meta, "new_project": None, "updated_at": time.time()}
        await app.state.store.aput(("planning", repo), session_id, meta)
        return {"project": None, "session": meta}

    proposal = meta.get("new_project")
    if not proposal:
        raise HTTPException(409, "this session has no proposed project to confirm")
    # The move rewrites the rows a running turn writes back at its end, so it
    # is refused mid-turn the same way delete is.
    with _claim_run_slot(_running_planning_turns, session_id,
                         "planning session is processing a message"):
        create_req = CreateProjectRequest(
            name=proposal["name"],
            description=proposal.get("description") or "",
            github=bool(proposal.get("github")) if req.github is None else req.github,
            token_name=req.token_name,
        )
        result = await _create_project(create_req, user)
        if not result.get("ok"):
            return {"project": result, "session": meta}
        # reload_projects() ran inside _create_project, so the new repo is in
        # PROJECTS and _find_planning_meta can see the moved row.
        new_meta = await _move_planning_session(session_id, repo, result["name"], meta)
    return {"project": result, "session": new_meta}


@app.websocket("/api/planning/sessions/{session_id}/stream")
async def stream_planning_session(ws: WebSocket, session_id: str):
    # WebSocket.cookies is populated from the handshake's headers before
    # accept() is ever called -- validate first and close outright (never
    # accept then immediately drop) for an unauthorized or repo-mismatched
    # connection attempt.
    user = await auth.get_user_from_ws_cookie(app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: enforce the same forced-screen gates as require_full_auth. A
    # valid session cookie alone must not open the live stream while the user
    # is parked behind the forced password-change / 2FA-setup screen.
    if _forced_screen_block(user):
        await ws.close(code=4403)
        return
    repo, meta = await _find_planning_meta(session_id)
    if not meta or not user.can_access(repo):
        await ws.close(code=4403)
        return
    await ws.accept()
    # No eviction of prior connections -- see stream_task's own comment on
    # this exact same pattern. Multiple viewers on the same planning session
    # (different users, or the same user in two tabs) all stay live
    # simultaneously; each connection's own receiver() below cleans up only
    # its own entry when it actually disconnects.
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)  # audit M-34
    _planning_subscribers.setdefault(session_id, []).append((queue, ws))

    async def sender():
        while True:
            # Heartbeat (2026-08-28): a long model call can mean 60s+ of
            # total socket silence, and NAT/middleboxes between the operator
            # and this VPS kill idle TCP without telling either end -- the
            # browser's stream just goes still until a manual refresh
            # (reported live). A ping every 20s keeps bytes flowing through
            # every hop, and when the socket IS dead, send_json raises here
            # promptly so the client gets a real close event to react to
            # instead of silence. Clients ignore the "ping" type.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
            if event.get("type") == "closed":
                break

    async def receiver():
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _planning_subscribers.get(session_id, []):
            _planning_subscribers[session_id].remove(entry)
        if session_id in _planning_subscribers and not _planning_subscribers[session_id]:
            del _planning_subscribers[session_id]  # audit M-34


def _readable_repos(user: User) -> list[str]:
    """Which projects a task started by `user` may READ for reference.

    Their own access, resolved here rather than carried as "None means
    everything": the task stores a concrete list, so a task resumed from a
    checkpoint written before this existed falls back to its own repo alone
    instead of silently to all of them. See agent/tools/reference_tools.py.
    """
    if user.allowed_repos is None:
        return sorted(PROJECTS)
    return sorted(r for r in user.allowed_repos if r in PROJECTS)


async def _start_task(
    goal: str, repo: str, budget_usd: float | None, route: str, *,
    auto_approve_commands: bool, require_merge_review: bool,
    reference_repos: list[str] | None = None,
    attachments: list[dict] | None = None, origin: str | None = None,
) -> dict:
    """Classify, route and launch a task. The New Task form, Build Now and the
    GitHub inbox all come through here so a task is the same thing whoever
    started it: same classifier, same route decision, same budget default.
    `origin` is recorded on the task meta ("github" for inbox tasks)."""
    goal = goal.strip()
    if not goal:
        raise HTTPException(422, "goal must not be empty")
    task_id = str(uuid.uuid4())
    budget = budget_usd or runtime_settings.value("default_task_budget_usd")
    # Classified on the clean, operator-typed goal -- not the attachments
    # note appended below, which is boilerplate for the model, not signal
    # about what kind of task this is.
    # audit M-34: bound how long task creation blocks on the classifier. It
    # already self-times-out at 15s, but the operator shouldn't wait that long
    # for a task id over a label that's mostly for Analytics -- fall back to the
    # neutral classification if it's slow.
    try:
        classification = await asyncio.wait_for(classify_task(goal, config), timeout=8)
    except TimeoutError:
        logger.warning("task classification exceeded 8s; starting with fallback classification")
        classification = TaskClassification(category="other", needs_tests=False)
    raw_goal = goal
    if attachments:
        goal = goal + _attachments_note(attachments)
    if classification.needs_tests:
        # An explicit directive, not a hope: the coordinator's system prompt
        # already tells it to delegate test-writing "when the task calls for
        # it", but nothing enforces that judgment call -- this makes the
        # judgment call for it up front, for the one signal (new/changed
        # testable logic) classify_task can actually assess before any code
        # has been read.
        goal = goal + TEST_REMINDER_NOTE
    # Which coder seat (agent/frontend_route.py): the operator's toggle, else
    # the category, the named paths, then keywords. Decided once, here.
    decision = classify_frontend(raw_goal, classification.category, normalize_override(route))
    if origin:
        # write_task_meta merges, so this survives _stream_graph's own first
        # write whichever lands first.
        await write_task_meta(app.state.store, repo, task_id, origin=origin)
    _running_tasks[task_id] = asyncio.create_task(
        _run_task(
            task_id, goal, repo, budget, classification.category,
            # Snapshot of the creator's own settings -- see outer_state.py.
            auto_approve_commands=auto_approve_commands,
            require_merge_review=require_merge_review,
            reference_repos=reference_repos or [],
            route=decision.route, route_reason=decision.reason,
        )
    )
    return {"task_id": task_id, "category": classification.category, "needs_tests": classification.needs_tests,
            "route": decision.route, "route_reason": decision.reason}


@app.post("/api/tasks", status_code=201)
async def create_task(req: CreateTaskRequest, user: User = Depends(require_full_auth)):
    if req.repo not in PROJECTS:
        raise HTTPException(400, f"unknown repo {req.repo!r}, must be one of {list(PROJECTS)}")
    check_repo_access(user, req.repo)
    # audit M-33: reject a whitespace-only goal (Field min_length=1 still lets a
    # lone space through), matching send_planning_message's own check.
    return await _start_task(
        req.goal, req.repo, req.budget_usd, req.route,
        # Per project, not per account: see User.auto_approves.
        auto_approve_commands=user.auto_approves(req.repo),
        require_merge_review=user.require_merge_review,
        # What this task may read for reference, from the creator's own
        # access at the time -- see _readable_repos.
        reference_repos=_readable_repos(user),
        attachments=[a.model_dump() for a in req.attachments] if req.attachments else None,
    )


@app.get("/api/tasks")
async def list_tasks(repo: str | None = None, user: User = Depends(require_full_auth)):
    store = app.state.store
    if repo:
        check_repo_access(user, repo)
        repos = [repo]
    else:
        repos = [r for r in PROJECTS if user.can_access(r)]
    items = []
    for r in repos:
        results = await _read_with_retry(lambda r=r: store.asearch(("tasks", r), limit=50))
        items.extend(item.value for item in results)
    items.sort(key=lambda t: t.get("created_at", 0), reverse=True)
    return items


# The commit-reviewer service's own dashboard API (router credit balance,
# per-model spend). The frontend used to call that service directly, but a
# deployment may put it behind a separate reverse-proxy auth that this app's
# own users have no session for (this one did). That silently 401'd the
# balance fetch for anyone who had only logged into Tektonix's own auth,
# and BalanceStrip.tsx swallows any fetch failure (renders nothing rather
# than an error), so the balance just vanished from the sidebar with no
# visible cause. This passthrough re-uses this app's own auth instead, so
# the balance only ever depends on being logged into Tektonix itself.
_REVIEW_SERVICE_BASE_URL = "http://127.0.0.1:4100"


@app.get("/api/router-balance")
async def get_router_balance(user: User = Depends(require_full_auth)):
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_REVIEW_SERVICE_BASE_URL}/api/router/balance")
        resp.raise_for_status()
        return resp.json()


@app.get("/api/stats")
async def get_stats(user: User = Depends(require_full_auth)):
    """Per-repo cost/outcome aggregates, computed fresh from the Store on
    every call rather than maintained as a running counter -- this backend
    doesn't run often enough or handle enough volume for that to matter, and
    computing fresh avoids a counter silently drifting from reality.
    """
    auth.require_admin(user)
    store = app.state.store
    per_repo = {}
    total_cost = 0.0
    total_tasks = 0
    status_counts = {"running": 0, "done": 0, "escalated": 0, "error": 0}

    for repo in PROJECTS:
        results = await _read_with_retry(lambda repo=repo: store.asearch(("tasks", repo), limit=200))
        tasks = [item.value for item in results]
        repo_cost = sum(t.get("cost_so_far", 0.0) or 0.0 for t in tasks)
        per_repo[repo] = {
            "task_count": len(tasks),
            "total_cost": repo_cost,
            "status_counts": {
                s: sum(1 for t in tasks if t.get("status") == s) for s in ("running", "done", "escalated", "error")
            },
        }
        total_cost += repo_cost
        total_tasks += len(tasks)
        for t in tasks:
            s = t.get("status")
            if s in status_counts:
                status_counts[s] += 1

    return {
        "per_repo": per_repo,
        "total_cost": total_cost,
        "total_tasks": total_tasks,
        "status_counts": status_counts,
    }


@app.post("/api/tasks/{task_id}/message")
async def send_message(task_id: str, req: SendMessageRequest, user: User = Depends(require_full_auth)):
    if task_id not in _running_tasks:
        raise HTTPException(409, "task is not running -- nothing would read this message")
    repo = await _resolve_task_repo(task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    add_message(task_id, req.text)
    # Published immediately so the UI shows the nudge landed right away --
    # work_node (agent/nodes/work.py) drains this mailbox at the start of
    # its next pass, which can be much later than the moment this message
    # was sent (a "work" pass can span many inner turns before
    # verify_and_ship ever loops back) -- a message that visibly "goes
    # nowhere" in the meantime is worse than no feedback.
    _publish(task_id, {
        "type": "node_update",
        "node": "operator",
        "execution_log": [{
            "node": "operator",
            "step_id": None,
            "summary": "message sent -- will be picked up at the start of the next work pass",
            "detail": req.text,
            "cost_usd": 0.0,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }],
    })
    return {"ok": True}


@app.post("/api/tasks/{task_id}/stop")
async def stop_task(task_id: str, user: User = Depends(require_full_auth)):
    """Cancels the asyncio task actually driving this run and waits for it
    to finish tearing down before responding -- firing task.cancel() and
    returning immediately would tell the UI "stopped" while a shell command
    or LLM call might still be mid-flight for another few seconds, which is
    exactly the "doesn't actually stop" gap this exists to close. run_shell
    (agent/tools/shell.py) kills the whole process group on cancellation
    too, not just abandons the await -- a naive stop here would otherwise
    leave e.g. a typecheck/test subprocess and everything it forked running
    orphaned on the server. _stream_graph's own CancelledError handler
    records the real cost-so-far and flips Store status to "stopped" before
    this returns.
    """
    repo = await _resolve_task_repo(task_id)
    # Fail CLOSED. This used to be `if repo:` -- so a task whose repo could
    # not be resolved (checkpoint not yet written, a transient store read
    # failure) skipped the access check entirely and any authenticated user
    # could act on it. An authorization check that silently no-ops when it
    # can't reach its input is not a check.
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    task = _running_tasks.get(task_id)
    if not task:
        raise HTTPException(409, "task is not running")
    task.cancel()
    # audit M-34: bound the wait for teardown. A slow Postgres in the
    # CancelledError handler could otherwise hang this request indefinitely; the
    # cancellation itself has already been requested, so after the timeout we
    # return and let teardown finish in the background rather than block the UI.
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=30)
    except asyncio.CancelledError:
        pass
    except TimeoutError:
        logger.warning("stop_task: teardown for %s still running after 30s; returning anyway", task_id)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str, req: ResumeTaskRequest, user: User = Depends(require_full_auth)):
    """Escalation is a circuit breaker, not a dead end -- the graph's full
    state (plan, execution log, cost so far) is still sitting in the
    checkpointer, so an escalated or budget-exhausted task has a path back
    in: add budget and continue rather than being stuck looking abandoned.

    Also covers a task orphaned by a backend restart mid-run -- Store still
    says "running" but nothing is actually driving it (this pm2 process gets
    restarted routinely to deploy fixes; a task that happened to be running
    at that moment would otherwise look "running" forever with no way back
    in either).

    These two cases need different handling:

    - Orphaned/stopped: patch `budget_usd`/`max_iterations`/`task_id`
      without `as_node` -- LangGraph resumes at whatever node the checkpoint
      already has as `next` (either "work", if the process died mid-work-pass
      or before one ever started, or "verify_and_ship", if it died between a
      completed work pass and the gate re-running). Nothing about escalated/
      pending_feedback is touched, so routing is exactly whatever it already
      was -- no replanning, no lost progress.
    - Genuine escalation: `_route_after_verify` (outer_graph.py) checks
      `state["escalated"]` first, before pending_feedback, so simply
      patching `escalated=False` is not enough on its own -- it also needs a
      truthy `pending_feedback` to route to "work" rather than falling
      through to END. Patched `as_node="verify_and_ship"` so the conditional
      edge re-evaluates against the new state and correctly resolves to
      "work". `pending_feedback` here doubles as the actual instruction the
      deep agent sees as a new HumanMessage on its next work.py pass (case 2
      in work.py's own docstring) -- not just a routing signal, real
      content: what the escalation was and that budget was added, plus the
      operator's own resume message if they left one.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        meta = await app.state.store.aget(("tasks", values["repo"]), task_id)
        store_status = meta.value.get("status") if meta else None
        was_escalated = bool(values.get("escalated"))
        # "done" is resumable too, not fully terminal like any other completion:
        # the "two consecutive no-diff passes -> done, no changes needed"
        # safeguard in verify_and_ship.py (built to stop genuinely-finished
        # investigations from looping forever) can't always tell a real
        # conclusion apart from the model dropping a tool call mid-investigation.
        # An operator who judges a "done" verdict premature needs a way back in,
        # the same as an escalation -- there's no substitute for a human catching
        # a wrong "done" and saying "no, keep going."
        was_done = (not was_escalated) and store_status == "done"
        # "running" here means orphaned (Store says running, nothing actually
        # driving it) rather than genuinely running, since the endpoint itself
        # already 409s above when task_id is truly in _running_tasks.
        # "stopped" is the operator's own Stop button (/stop below) -- same
        # "nothing lost, just paused" situation as an orphaned task, so it's
        # resumable through the exact same non-replanning path.
        # audit H-20: "error" is resumable too. verify_and_ship's catch-all exists
        # specifically to route around an unresumable error status, but anything
        # raising outside that handler still lands with an intact checkpoint the
        # endpoint used to refuse -- contradicting "a task is never a dead end."
        # The checkpoint is intact, so a resume continues from the last good state.
        resumable = was_escalated or was_done or store_status in ("running", "stopped", "error")
        if not resumable:
            raise HTTPException(409, f"task status is {store_status!r} -- nothing to resume")
        if was_done and not req.message:
            # Unlike an escalation (which always has a real reason to restate),
            # a "done" task has nothing to nudge with on its own -- silently
            # reopening it with no instruction would just re-run the same
            # investigation and likely land on the same premature conclusion.
            raise HTTPException(400, "resuming a done task requires a message telling it what to do next")

        # A bare float let a negative value shrink the ceiling below what has
        # already been spent (making the guard fire immediately and look like a
        # crash) and let an enormous one defeat the budget entirely. Bound it to a
        # sane top-up range; the field is a *delta*, not a new total. Zero is a
        # valid delta: the dashboard's resume panel only shows the budget field
        # when the task is nearly out of money and sends 0 otherwise (a merge
        # failure or an operator Stop has nothing to do with cost), and the
        # escalated branch below already words the note for "no budget added".
        # Rejecting 0 left every such resume stuck on a 400 with no field to fix.
        _check_budget_topup(req.additional_budget_usd)
        new_budget = values["budget_usd"] + req.additional_budget_usd
        # max_iterations is set once at task creation (40) and, unlike
        # budget_usd, was never bumped on resume -- every work<->verify_and_ship
        # cycle across the task's entire lifetime counts against that same fixed
        # cap, no matter how many times it's legitimately been resumed (backend
        # restarts, manual stop/resume, operator nudges). Without growing it on
        # each resume, a task resumed several times over one long session could
        # hit iteration_count == max_iterations with cost_so_far nowhere near
        # budget_usd -- an iteration-count artifact, not real futility. +40 per
        # resume mirrors how budget_usd already grows here.
        new_max_iterations = values.get("max_iterations", 40) + 40
        # Explicit even though initial_state() already sets this for any task
        # created after task_id was added to AgentState -- this closes the gap
        # for older tasks that predate that field.
        patch = {"task_id": task_id, "budget_usd": new_budget, "max_iterations": new_max_iterations}
        approved = values.get("merge_approved_sha")
        if was_escalated and approved and approved == values.get("committed_sha") and not req.message:
            # Approved, committed, and escalated only on the way out -- the
            # review service timing out, a push failing. The work is finished,
            # so this does not go back to work: no pending_feedback, and
            # _route_after_verify takes the approved-merge path straight into
            # verify_and_ship's fast path (re-review, then merge or PR). The
            # generic branch below sent five such tasks on 2026-09-23 into a
            # paid work pass that redid committed changes. An operator message
            # still means "do more", and still goes to work.
            patch["escalated"] = False
            patch["escalation_reason"] = None
            patch["stale_pending_review_streak"] = 0
            await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
        elif was_escalated:
            budget_note = (
                f"Additional budget granted -- ${req.additional_budget_usd:.2f} more, ${new_budget:.2f} total now. "
                if req.additional_budget_usd > 0
                else "No additional budget added. "
            )
            resume_note = (
                f"Resumed by operator after escalation (was: {values.get('escalation_reason') or 'unknown reason'}). "
                f"{budget_note}"
                "Continue the task from where you left off."
            )
            if req.message:
                resume_note += f"\n\nOperator note: {req.message}"
            patch["escalated"] = False
            patch["escalation_reason"] = None
            patch["pending_feedback"] = resume_note
            patch["no_diff_streak"] = 0  # fresh attempt -- don't inherit a streak from before the escalation
            # Same reasoning as no_diff_streak: an operator resume is a fresh
            # attempt. Without this, a task escalated for a maxed stale streak
            # resumes with that streak still at the limit and re-escalates on
            # its very first quiet pass -- zero real runway.
            patch["stale_pending_review_streak"] = 0
            await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
        elif was_done:
            # Same as_node-forces-re-routing mechanism as the escalated branch --
            # _route_after_verify checks escalated (False here) then
            # pending_approval (None) then pending_feedback, so a truthy
            # pending_feedback alone is enough to route back to "work".
            # no_diff_streak must reset to 0: it's sitting at 2 (that's exactly
            # what triggered "done" in the first place) -- without resetting it,
            # a work pass that produces no diff for any reason (including the
            # agent legitimately needing one more read-only turn before it can
            # act on the operator's note) would immediately re-trigger the same
            # "done, no changes needed" verdict before the nudge had a real
            # chance to land.
            patch["pending_feedback"] = f"Operator note: {req.message}"
            patch["no_diff_streak"] = 0
            await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
        else:
            # Orphaned/stopped resume -- an operator message here goes through
            # the same mailbox work_node already drains on its own next pass
            # (see agent/messages.py, work.py), not baked into this patch --
            # unlike the escalated branch, there's no synthetic pending_feedback
            # already being constructed here to fold it into, and routing must
            # stay untouched (see this function's own docstring).
            if req.message:
                add_message(task_id, req.message)
            await graph.aupdate_state(thread_config, patch)

        _running_tasks[task_id] = asyncio.create_task(
            _stream_graph(task_id, values["repo"], values["goal"], new_budget, None)
        )
        return {"ok": True, "new_budget_usd": new_budget, "new_max_iterations": new_max_iterations}


@app.get("/api/tasks/{task_id}/diff")
async def get_task_diff(task_id: str, user: User = Depends(require_full_auth)):
    """The task's current diff against its branch point -- committed AND
    uncommitted work, plus untracked files. Serves both halves of the diff
    panel: polled live while the task runs (watch the agent's edits land),
    and rendered as the final look when the task parks on awaiting_merge.
    Read-only; nothing from the request reaches a command line (repo resolves
    through PROJECTS, git output is parsed server-side)."""
    repo = await _resolve_task_repo(task_id)
    if not repo:
        raise HTTPException(404, "task not found")
    check_repo_access(user, repo)
    from agent.task_diff import collect_task_diff
    return await collect_task_diff(repo)


class MergeDecisionRequest(BaseModel):
    decision: str  # "approve" | "request_changes"
    message: str | None = None


@app.post("/api/tasks/{task_id}/merge-decision")
async def merge_decision(task_id: str, req: MergeDecisionRequest, user: User = Depends(require_full_auth)):
    """The operator's final-look verdict on a task parked at awaiting_merge.

    approve -> patch merge_approved_sha to the EXACT sha the operator was
    shown and re-invoke the graph: _route_after_verify sees approved+
    outstanding and runs verify_and_ship again, so the one code path that
    knows how to merge/record/finalize does the merge. The sha equality in
    that gate is what makes this race-safe -- approval can never ship a
    commit the operator didn't look at.

    request_changes -> the operator's notes become pending_feedback, exactly
    the shape a review-service rejection produces, so the agent loops back
    into work with the notes as its next instruction.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        pending = values.get("pending_merge_approval")
        if not pending:
            raise HTTPException(409, "task is not awaiting a merge decision")

        if req.decision == "approve":
            patch = {
                "merge_approved_sha": pending["sha"],
                "pending_merge_approval": None,
            }
        elif req.decision == "request_changes":
            if not (req.message or "").strip():
                raise HTTPException(400, "request_changes requires a message -- the agent needs to know what to change")
            patch = {
                "pending_merge_approval": None,
                "merge_approved_sha": None,
                "pending_feedback": (
                    "The operator reviewed the final diff and sent it back for more work "
                    "before it may merge. Their notes:\n\n" + req.message.strip()
                ),
                # Fresh attempt, same reasoning as resume_task's escalated branch.
                "no_diff_streak": 0,
                "stale_pending_review_streak": 0,
            }
        else:
            raise HTTPException(400, "decision must be 'approve' or 'request_changes'")

        await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")
        await audit.record(
            app.state.store, actor=user.email,
            action="merge.approve" if req.decision == "approve" else "merge.request_changes",
            target=f'{values["repo"]}/{task_id[:8]}',
            detail=pending.get("sha", "")[:12] if req.decision == "approve" else (req.message or "")[:200],
        )
        _running_tasks[task_id] = asyncio.create_task(
            _stream_graph(task_id, values["repo"], values["goal"], values.get("budget_usd", 0.0), None)
        )
        return {"ok": True, "decision": req.decision}


def _approval_summary(action_request: dict, count: int) -> str:
    """One line naming what was approved: the tool, and the part of its
    arguments a person would recognise. Truncated hard -- this is a log
    entry, not a transcript, and a pasted file can be megabytes."""
    name = action_request.get("name") or action_request.get("action") or "?"
    args = action_request.get("args") or action_request.get("arguments") or {}
    if isinstance(args, dict):
        shown = args.get("command") or args.get("file_path") or args.get("path") or ""
    else:
        shown = str(args)
    line = f"{name}: {str(shown)[:160]}" if shown else str(name)
    return f"{line} (+{count - 1} more)" if count > 1 else line


@app.post("/api/tasks/{task_id}/approve")
async def approve_task(task_id: str, req: ApprovalRequest, user: User = Depends(require_full_auth)):
    """Submits an operator decision on a pending human-in-the-loop approval
    request (deep_agent.py's INTERRUPT_ON -- a bash/write/edit call the
    coordinator or a subagent proposed that matched a sensitive-path/
    dangerous-command pattern). Structurally the same resume mechanism
    resume_task's escalated branch uses -- patch state `as_node=
    "verify_and_ship"` with a truthy `pending_feedback` so
    `_route_after_verify` routes to "work", and let work_node's own
    graph_input priority logic (approval_decision checked before
    pending_feedback -- see work.py's own docstring case 0) do the real
    work of constructing `Command(resume={"decisions": [...]})` against the
    same inner thread, resuming it exactly where interrupt() paused it.

    One decision applies uniformly to every action_request in this batch
    (approve-all or reject-all) -- deepagents' own protocol technically
    supports a distinct decision per action_request, but the model
    virtually always proposes one risky call at a time in practice, and a
    per-action-request UI is real added complexity for a case that's
    rare enough not to justify it in this first pass.
    """
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        graph = app.state.graph
        thread_config = {"configurable": {"thread_id": task_id}}
        checkpoint = await graph.aget_state(thread_config)
        if not checkpoint or not checkpoint.values:
            raise HTTPException(404, "task not found")
        values = checkpoint.values
        check_repo_access(user, values["repo"])

        pending = values.get("pending_approval")
        if not pending:
            raise HTTPException(409, "task has no pending approval request")

        action_count = len(pending.get("action_requests") or [])
        if req.decision == "approve":
            decisions = [{"type": "approve"} for _ in range(action_count)]
        elif req.decision == "respond":
            # ask_user answers: the operator's text IS the tool result (the
            # library's native "ask user"-style-tool pattern -- see deep_agent's
            # INTERRUPT_ON["ask_user"]). A respond without text is meaningless.
            if not (req.message or "").strip():
                raise HTTPException(400, "respond decision requires a message (the answer)")
            decisions = [{"type": "respond", "message": req.message} for _ in range(action_count)]
        else:
            reject = {"type": "reject"}
            if req.message:
                reject["message"] = req.message
            decisions = [dict(reject) for _ in range(action_count)]

        patch = {
            "task_id": task_id,
            "pending_approval": None,
            "approval_decision": decisions,
            # Placeholder text, never actually sent to the model -- work_node's
            # graph_input logic checks approval_decision first and uses that
            # instead whenever it's set (see work.py case 0). This exists
            # purely so _route_after_verify's existing "pending_feedback set ->
            # route to work" check fires, reusing that already-proven mechanism.
            "pending_feedback": "[operator submitted an approval decision]",
        }
        await graph.aupdate_state(thread_config, patch, as_node="verify_and_ship")

        # What was approved matters as much as that it was: the first action
        # request's tool and a short form of its arguments, so a later reader
        # can see which command a person let through.
        first = (pending.get("action_requests") or [{}])[0]
        await audit.record(
            app.state.store, actor=user.email,
            action="command.approve" if req.decision == "approve" else "command.reject",
            target=f'{values["repo"]}/{task_id[:8]}',
            detail=_approval_summary(first, action_count),
        )

        _running_tasks[task_id] = asyncio.create_task(
            _stream_graph(task_id, values["repo"], values["goal"], values["budget_usd"], None)
        )
        return {"ok": True, "decision": req.decision}


@app.get("/api/tasks/{task_id}")
async def get_task(task_id: str, repo: str, user: User = Depends(require_full_auth)):
    check_repo_access(user, repo)
    store = app.state.store
    meta = await _read_with_retry(lambda: store.aget(("tasks", repo), task_id))
    if not meta:
        raise HTTPException(404, "task not found")
    thread_config = {"configurable": {"thread_id": task_id}}
    checkpoint = await _read_with_retry(lambda: app.state.graph.aget_state(thread_config))
    values = checkpoint.values if checkpoint else None
    # Same "orphaned" condition resume_task already accepts (store says
    # "running" but nothing is actually driving it -- e.g. a backend restart
    # mid-run) -- surfaced here too so the frontend can offer Resume instead
    # of leaving the task looking merely slow forever.
    orphaned = bool(
        meta.value.get("status") == "running"
        and task_id not in _running_tasks
        and not (values or {}).get("escalated")
    )
    # _state_snapshot_for_frontend translates latest_todos -> plan (the
    # AgentState has no `plan` key at all -- see that function's own
    # docstring); without it, a page load/reconnect would show an empty
    # plan until the next live "todos" event happened to arrive.
    snapshot = _apply_plan_fallback(_state_snapshot_for_frontend(values) if values else None, meta.value)
    if snapshot is not None:
        # Cost freshness on hydrate (2026-08-28): the checkpoint's
        # cost_so_far only advances when a work pass RETURNS, so a refresh
        # mid-pass showed a stale number until the next live tick. The meta
        # mirror tracks live spend -- take whichever is higher (cost only
        # ever grows within a run).
        _meta_cost = meta.value.get("cost_so_far")
        if isinstance(_meta_cost, (int, float)) and _meta_cost > (snapshot.get("cost_so_far") or 0):
            snapshot["cost_so_far"] = _meta_cost
        # Full detailed history across refresh/task-switch -- see the live-log
        # buffer's own comment. The checkpoint's execution_log (per-pass
        # summaries) stays the durable fallback.
        # Three sources, fullest wins: this process's live buffer, the durable
        # transcript (survives a restart -- agent/planning_log.py), and the
        # checkpoint's per-pass summaries as the last resort.
        durable = await planning_log.load(store, repo, task_id,
                                          namespace=planning_log.TASK_NAMESPACE)
        snapshot["execution_log"] = _fuller_log(
            _live_task_log.get(task_id),
            _fuller_log(durable, snapshot.get("execution_log")))
    # Where this snapshot sits in the event stream. The browser opens its
    # socket first and buffers; on replay it drops anything at or below this,
    # so an event already folded into the snapshot is not applied twice.
    return {"meta": meta.value, "state": snapshot, "orphaned": orphaned,
            "seq": _task_event_seq.current(task_id)}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, repo: str, user: User = Depends(require_full_auth)):
    """Removes a finished task from the list entirely -- for work that was
    completed by hand (e.g. an operator finished it directly and merged/
    deployed outside the agent), where leaving it sitting as "stopped"
    forever would be misleading clutter, not a useful record. Refuses to
    delete a task that's still actively running -- stop it first, same as
    any other state-changing action here.
    """
    check_repo_access(user, repo)
    with _claim_run_slot(_running_tasks, task_id, "task is already running"):
        store = app.state.store
        meta = await store.aget(("tasks", repo), task_id)
        if not meta:
            raise HTTPException(404, "task not found")
        # BEFORE the two deletes below, and for the same reason
        # agent/consolidation.py indexes before it prunes. A task row and
        # its build transcript are corpora of the history index with NO
        # write-time hook -- unlike an episode, they are indexed only by the
        # nightly sync_project -- so a task deleted from the dashboard
        # before the next nightly run was never indexed at all, and
        # demote_missing cannot rescue it because there is no row to demote.
        # Best-effort, exactly like episodes.write_episode's own call: a
        # stored record that is not searchable is a gap the next sync
        # closes; a deleted record that was never indexed is gone.
        log_item = await store.aget((planning_log.TASK_NAMESPACE, repo), task_id)
        await history_index.index_task(
            config, repo, task_id, meta.value,
            log_item.value if log_item is not None else None)
        await store.adelete(("tasks", repo), task_id)
        # Nothing will ever stream for this task again.
        _live_task_log.pop(task_id, None)
        _task_recorders.pop(task_id, None)
        await planning_log.forget(store, repo, task_id, namespace=planning_log.TASK_NAMESPACE)
        _task_event_seq.forget(task_id)
        await app.state.checkpointer.adelete_thread(task_id)
        # Also delete the inner deep-agent thread's own checkpoints. Every "work"
        # pass runs the deep agent against a derived thread_id, f"{task_id}:work"
        # (see work.py's inner_thread_config), on the same checkpointer/DB as
        # the outer graph -- deleting only the outer task_id's thread would leave
        # that inner thread's checkpoints orphaned in the checkpoints table.
        await app.state.checkpointer.adelete_thread(f"{task_id}:work")
        # Fresh-restart generations (work.py's inner_thread_config with
        # generation > 0 -- see outer_state.py's inner_thread_generation) get
        # their own derived thread_ids too; delete those as well or they'd be
        # orphaned exactly the way the base :work thread would be. The actual
        # generation count lives in the outer thread's now-deleted checkpoint,
        # so sweep a bounded range instead -- adelete_thread on a nonexistent
        # thread is a harmless no-op, and MAX_THREAD_RESTARTS (currently 1)
        # keeps real generations far below this bound.
        for generation in range(1, 10):
            await app.state.checkpointer.adelete_thread(f"{task_id}:work:g{generation}")
        for _q, ws in _subscribers.pop(task_id, []):
            try:
                await ws.close(code=4000, reason="task deleted")
            except Exception:
                pass
        return {"ok": True}


# ---------------------------------------------------------------------------
# Project onboarding wizard (agent/provisioning.py)
#
# Admin-only, and deliberately three separate calls -- detect, then confirm,
# then provision. The middle step is not ceremony: detection can propose a
# check command that would run against a live production service, and the
# only reliable filter for that is a human who knows the system. See
# agent/provisioning.py's docstring.
# ---------------------------------------------------------------------------


class DetectProjectRequest(BaseModel):
    path: str


class ProvisionProjectRequest(BaseModel):
    # Only the path and the operator's answers cross the wire. `live` and
    # `sandbox` are deliberately NOT accepted: they were a filesystem write
    # primitive supplied by the client. The server re-runs detection and
    # derives both, then confirms the answers are a subset of what it just
    # proposed (agent/provisioning.validate_choices).
    path: str
    choices: dict
    grant_access: bool = True
    # The filename of an archive to restore into the new project, from the
    # `archives` list the detect step returned. A name, never a path -- see
    # project_removal.read_archive for the containment that enforces.
    restore_archive: str | None = None


async def _running_repos() -> set[str]:
    """Which projects have work in flight right now.

    Removing a project underneath a running task would pull its worktree out
    from under the agent mid-edit and leave a half-finished branch nobody
    owns, so removal refuses instead. Resolving each running task's repo from
    its checkpoint is a handful of reads; there are never many.
    """
    repos: set[str] = set()
    for task_id in list(_running_tasks):
        repo = await _resolve_task_repo(task_id)
        if repo:
            repos.add(repo)
    for session_id in list(_running_planning_turns):
        meta = await _find_planning_meta(session_id)
        if meta and meta.get("repo"):
            repos.add(meta["repo"])
    return repos


@app.get("/api/projects/archives")
async def list_project_archives(user: User = Depends(require_full_auth)):
    """Archived memory from removed projects, newest first."""
    auth.require_admin(user)
    from agent import project_removal  # noqa: PLC0415
    return {"archives": project_removal.list_archives()}


@app.delete("/api/projects/archives/{filename}")
async def delete_project_archive(filename: str, user: User = Depends(require_full_auth)):
    """Throw away one archive. Separate from removing a project so that
    forgetting a project and forgetting what it knew are two decisions."""
    auth.require_admin(user)
    from agent import project_removal  # noqa: PLC0415
    try:
        project_removal.delete_archive(filename)
    except project_removal.RemovalError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


def _checkout_verdict(name: str) -> dict:
    """Whether `name`'s checkout could be deleted along with the project.

    Split out so the answer the operator is shown and the answer the deletion
    acts on come from one place; the deletion asks again at the moment it
    would delete, because a working tree can change between the two.
    """
    from agent import project_removal  # noqa: PLC0415

    entry = PROJECTS.get(name) or {}
    live = entry.get("live", "")
    secrets = (entry.get("review") or {}).get("secretFiles") or []
    runs = project_removal.runs_on_this_box(entry)
    removable, reason = project_removal.checkout_disposable(live, secrets, runs)
    return {"live": live, "removable": removable, "reason": reason}


@app.get("/api/projects/{name}/checkout")
async def project_checkout_endpoint(name: str, user: User = Depends(require_full_auth)):
    """What deleting this project's checkout would cost, before anyone picks.

    Asked for when the removal panel opens rather than with the project list:
    it is several git commands per project, and nobody needs the answer until
    they are standing in front of the choice.
    """
    auth.require_admin(user)
    if name not in PROJECTS:
        raise HTTPException(404, f"no project named {name!r}")
    return await asyncio.to_thread(_checkout_verdict, name)


@app.delete("/api/projects/{name}")
async def remove_project_endpoint(name: str, req: RemoveProjectRequest,
                                  user: User = Depends(require_full_auth)):
    """Take a project off the agent.

    What this does NOT do is the important half: the live repository is left
    exactly as it is -- every file, every branch the agent ever pushed to it,
    and its remote. Removing a project means Tektonix forgets it, not that
    anybody's code goes away. The only thing touched inside the live repo is
    `core.sshCommand`, which is unset because the agent set it when it minted
    the deploy key, and leaving it would point the operator's own git at a key
    file that no longer exists.

    Each step reports independently, for the same reason provisioning does: a
    failure after the worktree is gone must not read as "nothing happened".
    """
    auth.require_admin(user)
    from agent import deploy_keys, project_removal  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH, reload_projects  # noqa: PLC0415

    entry = PROJECTS.get(name)
    if entry is None:
        raise HTTPException(404, f"no project named {name!r}")

    busy = await _running_repos()
    if name in busy:
        raise HTTPException(409, (
            f"{name} has work in flight -- stop the running task or planning turn first, "
            "or removing it would pull the workspace out from under the agent mid-edit"))

    live, sandbox = entry.get("live", ""), entry.get("sandbox", "")
    secret_files = (entry.get("review") or {}).get("secretFiles") or []
    runs_here = project_removal.runs_on_this_box(entry)

    # Checked here, before a single destructive step: a refusal after the
    # memory is archived and the worktree is gone is a half-removed project
    # and an operator with no idea which half.
    if req.files == "delete":
        ok, reason = await asyncio.to_thread(
            project_removal.checkout_disposable, live, secret_files, runs_here)
        if not ok:
            raise HTTPException(409, f"{live} cannot be deleted: {reason}")

    steps: list[dict] = []
    archived: str | None = None

    # Knowledge first: while the project is still configured, so a failure
    # here leaves it whole rather than half-removed and unreachable.
    store = getattr(app.state, "store", None)
    if store is not None:
        # Before anything is archived or purged. On Postgres the index is
        # where every already-pruned episode lives, so with no index object
        # in this process the archive would quietly omit those rows AND the
        # purge would leave every one of the project's rows behind in
        # agent_history_fts -- the project's own goal text surviving its
        # removal, which is exactly the leftover the removal tests exist to
        # catch. Refusing costs a restart; continuing costs both halves.
        if (backend_for_dsn(config.dsn) == "postgres"
                and history_index.default_index() is None):
            raise HTTPException(503, (
                f"the history index is not open in this process, so {name}'s searchable "
                "history could be neither archived nor removed -- nothing was removed; "
                "restart the server and try again"))
        if req.memory == "archive":
            try:
                doc = await project_removal.collect(store, name, history_index.default_index())
                path = await asyncio.to_thread(project_removal.write_archive, doc)
                archived = path.name
                steps.append({"step": "archive", "ok": True,
                              "detail": f"{doc['item_count']} item(s) saved to {path.name}"})
            except Exception:  # noqa: BLE001
                # Refuse rather than continue: the operator asked to keep this,
                # and deleting it anyway is the one mistake with no undo.
                logger.exception("remove: archiving %s failed", name)
                # The reason is in the log, not the response: an arbitrary
                # exception's text carries paths and internals that a caller
                # has no business seeing (CodeQL py/stack-trace-exposure).
                raise HTTPException(500, (
                    f"could not archive {name}'s memory, so nothing was removed "
                    "-- see the server log"))
        removed = await project_removal.purge(store, name, history_index.default_index())
        steps.append({"step": "memory", "ok": True,
                      "detail": f"{removed} item(s) {'archived and removed' if archived else 'deleted'}"})

    ok, detail = await asyncio.to_thread(project_removal.remove_worktree, live, sandbox)
    steps.append({"step": "workspace", "ok": ok, "detail": detail})

    try:
        deploy_keys.remove_key(name, live)
        steps.append({"step": "deploy-key", "ok": True,
                      "detail": "key deleted and the repo's core.sshCommand unset"})
    except Exception as e:  # noqa: BLE001 -- never fatal; the key is ours, not theirs
        steps.append({"step": "deploy-key", "ok": False,
                      "detail": _provisioning_public_error(e)})

    reviewer_state = paths.REPO_ROOT / "services" / "commit-reviewer" / "state.json"
    if project_removal.clear_reviewer_state(reviewer_state, name):
        steps.append({"step": "review-state", "ok": True, "detail": "last verdict cleared"})

    try:
        project_removal.remove_project_entry(_PROJECTS_CONFIG_PATH, name)
    except project_removal.RemovalError as e:
        raise HTTPException(500, str(e))
    reload_projects()
    steps.append({"step": "config", "ok": True,
                  "detail": f"{name} removed; the review services drop it on their next poll"})

    # Last, because everything above is recoverable and this is not.
    deleted = False
    if req.files == "delete":
        deleted, detail = await asyncio.to_thread(
            project_removal.delete_checkout, live, secret_files, runs_here)
        steps.append({"step": "checkout", "ok": deleted, "detail": detail})

    await audit.record(_audit_store(), actor=user.email, action="project.remove",
                       target=name, detail=f"memory {req.memory}, files {req.files}",
                       extra={"archive": archived, "checkout_deleted": deleted})

    return {"ok": True, "name": name, "steps": steps, "archive": archived,
            "live_untouched": None if deleted else live,
            "live_removed": live if deleted else None}


@app.get("/api/projects")
async def list_projects_config(user: User = Depends(require_full_auth)):
    """Every configured project with its full entry -- the wizard's landing
    view. Admin-only because the entries name host paths and credential
    filenames."""
    auth.require_admin(user)
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    return {
        "projects": PROJECTS,
        "config_path": str(_PROJECTS_CONFIG_PATH),
        "restart_required_hint": (
            "A project added from the dashboard is live immediately, and the review "
            "and deploy services re-read projects.json on their next poll. A project "
            "written to this file by hand or by scripts/add_project.py needs "
            "`pm2 restart tektonix` before the agent process sees it."
        ),
    }


class GitHubReposRequest(BaseModel):
    name: str | None = None      # a stored token, by label
    token: str | None = None     # or a pasted one, before saving


def _project_remote_slugs() -> dict[str, str]:
    """`owner/repo` (lowercased) -> project name, for every configured project
    whose checkout has a GitHub origin.

    Read from the checkouts rather than from projects.json, because how a
    project was added says nothing about where it lives now: one the operator
    typed a path for is just as likely to be on GitHub as one the agent
    cloned. Blocking calls -- run it in a thread.
    """
    import subprocess  # noqa: PLC0415

    from agent.tools import github_tools  # noqa: PLC0415

    known: dict[str, str] = {}
    for name, cfg in PROJECTS.items():
        live = (cfg or {}).get("live")
        if not live:
            continue
        try:
            out = subprocess.run(
                ["git", "config", "--local", "--get", "remote.origin.url"],
                capture_output=True, text=True, cwd=live, timeout=10, check=False,
            )
        except Exception:  # noqa: BLE001 -- a project whose checkout is gone is not this route's problem
            continue
        slug = github_tools.repo_slug_from_remote((out.stdout or "").strip())
        if slug:
            known[slug.lower()] = name
    return known


async def _onboarded_as(slug: str, token: str | None) -> str | None:
    """The project a repository is ALREADY onboarded as, or None.

    Tries the slug as given, then asks GitHub what each configured project's
    remote is called now. A repository transferred to an organisation leaves
    every checkout made before the move pointing at the old path, which GitHub
    still serves by redirect -- so the remote works, the slug no longer
    matches, and without this the same repository can be onboarded twice, one
    copy live and one a fresh clone beside it.

    The resolving loop is bounded by the number of configured projects whose
    remote did not match outright, and only runs on the manual add path.
    """
    from agent import github_repos as gh_repos  # noqa: PLC0415

    known = await asyncio.to_thread(_project_remote_slugs)
    hit = known.get(slug.lower())
    if hit or not token:
        return hit
    for other, name in known.items():
        if other == slug.lower():
            continue
        current = await gh_repos.resolve_slug(token, other)
        if current and current.lower() == slug.lower():
            return name
    return None


@app.post("/api/github/repos")
async def github_repos_endpoint(req: GitHubReposRequest, user: User = Depends(require_full_auth)):
    """Every repository a token can reach, and which are already onboarded.

    The token carries its own grant, so this is the honest answer to "which
    repositories are available" -- and it goes stale when the grant changes in
    GitHub rather than when somebody remembers to update something here.

    `onboarded` is matched on the remote each existing project actually has,
    not on how it was added. A project the operator typed a path for is just
    as likely to be on GitHub as one the agent cloned, and calling those
    different kinds of project is a distinction only this codebase can see.
    """
    auth.require_admin(user)
    from agent import github_repos as gh_repos, github_settings  # noqa: PLC0415

    raw = (req.token or "").strip()
    if not raw and req.name:
        settings = await github_settings.load(app.state.store)
        entry = settings["tokens"].get(req.name)
        if not entry:
            raise HTTPException(404, f"no token named {req.name!r}")
        raw = github_settings.decrypt_token(config, entry["enc"])
    if not raw:
        raise HTTPException(400, "give a stored token's name, or a token to try")

    try:
        repos = await gh_repos.list_accessible(raw)
    except PermissionError as e:
        raise HTTPException(400, str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"could not reach GitHub: {type(e).__name__}")

    # What each configured project's checkout actually points at.
    known = await asyncio.to_thread(_project_remote_slugs)

    # A checkout made before the repository was renamed or transferred still
    # has the old path in its origin, and GitHub keeps serving that by
    # redirect -- the remote works, the slug matches nothing below, and the
    # row offers to clone a project that is already here. Ask what each
    # unmatched one is called now. Bounded by the projects the token's own
    # list did not already account for, which is normally none.
    listed = {r["slug"].lower() for r in repos}
    for stale in [s for s in known if s not in listed]:
        current = await gh_repos.resolve_slug(raw, stale)
        if current and current.lower() != stale:
            known.setdefault(current.lower(), known[stale])

    for r in repos:
        r["onboarded_as"] = known.get(r["slug"].lower())
    return {"repos": repos, "onboarded": sorted(set(known.values()))}


class OnboardFromGitHubRequest(BaseModel):
    slug: str                    # owner/repo, from the repo list
    token_name: str | None = None
    ship: str | None = None      # push | pr; the default is asked for, not inferred


@app.post("/api/projects/onboard-github")
async def onboard_github_endpoint(req: OnboardFromGitHubRequest,
                                  user: User = Depends(require_full_auth)):
    """Clone a repository the token can reach, and provision it in one step.

    The long way round -- clone, read the report, tick the boxes -- still
    exists and is what somebody wants for a project with unusual checks. This
    is for the common case: a repository the operator can see in the list,
    onboarded with exactly the answers the wizard would have pre-ticked.
    """
    auth.require_admin(user)
    from agent import github_settings, provisioning  # noqa: PLC0415

    slug = (req.slug or "").strip()
    if not provisioning.parse_github_source(slug):
        raise HTTPException(400, f"{slug!r} is not a GitHub repository")

    token = getattr(config, "github_token", None)
    if req.token_name:
        settings = await github_settings.load(app.state.store)
        entry = settings["tokens"].get(req.token_name)
        if not entry:
            raise HTTPException(404, f"no token named {req.token_name!r}")
        token = github_settings.decrypt_token(config, entry["enc"])

    # A repository that is already a project must not become a second one.
    # The list this slug was picked from is built when somebody opens it, and
    # a transfer in GitHub after that moves a project's remote out from under
    # it -- so the refusal belongs here, where the clone is, and not only in
    # the list. Without it, adding a repository already onboarded under its
    # old path clones it again next to the live checkout, and two projects
    # then point at one repository.
    duplicate = await _onboarded_as(slug, token)
    if duplicate:
        raise HTTPException(
            409,
            f"{slug} is already onboarded as {duplicate!r}"
            + ("" if duplicate.lower() == slug.split("/")[-1].lower()
               else " (its checkout still has the path this repository had before it moved)"),
        )

    try:
        path = await asyncio.to_thread(
            provisioning.clone_repository, slug, existing_names=list(PROJECTS), token=token,
        )
        report = await asyncio.to_thread(
            provisioning.detect_project, path, existing_names=list(PROJECTS)
        )
        choices = provisioning.validate_choices(report, provisioning.recommended_choices(report))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    # Asked for, not inferred. A repository the agent cloned defaults to
    # opening pull requests, but the operator chose that in the list.
    choices["ship"] = req.ship if req.ship in ("push", "pr") else "pr"
    if report.blockers:
        raise HTTPException(400, "; ".join(report.blockers))

    ok, steps = await _provision_from_report(report, choices, user, True)
    return {"ok": ok, "name": report.name, "path": path, "steps": steps,
            "ship": choices["ship"], "slug": slug}


@app.post("/api/projects/clone")
async def clone_project_endpoint(req: DetectProjectRequest, user: User = Depends(require_full_auth)):
    """Clone a GitHub repository into an allowed root, then detect it.

    Separate from /detect on purpose: that one promises to create nothing, and
    an operator who pastes a URL expecting a look is entitled to that promise.
    This one says in its name that it writes.

    Everything after the clone is the ordinary onboarding path, against the
    path the clone produced -- the wizard, the worktree and projects.json do
    not know the directory arrived over the network.
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    source = req.path.strip()
    if not provisioning.parse_github_source(source):
        raise HTTPException(400, f"{source!r} is not a GitHub URL or owner/repo")
    try:
        path = await asyncio.to_thread(
            provisioning.clone_repository, source,
            # The project does not exist yet, so there is no per-project token
            # to prefer -- the environment fallback is the only one there is.
            existing_names=list(PROJECTS), token=getattr(config, "github_token", None),
        )
        report = await asyncio.to_thread(
            provisioning.detect_project, path, existing_names=list(PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    out = report.to_dict()
    out["cloned_to"] = path
    # Carried into provisioning so the entry's `ship` default reflects how the
    # project arrived. A repository the agent cloned is not one it was asked
    # to own, so it opens pull requests unless the operator says otherwise.
    out["cloned_from_github"] = True
    out["recommended_ship"] = "pr"
    from agent import project_removal  # noqa: PLC0415
    out["archives"] = project_removal.list_archives(report.name) if report.name else []
    return out


@app.post("/api/projects/detect")
async def detect_project_endpoint(req: DetectProjectRequest, user: User = Depends(require_full_auth)):
    """Read-only inspection of a candidate directory. Creates nothing.

    A GitHub URL is reported as such rather than treated as a path, so the
    wizard can offer to clone instead of failing with "no such directory".
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    if provisioning.parse_github_source(req.path.strip(), allow_slug=False):
        raise HTTPException(
            400,
            "that is a GitHub repository, not a path on this machine. "
            "Use Clone to bring it down first.",
        )
    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    from agent import project_removal  # noqa: PLC0415
    # A project removed earlier leaves its memory behind on purpose. Surfacing
    # it HERE is what closes the loop: the operator sees "there is archived
    # memory for a project called this" at the moment they are deciding to add
    # it, rather than discovering the file months later with no idea what it is.
    out = report.to_dict()
    out["archives"] = project_removal.list_archives(report.name) if report.name else []
    return out


@app.post("/api/projects/provision")
async def provision_project_endpoint(req: ProvisionProjectRequest, user: User = Depends(require_full_auth)):
    """Create the worktree, write the config entry, and seed the agent's
    knowledge for this project. Each step reports independently: a failure
    after the worktree exists must not read as "nothing happened".
    """
    auth.require_admin(user)
    from agent import provisioning  # noqa: PLC0415

    # Re-detect rather than trust the client's copy of the report: between the
    # wizard's two calls the directory may have changed, and a hand-made
    # request could otherwise assert facts (paths, commands) the server never
    # established.
    try:
        report = await asyncio.to_thread(
            provisioning.detect_project, req.path.strip(), existing_names=list(PROJECTS)
        )
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))
    if report.blockers:
        raise HTTPException(400, "; ".join(report.blockers))

    name = report.name
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "invalid project name")
    if name in PROJECTS:
        raise HTTPException(400, f"{name!r} is already configured")

    try:
        choices = provisioning.validate_choices(report, req.choices)
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, str(e))

    ok, steps = await _provision_from_report(report, choices, user, req.grant_access)
    if not ok:
        return {"ok": False, "steps": steps}

    # Onboarding hands an agent bash and write access to a directory, which
    # makes "who added this project, and when" a question worth being able to
    # answer later.
    if req.restore_archive:
        # After provisioning, never before: restoring memory into a project
        # whose worktree or config then failed to land would leave rows for a
        # project that does not exist.
        from agent import project_removal  # noqa: PLC0415
        try:
            doc = project_removal.read_archive(req.restore_archive)
            written = await project_removal.restore(app.state.store, name, doc,
                                                    history_index.default_index())
            steps.append({"step": "restore", "ok": True,
                          "detail": f"{written} item(s) restored from {req.restore_archive}"})
        except Exception as e:  # noqa: BLE001 -- the project is already live; this is additive
            logger.exception("provision: restoring %s failed", req.restore_archive)
            steps.append({"step": "restore", "ok": False,
                          "detail": _provisioning_public_error(e)})

    await audit.record(_audit_store(), actor=user.email, action="project.onboard",
                       target=name, detail=report.live)

    return {
        "ok": True,
        "steps": steps,
        "message": f"{name} is configured and live in this process.",
    }


def _provisioning_public_error(e: Exception) -> str:
    """What a step may say about a failure: a ProvisioningError's own
    message (written for the operator), otherwise a pointer to the log
    -- an arbitrary exception's text is not for the response (CodeQL
    py/stack-trace-exposure)."""
    from agent import provisioning  # noqa: PLC0415

    if isinstance(e, provisioning.ProvisioningError):
        return e.detail
    logger.exception("provisioning step failed")
    return "failed -- see the server log"


async def _provision_from_report(report, choices: dict, user: User,
                                 grant_access: bool, *,
                                 fresh_workspace: bool = False) -> tuple[bool, list[dict]]:
    """Everything after the operator's answers are validated: worktree,
    projects.json entry, in-process reload, knowledge seeding, access.

    Shared verbatim by /api/projects/provision (the wizard) and
    /api/projects/create (a repo this server just made), so a project that
    arrives by either door ends up wired identically. Returns (ok, steps);
    ok is False only when a step the project cannot exist without failed.

    `fresh_workspace` is set by the create door: that project has never run,
    so an existing workspace at its path is another project's leftover rather
    than its own, and adopting it would run every task against the wrong repo.
    """
    from agent import provisioning  # noqa: PLC0415
    from agent.config import _PROJECTS_CONFIG_PATH  # noqa: PLC0415

    name = report.name
    steps: list[dict] = []
    _public_error = _provisioning_public_error

    def _step(label: str, ok: bool, detail: str = "") -> None:
        steps.append({"step": label, "ok": ok, "detail": detail})

    logger.info("onboarding: %s provisioning %s from %s", user.email, name, report.live)

    ok, detail = await asyncio.to_thread(
        functools.partial(provisioning.create_worktree, report.live, report.sandbox,
                          must_be_new=fresh_workspace))
    _step("worktree", ok, detail)
    if not ok:
        return False, steps

    entry = provisioning.config_from_choices(name, report.live, report.sandbox, choices)
    try:
        await asyncio.to_thread(provisioning.write_project_entry, _PROJECTS_CONFIG_PATH, name, entry)
        _step("config", True, f"wrote {name} to projects.json")
    except (provisioning.ProvisioningError, OSError, ValueError) as e:
        _step("config", False, _public_error(e))
        return False, steps

    # Load the new entry into the RUNNING process. Without this the project
    # exists in projects.json and nowhere else -- every consumer holds the
    # dict read at import, so the cartographer below would KeyError on it and
    # the project would stay invisible until a restart.
    from agent.config import reload_projects  # noqa: PLC0415

    reload_projects()
    _step("reload", True, f"{len(PROJECTS)} projects now live in this process")

    # Knowledge seeding. Best-effort by design: a project whose map failed to
    # build is still a usable project and the operator can re-run the
    # cartographer. Never fail the whole onboarding over it.
    try:
        from agent.deep_agent import seed_memory  # noqa: PLC0415

        starter = (
            f"# {name} project memory\n\n"
            "Durable, cross-task facts about this project. The consolidator appends what "
            "it learns from completed tasks; add anything an agent must know before "
            "touching this repo.\n"
        )
        await seed_memory(name, app.state.store, starter)
        _step("memory", True, "seeded starter project memory")
    except Exception as e:  # noqa: BLE001 -- reported, never fatal
        _step("memory", False, _public_error(e))

    # Started, not awaited. Reading a whole repository is minutes of model
    # calls on anything real, and awaiting it here held the HTTP response open
    # for all of them -- past the browser's own timeout, which aborts the
    # request while the server carries on and finishes. The operator then sees
    # a failure next to a project that does in fact exist, and the obvious
    # next move is to add it again. The map is best-effort by design (a
    # project without one is a usable project), so it belongs off this path.
    _spawn_background(
        cartographer.run_cartographer(config, name, app.state.store, force=True),
        f"cartographer:{name}",
    )
    _step("codebase-map", True,
          f"building in the background -- agents get it when it lands; "
          f"scripts/run_cartographer.py {name} re-runs it")

    if grant_access and user.allowed_repos is not None:
        try:
            await auth.update_user_access(app.state.auth_pool, user.id,
                                          [*user.allowed_repos, name])
            _step("access", True, f"granted {user.email} access to {name}")
        except Exception as e:  # noqa: BLE001
            _step("access", False, _public_error(e))

    return True, steps


class CreateProjectRequest(BaseModel):
    # `parent` is the directory the new repo is created UNDER, never the repo
    # path itself: the name is validated separately (one path component) and
    # the join is re-checked for containment, so a client cannot pick an
    # arbitrary location any more than the wizard's `path` can.
    name: str
    description: str = ""
    parent: str | None = None
    github: bool = False
    token_name: str | None = None


def _resolve_github_token(token_name: str | None) -> tuple[str, str | None]:
    """Returns (token, stored_name). A named stored token if one was asked
    for, else the GITHUB_TOKEN env fallback, else the single stored token if
    that is unambiguous. Raises the HTTP error the endpoint should answer
    with; the token itself never goes anywhere but the request headers in
    agent/github_repos.py.

    The unambiguous-stored fallback exists because the dashboard decides
    whether to offer "create a private GitHub repo" from
    GET /api/settings/github, which reports stored tokens AND the env one --
    so on a box with a token saved in Settings and no GITHUB_TOKEN (the
    normal shape, since Settings is the documented place to put it) the
    checkbox was offered and the request then died on "no GitHub token is
    configured". Stored-first also matches github_settings.token_for's own
    precedence for every other GitHub call.
    """
    tokens = github_settings.current()["tokens"]
    if token_name:
        entry = tokens.get(token_name)
        if not entry:
            raise HTTPException(400, f"no stored GitHub token named {token_name!r} (Settings -> GitHub)")
        return github_settings.decrypt_token(config, entry["enc"]), token_name
    if len(tokens) == 1:
        only = next(iter(tokens))
        return github_settings.decrypt_token(config, tokens[only]["enc"]), only
    if len(tokens) > 1 and not getattr(config, "github_token", None):
        raise HTTPException(400, (
            f"several GitHub tokens are stored ({', '.join(sorted(tokens))}) and none was chosen -- "
            "name one in the request, or set GITHUB_TOKEN"))
    if getattr(config, "github_token", None):
        return config.github_token, None
    raise HTTPException(400, "no GitHub token is configured (Settings -> GitHub)")


def _rollback_new_project(live: str, name: str, github_info: dict | None, steps: list[dict]) -> None:
    """Remove the directory this call created when a later step fails, so the
    retry the dashboard offers is a real retry.

    Without this, "create" was a one-shot: create_repository only cleans up
    after its own git failure, so a project whose detect/worktree/config step
    died left <parent>/<name> on disk, and the second attempt -- with the same
    name, from a form the UI deliberately keeps filled in -- hit "already
    exists" and could never succeed.

    Not when a GitHub repository was created: that one is not ours to throw
    away silently, and the local checkout is the only copy of its deploy-key
    config. The operator gets the path and onboards it with the wizard.
    """
    if github_info:
        steps.append({"step": "rollback", "ok": True,
                      "detail": f"kept {live} (its GitHub repo {github_info['full_name']} exists); "
                                "onboard it from Settings -> Projects, or delete both to start over"})
        return
    try:
        shutil.rmtree(live)
    except OSError as e:
        steps.append({"step": "rollback", "ok": False,
                      "detail": f"could not remove {live}: {e}"})
        return
    steps.append({"step": "rollback", "ok": True,
                  "detail": f"removed {live}, so {name} can be created again"})


@app.post("/api/projects/create")
async def create_project_endpoint(req: CreateProjectRequest, user: User = Depends(require_full_auth)):
    """Start a project from nothing -- see _create_project, which the
    planner's confirm route (decide_planning_new_project) shares verbatim so
    a project arrives wired identically whichever door it came through."""
    auth.require_admin(user)
    return await _create_project(req, user)


async def _create_project(req: CreateProjectRequest, user: User) -> dict:
    """A git repo with one commit under an allowed root, optionally mirrored
    to a new PRIVATE GitHub repository, then provisioned exactly as the
    wizard would with the recommended answers. The caller has already
    checked the admin role.

    Order matters. The token is resolved before anything is created so a
    missing token is a clean 400 with no directory left behind; the GitHub
    steps run before detection so a push failure is reported alongside the
    local steps rather than losing the project; and the GitHub failure is a
    failed step, not an abort -- the repo on disk is real and usable, and
    the operator can connect it from the deploy-key panel later.
    """
    from agent import provisioning, github_repos, deploy_keys  # noqa: PLC0415

    try:
        name = provisioning.validate_project_name(req.name, list(PROJECTS))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, e.detail)

    token: str | None = None
    stored_token_name: str | None = None
    if req.github:
        token, stored_token_name = _resolve_github_token(req.token_name)

    try:
        live = await asyncio.to_thread(
            provisioning.create_repository, req.parent, name,
            description=req.description, existing_names=list(PROJECTS))
    except provisioning.ProvisioningError as e:
        raise HTTPException(400, e.detail)

    steps: list[dict] = [{"step": "repository", "ok": True,
                          "detail": f"initialised {live} with one commit on main"}]
    github_info: dict | None = None

    if req.github and token:
        # Host-side push -- see agent/github_repos.py for why this is allowed
        # here and nowhere an agent runs.
        try:
            created = await github_repos.create_private_repo(token, name, req.description)
            github_info = {"full_name": created["full_name"], "html_url": created["html_url"]}
            public_key = await asyncio.to_thread(
                github_repos.connect_origin, live, created["ssh_url"], name)
            await github_repos.add_deploy_key(token, created["full_name"],
                                              f"tektonix-{name}", public_key)
            ok, detail = await asyncio.to_thread(github_repos.push_initial, live, name)
            steps.append({"step": "github", "ok": ok,
                          "detail": f"{created['full_name']}: {detail}" if ok else detail})
        except (PermissionError, LookupError, ValueError, deploy_keys.DeployKeyError) as e:
            # These messages are written by github_repos/deploy_keys for the
            # operator and carry neither the token nor a response body.
            steps.append({"step": "github", "ok": False, "detail": str(e)[:400]})
        except httpx.HTTPError as e:
            logger.exception("github: creating the repository for %s failed", name)
            steps.append({"step": "github", "ok": False,
                          "detail": f"GitHub request failed ({type(e).__name__}) -- see the server log"})
        if stored_token_name and github_info:
            # So token_for(name) -- the inbox poller, the PR tools -- reaches
            # this repo with the same token that created it.
            try:
                await github_settings.save(app.state.store, config,
                                           {"projects": {name: {"token": stored_token_name}}})
                steps.append({"step": "github-token", "ok": True,
                              "detail": f"{name} uses the stored token {stored_token_name!r}"})
            except Exception as e:  # noqa: BLE001 -- reported, never fatal
                steps.append({"step": "github-token", "ok": False,
                              "detail": _provisioning_public_error(e)})

    try:
        report = await asyncio.to_thread(provisioning.detect_project, live,
                                         existing_names=list(PROJECTS))
        if report.blockers:
            raise provisioning.ProvisioningError("; ".join(report.blockers))
        choices = provisioning.validate_choices(report, provisioning.recommended_choices(report))
    except provisioning.ProvisioningError as e:
        steps.append({"step": "detect", "ok": False, "detail": e.detail})
        _rollback_new_project(live, name, github_info, steps)
        return {"ok": False, "name": name, "live": live, "steps": steps, "github": github_info}
    steps.append({"step": "detect", "ok": True,
                  "detail": f"{len(report.checks)} check(s), {len(report.warnings)} warning(s)"})

    ok, provision_steps = await _provision_from_report(report, choices, user, True,
                                                       fresh_workspace=True)
    steps.extend(provision_steps)
    if not ok:
        # Only when nothing was registered: once the projects.json entry is
        # written the project exists, and removing its checkout underneath a
        # configured name is worse than leaving a half-provisioned one.
        if name not in PROJECTS:
            _rollback_new_project(live, name, github_info, steps)
        return {"ok": False, "name": name, "live": live, "steps": steps, "github": github_info}

    await audit.record(_audit_store(), actor=user.email, action="project.create",
                       target=name, detail=live,
                       extra={"github": github_info["full_name"] if github_info else None})

    return {
        "ok": True,
        "name": name,
        "live": live,
        "steps": steps,
        "github": github_info,
        "message": f"{name} is created, configured and live in this process.",
    }


# ---------------------------------------------------------------------------
# Per-project deploy keys (agent/deploy_keys.py)
#
# The private half is write-only across this API: it can be installed,
# generated and replaced, and its fingerprint/public half can be read, but
# nothing here returns it. Admin-only, like every other credential surface.
# ---------------------------------------------------------------------------


class DeployKeyRequest(BaseModel):
    private_key: str


def _project_live_or_404(name: str) -> str:
    project = PROJECTS.get(name)
    if not project:
        raise HTTPException(404, f"unknown project {name!r}")
    return project["live"]


@app.get("/api/projects/{name}/deploy-key")
async def get_deploy_key_status(name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        return (await asyncio.to_thread(deploy_keys.status, name, live)).to_dict()
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))


@app.post("/api/projects/{name}/deploy-key")
async def install_deploy_key(name: str, req: DeployKeyRequest,
                             user: User = Depends(require_full_auth)):
    """Install a pasted private key for this project."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        st = await asyncio.to_thread(deploy_keys.install_key, name, live, req.private_key)
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))
    logger.info("deploy key installed for %s by %s", name, user.email)
    return st.to_dict()


@app.post("/api/projects/{name}/deploy-key/generate")
async def generate_deploy_key(name: str, user: User = Depends(require_full_auth)):
    """Mint a fresh keypair. Preferred over pasting: the operator never
    handles the private half -- they copy the public half out of the
    response and register it on the remote."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    try:
        st = await asyncio.to_thread(deploy_keys.generate_key, name, live)
    except deploy_keys.DeployKeyError as e:
        raise HTTPException(400, str(e))
    logger.info("deploy key generated for %s by %s", name, user.email)
    # A deploy key is push access to the real repository. The log line above
    # is in a file that rotates; this one is in the store.
    await audit.record(_audit_store(), actor=user.email, action="deploy_key.generate",
                       target=name, detail=st.to_dict().get("fingerprint"))
    return st.to_dict()


@app.post("/api/projects/{name}/deploy-key/test")
async def test_deploy_key(name: str, user: User = Depends(require_full_auth)):
    """Contact the remote exactly the way the post-merge push will."""
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    ok, detail = await asyncio.to_thread(deploy_keys.check_remote, name, live)
    return {"ok": ok, "detail": detail}


@app.delete("/api/projects/{name}/deploy-key")
async def delete_deploy_key(name: str, user: User = Depends(require_full_auth)):
    auth.require_admin(user)
    from agent import deploy_keys  # noqa: PLC0415

    live = _project_live_or_404(name)
    st = await asyncio.to_thread(deploy_keys.remove_key, name, live)
    logger.info("deploy key removed for %s by %s", name, user.email)
    await audit.record(_audit_store(), actor=user.email, action="deploy_key.delete", target=name)
    return st.to_dict()


def _tail_lines(path: Path, count: int, block: int = 64 * 1024) -> str:
    """Last `count` lines without reading the whole file.

    The consolidation log is appended to on every cron run and never rotated,
    so a full read grows without bound -- and it happened on the event loop.
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            data = b""
            while end > 0 and data.count(b"\n") <= count:
                step = min(block, end)
                end -= step
                fh.seek(end)
                data = fh.read(step) + data
        return "\n".join(data.decode(errors="replace").splitlines()[-count:])
    except OSError as e:
        return f"(could not read {path}: {e})"


@app.get("/api/consolidation/status")
async def consolidation_status(user: User = Depends(require_full_auth)):
    """Last nightly memory-consolidation run: when, and whether it succeeded.

    Exists because a failed run used to be indistinguishable from a healthy one
    — the script printed a line and exited 0, so cron stayed quiet and a provider
    incompatibility skipped consolidation unnoticed for months. The marker file
    is written by scripts/consolidation-cron.sh on every run.
    """
    auth.require_admin(user)
    root = Path(__file__).resolve().parent.parent
    marker = root / "data" / "last_consolidation.json"
    log = Path("/var/log/agent-consolidation.log")

    # stale defaults False, not True: a default of True combined with a silent
    # except-pass meant any error here rendered a healthy run as STALE. That is
    # the same silent-failure shape this panel exists to catch, so failures to
    # parse are reported as `stale_error` rather than swallowed into a verdict.
    payload: dict = {"ran_at": None, "ok": None, "exit_code": None, "stale": False, "tail": ""}
    try:
        payload.update(json.loads(await asyncio.to_thread(marker.read_text)))
    except FileNotFoundError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning("consolidation status: marker unreadable: %s", e)
        payload["marker_error"] = "marker file unreadable -- see the server log"

    # Stale = no run in over 48h. The job is nightly, so one missed night is
    # worth surfacing rather than waiting for someone to read a log.
    if payload.get("ran_at"):
        try:
            ran = _dt.fromisoformat(str(payload["ran_at"]).replace("Z", "+00:00"))
            age_h = (_dt.now(UTC) - ran).total_seconds() / 3600
            payload["age_hours"] = round(age_h, 1)
            payload["stale"] = age_h > 48
        except Exception as e:  # noqa: BLE001
            logger.warning("consolidation status: could not parse ran_at: %s", e)
            payload["stale_error"] = "could not parse ran_at -- see the server log"

    try:
        # to_thread, and only the tail: this log is never rotated, so
        # read_text() pulled the WHOLE file into memory on the event loop --
        # every other request stalled behind it, and the cost grew with the
        # file. _tail_lines seeks from the end instead.
        payload["tail"] = await asyncio.to_thread(_tail_lines, log, 40)
    except Exception:
        pass
    return payload


@app.websocket("/api/tasks/{task_id}/stream")
async def stream_task(ws: WebSocket, task_id: str):
    user = await auth.get_user_from_ws_cookie(app.state.auth_pool, ws.cookies)
    if not user:
        await ws.close(code=4401)
        return
    # audit H-1: same forced-screen enforcement as the planning stream above.
    if _forced_screen_block(user):
        await ws.close(code=4403)
        return
    task_repo = await _resolve_task_repo(task_id)
    # Fail CLOSED, same reasoning as the REST endpoints above: an
    # unresolvable repo previously meant "no check", which let any
    # authenticated user attach to the stream.
    if not task_repo or not user.can_access(task_repo):
        await ws.close(code=4403)
        return
    await ws.accept()

    # Every connection for this task_id gets its own queue and stays
    # subscribed for its own lifetime -- multiple viewers (different users,
    # or the same user in two tabs) are simultaneously live on the same
    # task, all receiving the same fan-out from _publish(). This used to
    # evict every prior connection the moment a new one arrived ("only the
    # newest connection should ever receive events"), which was fine when
    # this system had exactly one operator but broke outright the moment a
    # second real user existed -- whoever opened the task last would silently
    # kick everyone else's live view. Each connection's own receiver() below
    # still detects and cleans up ITS OWN disconnect independently, so a
    # genuinely stale/dead connection is removed on its own without needing
    # to evict anyone else's.
    # audit M-34: bounded, so a stalled-but-connected reader can't accumulate
    # every event of a multi-hour task in memory (_publish drops on overflow).
    queue: asyncio.Queue = asyncio.Queue(maxsize=_SUBSCRIBER_QUEUE_MAX)
    _subscribers.setdefault(task_id, []).append((queue, ws))

    async def sender():
        while True:
            # Heartbeat (2026-08-28): a long model call can mean 60s+ of
            # total socket silence, and NAT/middleboxes between the operator
            # and this VPS kill idle TCP without telling either end -- the
            # browser's stream just goes still until a manual refresh
            # (reported live). A ping every 20s keeps bytes flowing through
            # every hop, and when the socket IS dead, send_json raises here
            # promptly so the client gets a real close event to react to
            # instead of silence. Clients ignore the "ping" type.
            try:
                event = await asyncio.wait_for(queue.get(), timeout=20)
            except TimeoutError:
                await ws.send_json({"type": "ping"})
                continue
            await ws.send_json(event)
            if event.get("type") == "closed":
                break

    async def receiver():
        # The client never actually sends anything on this socket -- this
        # exists purely to detect a disconnect promptly. `await queue.get()`
        # in sender() has no way to notice the client closed the connection;
        # it only finds out lazily, the next time it tries to send and that
        # fails. Between those two points the stale queue stays subscribed,
        # which is harmless now beyond a handful of buffered events never
        # being delivered anywhere -- the `finally` block below still always
        # removes exactly this one connection's own entry, never another
        # viewer's.
        while True:
            await ws.receive()

    sender_task = asyncio.create_task(sender())
    receiver_task = asyncio.create_task(receiver())
    try:
        done, pending = await asyncio.wait([sender_task, receiver_task], return_when=asyncio.FIRST_COMPLETED)
        # asyncio.wait() does not propagate exceptions from the tasks it
        # waits on (unlike awaiting a task directly) -- WebSocketDisconnect
        # from receiver() would otherwise never surface anywhere and could
        # log an "exception was never retrieved" warning. Retrieving it here
        # (without re-raising -- a disconnect is the expected, normal way
        # this ends) marks it handled either way.
        for task in done:
            task.exception()
        for task in pending:
            task.cancel()
    finally:
        entry = (queue, ws)
        if entry in _subscribers.get(task_id, []):
            _subscribers[task_id].remove(entry)
        # audit M-34: drop the now-empty list so the dict doesn't keep one stale
        # key per task forever.
        if task_id in _subscribers and not _subscribers[task_id]:
            del _subscribers[task_id]


# Static frontend, mounted last so it never shadows an /api/* route above.
# A single-page app with one real route today, but the catch-all fallback
# means adding client-side routes later won't need a matching nginx change.
#
# Cache headers are the whole reason this isn't just a bare StaticFiles
# mount. Nothing here sent any Cache-Control at all, which does NOT mean
# "don't cache" -- with only a Last-Modified to go on, browsers fall back to
# heuristic caching and are free to reuse index.html for a while. index.html
# is the file that names the content-hashed bundle, so a stale copy of it
# pins the browser to the PREVIOUS deploy's JS/CSS: new code ships, the
# server serves it correctly, and the operator still sees the old UI until
# they happen to hard-reload. Confirmed live 2026-08-23 (this exact bug: a
# rebuilt chat composer was being served and simply never appeared).
#
# The fix is the standard split for hashed-asset SPAs:
#   * index.html      -> no-store. Never reused; every load re-reads which
#                        bundle is current. It's ~400 bytes, so revalidating
#                        it on each load costs nothing.
#   * /assets/*       -> immutable, one year. Safe precisely BECAUSE the
#                        filename contains a content hash -- different
#                        content is always a different URL, so a cached copy
#                        can never be stale. This is what keeps the split
#                        cheap: the tiny file is always fetched, the 690KB
#                        one is cached hard.
class _ImmutableAssets(StaticFiles):
    """StaticFiles that marks content-hashed bundles permanently cacheable."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
        return response


def _not_modified(request: Request, response: FileResponse) -> bool:
    """Answer a conditional request the way StaticFiles does.

    FileResponse sets etag and last-modified but never checks the request
    against them, so `no-cache` below would mean re-sending the whole file on
    every load rather than the 304 the browser is asking for. ETag wins over
    Last-Modified when both are present, per RFC 9110.
    """
    etag = response.headers.get("etag")
    if_none_match = request.headers.get("if-none-match")
    if etag and if_none_match:
        candidate = etag.strip('"')
        for token in if_none_match.split(","):
            token = token.strip()
            if token == "*":
                return True
            # W/ marks a weak validator; the tag itself is what compares.
            if token.removeprefix("W/").strip('"') == candidate:
                return True
        return False

    modified_since = request.headers.get("if-modified-since")
    last_modified = response.headers.get("last-modified")
    if modified_since and last_modified:
        try:
            return _parsedate(last_modified) <= _parsedate(modified_since)
        except (TypeError, ValueError):
            return False
    return False


# --- the review dashboard, proxied ------------------------------------------

# The review services listen on 4100/4101 and hold the only write path into a
# live repo, so they bind loopback and publish nothing. On a host install nginx
# bridges the console to them at /_review/, injecting the shared secret the
# browser must never hold. In the container bundle there is no nginx, so the
# gate ran and the agent drove it while a person could not: Check now, the diff
# view and the manual merge were host-install only.
#
# This is that bridge, in the one process that is already the authenticated
# front door. It changes nothing about who may call: admin, over the session
# the console already required, exactly as before. The ports stay unpublished.
_REVIEW_PROXY_HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
})

# Merge and restart genuinely take minutes on a large project, and nginx allows
# half an hour for exactly that reason. A shorter limit here would turn a slow
# deploy into a failed one.
_REVIEW_PROXY_TIMEOUT = 1800.0


@app.api_route("/_review/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
               include_in_schema=False)
async def review_proxy(path: str, request: Request, user: User = Depends(require_full_auth)):
    auth.require_admin(user)

    from agent.tools.review_gate import REVIEW_SERVICE_HOST, REVIEW_SERVICE_PORT  # noqa: PLC0415

    # The secret is SET here, never forwarded. A client that sends its own
    # X-Review-Secret must not be able to influence what the review service
    # sees -- this endpoint's authority comes from the session, not the header.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _REVIEW_PROXY_HOP_BY_HOP and k.lower() != "x-review-secret"
    }
    secret = os.environ.get("REVIEW_CONTROL_SECRET")
    if secret:
        headers["X-Review-Secret"] = secret

    url = f"http://{REVIEW_SERVICE_HOST}:{REVIEW_SERVICE_PORT}/{path}"
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=_REVIEW_PROXY_TIMEOUT) as client:
            upstream = await client.request(
                request.method, url, params=request.query_params,
                content=body or None, headers=headers,
            )
    except httpx.HTTPError as e:
        # The service being down is an ordinary state -- a restart, a bundle
        # where it was not started. Say which service, because "502" from the
        # console looks like the console.
        return _JSONResponse(
            {"ok": False, "error": f"the review service is not reachable: {type(e).__name__}"},
            status_code=502,
        )

    passthrough = {
        k: v for k, v in upstream.headers.items()
        if k.lower() not in _REVIEW_PROXY_HOP_BY_HOP
    }
    return Response(content=upstream.content, status_code=upstream.status_code,
                    headers=passthrough)


if FRONTEND_DIST.is_dir():
    app.mount("/assets", _ImmutableAssets(directory=FRONTEND_DIST / "assets"), name="assets")

    # HEAD as well as GET: FastAPI's @app.get registers exactly the methods
    # named, unlike Starlette's plain Route, which folds HEAD in for free. A
    # bare @app.get therefore 405s every HEAD -- including the one a link-
    # preview crawler sends to size an og:image before fetching it.
    dist_root = os.path.normpath(str(FRONTEND_DIST.resolve()))

    def _dist_root_file(full_path: str) -> Path | None:
        """A real file directly inside dist/, or None. One path segment only
        (a favicon, the apple-touch icon, og-preview.png): anything with a
        separator or a dot-segment is not a root file, and the normalised
        path must still sit under dist after joining -- the check static
        analysers look for (CodeQL py/path-injection), on top of the
        is_relative_to containment."""
        if not full_path or "/" in full_path or "\\" in full_path or full_path in (".", ".."):
            return None
        joined = os.path.normpath(os.path.join(dist_root, full_path))
        if not joined.startswith(dist_root + os.sep):
            return None
        candidate = Path(joined)
        if not candidate.is_file() or candidate.parent != Path(dist_root):
            return None
        return candidate

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
    async def spa_fallback(full_path: str, request: Request):
        # Real files at the dist root — favicons, the apple-touch icon — are
        # served as themselves. Before this, ONLY /assets was mounted and every
        # other path fell through to index.html, so the favicon <link>s fetched
        # 200 text/html and the tab never showed an icon (silently: a 200 with
        # the wrong body looks fine in every log). Resolved-and-contained check
        # rather than trusting the path: `..` segments must not escape dist.
        candidate = _dist_root_file(full_path)
        if candidate is not None:
            # Stable names, so freshness has to come from revalidation
            # rather than a lifetime: FileResponse already sends etag and
            # last-modified, and a max-age here would be exactly how long a
            # replaced icon or og:image outlives its deploy. Replacing the
            # brand art on another application on 2026-08-29 hit precisely that
            # -- correct bytes on disk, a day of stale ones in every cache.
            # stat_result up front: FileResponse only sets etag and
            # last-modified when it is given one, otherwise it stats
            # lazily inside __call__ -- and the conditional check below
            # would then be comparing against headers that do not exist
            # yet, so every revalidation came back 200 with the full body.
            response = FileResponse(
                candidate,
                headers={"cache-control": "public, no-cache"},
                stat_result=candidate.stat(),
            )
            if _not_modified(request, response):
                # Starlette's own 304, not a Response carrying this one's
                # headers: those include content-length, and a 304 has no
                # body, so uvicorn raised "Response content shorter than
                # Content-Length" on every browser revalidation of an icon.
                # NotModifiedResponse keeps only what a 304 may carry.
                return NotModifiedResponse(response.headers)
            return response
        return FileResponse(
            FRONTEND_DIST / "index.html",
            headers={"cache-control": "no-store, must-revalidate"},
        )
