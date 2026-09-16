"""The health endpoint: what it checks, what it refuses to leak, and that it
goes red for the right reasons.

There was no health route at all until 2026-09-11. Every restart check in this
repo's own history curled /api/health, got the SPA's index.html with a 200, and
concluded the process was healthy -- which proved only that uvicorn was serving
static files. These tests exist so the route keeps meaning something.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import agent.health as health
import agent.server as srv
from agent.auth import User

_ADMIN = User(id=1, email="a@e.com", role="admin", allowed_repos=None, totp_enabled=True,
              must_change_password=False, auto_approve_commands=False, require_merge_review=True)


class _Cur:
    async def fetchone(self):
        return (1,)


class _Conn:
    def __init__(self, fail=False):
        self.fail = fail

    async def execute(self, sql, params=None):
        if self.fail:
            raise RuntimeError("connection refused")
        return _Cur()


class FakePool:
    """Mimics psycopg's `async with pool.connection() as conn`."""
    def __init__(self, fail=False):
        self.fail = fail

    def connection(self):
        pool = self

        class _Ctx:
            async def __aenter__(self):
                if pool.fail:
                    raise RuntimeError("pool exhausted")
                return _Conn()

            async def __aexit__(self, *a):
                return False

        return _Ctx()


@pytest.fixture
def all_good(monkeypatch):
    monkeypatch.setattr(health, "_image_cache", None)
    monkeypatch.setattr(health, "_image_present", lambda: True)

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(health.httpx, "AsyncClient", _Client)
    monkeypatch.setenv("REVIEW_CONTROL_SECRET", "s3cret-value-never-echoed")
    return monkeypatch


def _collect(pool=None, base="http://127.0.0.1:4000/v1"):
    return asyncio.run(health.collect(pool or FakePool(), base, {"proj": {}}))


def test_a_healthy_process_reports_every_check_true(all_good):
    payload = _collect()
    assert payload["ok"] is True
    assert set(payload["checks"]) == {"postgres", "router", "sandbox_image", "review_secret"}
    assert all(c["ok"] for c in payload["checks"].values())
    assert payload["project_count"] == 1


def test_the_public_payload_never_names_a_project(all_good):
    """This route has no session behind it. A project name is the only field
    that would describe the operator's repos rather than this process, so the
    payload carries how many are onboarded and nothing more."""
    payload = asyncio.run(health.collect(
        FakePool(), "http://127.0.0.1:4000/v1",
        {"clients-private-repo": {}, "internal-billing": {}},
    ))
    body = json.dumps(payload)
    assert "clients-private-repo" not in body and "internal-billing" not in body
    assert payload["project_count"] == 2


def test_the_secret_is_reported_as_configured_and_never_echoed(all_good):
    payload = _collect()
    assert payload["checks"]["review_secret"]["ok"] is True
    assert "s3cret-value-never-echoed" not in str(payload)


def test_an_unset_review_secret_is_a_failure_with_the_reason(all_good):
    all_good.delenv("REVIEW_CONTROL_SECRET", raising=False)
    payload = _collect()
    assert payload["ok"] is False
    assert "REVIEW_CONTROL_SECRET" in payload["checks"]["review_secret"]["detail"]


def test_postgres_down_is_caught_rather_than_raising(all_good):
    payload = _collect(pool=FakePool(fail=True))
    assert payload["ok"] is False
    assert payload["checks"]["postgres"]["ok"] is False
    assert "pool exhausted" in payload["checks"]["postgres"]["detail"]


def test_a_missing_pool_during_startup_is_a_failure_not_a_crash(all_good):
    payload = asyncio.run(health.collect(None, "http://127.0.0.1:4000/v1", {}))
    assert payload["checks"]["postgres"]["ok"] is False
    assert "no pool" in payload["checks"]["postgres"]["detail"]


def test_a_missing_sandbox_image_names_the_build_script(all_good):
    all_good.setattr(health, "_image_cache", None)
    all_good.setattr(health, "_image_present", lambda: False)
    payload = _collect()
    assert payload["ok"] is False
    assert "build.sh" in payload["checks"]["sandbox_image"]["detail"]


def test_the_router_probe_uses_the_server_root_not_the_api_path():
    """MODEL_ROUTER_URL ends in /v1; the router serves liveness at the root. The
    first live run of this check returned 404 for exactly this reason."""
    assert health.router_liveness_url("http://127.0.0.1:4000/v1") == "http://127.0.0.1:4000/health/liveliness"
    assert health.router_liveness_url("http://127.0.0.1:4000/") == "http://127.0.0.1:4000/health/liveliness"
    assert health.router_liveness_url("https://router.example.com/v1/") == "https://router.example.com/health/liveliness"


def test_a_router_that_answers_wrong_is_a_failure(all_good, monkeypatch):
    class _Resp:
        status_code = 503

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(health.httpx, "AsyncClient", _Client)
    payload = _collect()
    assert payload["ok"] is False
    assert "503" in payload["checks"]["router"]["detail"]


def test_the_endpoint_needs_no_session_and_503s_when_unhealthy(all_good, monkeypatch):
    """A monitoring box has no cookie. A probe that reads only the status code
    must still be correct."""
    monkeypatch.setattr(srv.app.state, "auth_pool", FakePool(), raising=False)
    client = TestClient(srv.app)

    res = client.get("/api/health")          # no login, no dependency override
    assert res.status_code == 200 and res.json()["ok"] is True

    monkeypatch.setattr(srv.app.state, "auth_pool", FakePool(fail=True), raising=False)
    res = client.get("/api/health")
    assert res.status_code == 503 and res.json()["ok"] is False
    assert res.json()["checks"]["postgres"]["ok"] is False


def test_the_image_lookup_is_cached_so_polling_does_not_fork_per_request(all_good):
    calls = []

    def _present():
        calls.append(1)
        return True

    all_good.setattr(health, "_image_cache", None)
    all_good.setattr(health, "_image_present", _present)
    for _ in range(5):
        _collect()
    assert len(calls) == 1, "docker was shelled out to on every poll"


def test_checks_run_concurrently_rather_than_one_after_another(all_good, monkeypatch):
    """Four sequential 5s timeouts would make an unhealthy box take 20s to say
    so, which is how a health check gets dropped from a dashboard."""
    async def slow_pg(pool):
        await asyncio.sleep(0.05)
        return {"ok": True}

    async def slow_router(base):
        await asyncio.sleep(0.05)
        return {"ok": True}

    async def slow_image():
        await asyncio.sleep(0.05)
        return {"ok": True}

    monkeypatch.setattr(health, "_check_postgres", slow_pg)
    monkeypatch.setattr(health, "_check_router", slow_router)
    monkeypatch.setattr(health, "_check_sandbox_image", slow_image)

    loop = asyncio.new_event_loop()
    try:
        started = loop.time()
        loop.run_until_complete(health.collect(FakePool(), "http://x/v1", {}))
        elapsed = loop.time() - started
    finally:
        loop.close()
    assert elapsed < 0.12, f"checks ran sequentially ({elapsed:.3f}s)"


def test_the_payload_shape_is_stable_for_a_monitoring_box(all_good):
    payload = _collect()
    assert payload["service"] == "tektonix"
    assert isinstance(payload["ok"], bool)
    for name, check in payload["checks"].items():
        assert isinstance(check["ok"], bool), name
    assert SimpleNamespace(**payload).project_count == 1
