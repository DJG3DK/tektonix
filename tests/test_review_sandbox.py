"""The bundle's review checks run in a sandbox the AGENT starts, and the
agent decides what that sandbox can see.

Before agent/review_sandbox.py the bundle's reviewer ran checks in its own
process -- the container holding the review-control secret -- so a test file
could read the secret and force-merge its own branch. The reviewer now sends
a structured request here. These tests pin that the request cannot widen the
container: every path is re-checked against server-owned config, the
hardening is fixed, and the route is closed without the secret.
"""

from __future__ import annotations

import asyncio
import os
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent import review_sandbox as rs
from agent.config import PROJECTS
from agent.routers import review_sandbox as route


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"})


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A live checkout, a real review worktree of it, and the env the bundle sets."""
    live = tmp_path / "projects" / "shop"
    live.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=live)
    (live / "README.md").write_text("hi\n")
    _git("add", ".", cwd=live)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=live)
    (live / "node_modules" / ".bin").mkdir(parents=True)
    (live / "data").mkdir()
    (live / ".env").write_text("SECRET=live\n")

    root = tmp_path / "projects" / ".tektonix-review-worktrees"
    root.mkdir()
    wt = root / "shop-0123456789ab"
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=live)
    (wt / "node_modules").symlink_to(live / "node_modules")

    other = tmp_path / "projects" / "other"
    other.mkdir()
    _git("init", "-q", "-b", "main", cwd=other)

    monkeypatch.setitem(PROJECTS, "shop", {"live": str(live), "sandbox": str(live)})
    monkeypatch.setitem(PROJECTS, "other", {"live": str(other), "sandbox": str(other)})
    monkeypatch.setenv(rs.WORKTREE_ROOT_ENV, str(root))
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", f"{tmp_path / 'projects'}=/srv/code")
    monkeypatch.setenv("REVIEW_CONTROL_SECRET", "the-secret")
    return {"live": os.path.realpath(live), "root": os.path.realpath(root), "wt": str(wt),
            "tmp": tmp_path}


def _req(b, **kw):
    base = {"project": "shop", "worktree": b["wt"], "cmd": "npm", "args": ["test"]}
    base.update(kw)
    return rs.CheckRequest(**base)


def _argv(b, **kw):
    return rs.build_docker_argv(_req(b, **kw), "rvw-test")[0]


def test_a_valid_request_gets_the_hardened_container(bundle):
    argv = _argv(bundle, rel_dir="frontend", env={"FOO": "bar"})
    joined = " ".join(argv)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--pids-limit 512",
                 "--memory 2g", "--memory-swap 2g", "--network none", "--entrypoint npm",
                 "-w /workspace/frontend", "-e FOO=bar", "-e CI=true"):
        assert flag in joined, flag
    # The worktree reaches the host daemon by its HOST path.
    assert "/srv/code/.tektonix-review-worktrees/shop-0123456789ab:/workspace" in argv
    assert argv[-2:] == ["tektonix-sandbox:latest", "test"]
    assert "--privileged" not in argv


def test_mounts_are_read_only_and_only_where_the_reviewer_puts_them(bundle):
    live = bundle["live"]
    argv = _argv(bundle, mounts=[
        (os.path.join(live, "data"), "/workspace/data"),
        (os.path.join(live, "node_modules"), os.path.join(live, "node_modules")),
        (os.path.join(live, ".git"), os.path.join(live, ".git")),
    ])
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"][1:]
    assert vols == [
        "/srv/code/shop/data:/workspace/data:ro",
        f"/srv/code/shop/node_modules:{live}/node_modules:ro",
        f"/srv/code/shop/.git:{live}/.git:ro",
    ]


@pytest.mark.parametrize("mount", [
    ("/etc", "/workspace/etc"),                               # not inside live
    ("{live}", "/workspace/live"),                            # live itself: its .env and everything
    ("{live}/data", "/workspace/other"),                      # target does not match source
    ("{live}/data", "{live}/data"),                           # same-path, but not .git or node_modules
    ("{live}/data", "/etc"),                                  # lands on a system path
    ("{live}/node_modules", "/workspace/a:/b"),               # smuggles a second -v field
    ("{tmp}/projects/other/.git", "{tmp}/projects/other/.git"),  # another project's repo
])
def test_a_mount_outside_the_reviewers_layout_is_refused(bundle, mount):
    src, dst = (m.format(live=bundle["live"], tmp=bundle["tmp"]) for m in mount)
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, mounts=[(src, dst)])


def test_a_symlink_in_live_cannot_turn_a_mount_into_something_else(bundle):
    secret_dir = bundle["tmp"] / "secrets"
    secret_dir.mkdir()
    os.symlink(secret_dir, os.path.join(bundle["live"], "linked"))
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, mounts=[(os.path.join(bundle["live"], "linked"), "/workspace/linked")])


def test_the_worktree_must_be_this_projects_review_worktree(bundle):
    tmp = bundle["tmp"]
    # Anywhere else on disk.
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, worktree=bundle["live"])
    # Under the root, but a symlink out of it.
    os.symlink(bundle["live"], os.path.join(bundle["root"], "shop-feedfacecafe"))
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, worktree=os.path.join(bundle["root"], "shop-feedfacecafe"))
    # Named for another project.
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, project="other")
    # Named right, but its pointer names another project's repository -- the
    # pointer is a file the worktree's contents could rewrite.
    fake = os.path.join(bundle["root"], "shop-aaaaaaaaaaaa")
    os.mkdir(fake)
    with open(os.path.join(fake, ".git"), "w") as fh:
        fh.write(f"gitdir: {tmp}/projects/other/.git/worktrees/x\n")
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, worktree=fake)
    # An unknown project.
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, project="nope")


@pytest.mark.parametrize("kw", [
    {"network": "host"},
    {"network": "container:agent"},
    {"cmd": "--privileged"},
    {"cmd": ""},
    {"rel_dir": "../.."},
    {"rel_dir": "/etc"},
    {"env": {"BAD KEY": "x"}},
    {"env": {"X": "a\x00b"}},
    {"args": ["ok", "no\x00pe"]},
])
def test_malformed_requests_are_refused(bundle, kw):
    with pytest.raises(rs.RejectedRequest):
        _argv(bundle, **kw)


def test_the_image_comes_from_server_config_not_the_request(bundle, monkeypatch):
    monkeypatch.setitem(PROJECTS["shop"], "sandbox_image", "shop-image:1")
    assert _argv(bundle)[-2] == "shop-image:1"
    monkeypatch.setattr(rs.sb, "_stack_images", lambda: {
        "stacks": {"go": {"image": "golang:1.22-alpine", "env": {"GOCACHE": "/tmp/go"}}}})
    argv = _argv(bundle, stack="go")
    assert argv[-2] == "golang:1.22-alpine"
    assert "GOCACHE=/tmp/go" in argv
    # A stack nobody declared falls back to the default image, never to a
    # name the request supplied.
    assert _argv(bundle, stack="evil/image:latest")[-2] == rs.sb.SANDBOX_IMAGE


def test_the_timeout_is_clamped():
    assert rs.clamp_timeout_ms(10) == 1_000
    assert rs.clamp_timeout_ms(10**12) == rs.MAX_TIMEOUT_MS
    assert rs.clamp_timeout_ms("junk") == 300_000


def _client():
    app = FastAPI()
    app.include_router(route.router)
    return TestClient(app)


def _body(b, **kw):
    return {"project": "shop", "worktree": b["wt"], "cmd": "npm", "args": ["test"], **kw}


def test_the_route_is_closed_without_the_secret(bundle, monkeypatch):
    c = _client()
    url = "/api/internal/review-sandbox/run"
    assert c.post(url, json=_body(bundle)).status_code == 401
    assert c.post(url, json=_body(bundle), headers={"X-Review-Secret": "wrong"}).status_code == 401
    monkeypatch.delenv(rs.WORKTREE_ROOT_ENV)
    r = c.post(url, json=_body(bundle), headers={"X-Review-Secret": "the-secret"})
    assert r.status_code == 503, "a host install must not expose this at all"
    monkeypatch.setenv(rs.WORKTREE_ROOT_ENV, bundle["root"])
    monkeypatch.delenv("REVIEW_CONTROL_SECRET")
    r = c.post(url, json=_body(bundle), headers={"X-Review-Secret": ""})
    assert r.status_code == 503


def test_the_route_runs_docker_with_the_built_argv_and_refuses_bad_requests(bundle, monkeypatch):
    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"all green\n", None

        async def wait(self):
            return 0

    async def fake_exec(*argv, **_kw):
        seen["argv"] = argv
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    c = _client()
    url = "/api/internal/review-sandbox/run"
    h = {"X-Review-Secret": "the-secret"}
    r = c.post(url, json=_body(bundle, network="bridge"), headers=h)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "code": 0, "output": "all green\n", "image": "tektonix-sandbox:latest"}
    assert seen["argv"][:2] == ("docker", "run")
    assert "--cap-drop" in seen["argv"] and "bridge" in seen["argv"]

    r = c.post(url, json=_body(bundle, mounts=[{"src": "/etc", "dst": "/workspace/etc"}]), headers=h)
    assert r.status_code == 400
    assert "refused" in r.json()["detail"]


def test_a_timed_out_check_kills_its_container(bundle, monkeypatch):
    killed = []

    class _Slow:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)

        async def wait(self):
            return -9

    async def fake_exec(*argv, **_kw):
        return _Slow()

    async def fake_kill(name):
        killed.append(name)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(rs.sb, "_kill_container", fake_kill)
    monkeypatch.setattr(rs, "clamp_timeout_ms", lambda _v: 50)

    async def present(image):
        return True

    monkeypatch.setattr(rs, "image_present", present)   # this test is about the run, not the probe
    out = asyncio.run(rs.run_check(_req(bundle)))
    assert out["ok"] is False and out["code"] == 124
    assert killed and killed[0].startswith("rvw-")


# --- the probe, and a run without the image (2026-09-27) ---------------------

def _fake_docker(answers):
    """`answers`: {"version": (ok, out), "image": (ok, out)} for the two probes."""
    async def fake(*args, timeout_s=30):
        key = "version" if args[0] == "version" else "image"
        return answers[key]
    return fake


def test_the_probe_reports_docker_and_the_image_and_builds_a_missing_one(bundle, monkeypatch):
    c = _client()
    h = {"X-Review-Secret": "the-secret"}
    url = "/api/internal/review-sandbox/probe"
    assert c.get(url).status_code == 401
    monkeypatch.setattr(rs, "_docker", _fake_docker({"version": (True, "27.1"), "image": (True, "sha256:abc")}))
    r = c.get(url, headers=h)
    assert r.status_code == 200 and r.json()["ok"] is True and r.json()["mode"] == "delegated"
    assert r.json()["image"] == "tektonix-sandbox:latest" and r.json()["docker"] == "27.1"

    builds = []
    monkeypatch.setattr(rs, "start_build", lambda image: builds.append(image) or True)
    monkeypatch.setattr(rs, "_docker", _fake_docker({"version": (True, "27.1"), "image": (False, "No such image")}))
    r = c.get(url, headers=h).json()
    assert r["ok"] is False and r["mode"] == "unavailable" and "building it now" in r["reason"]
    assert builds == ["tektonix-sandbox:latest"]

    monkeypatch.setattr(rs, "_docker", _fake_docker({"version": (False, "Cannot connect to the Docker daemon"), "image": (True, "")}))
    r = c.get(url, headers=h).json()
    assert r["ok"] is False and "docker is not usable" in r["reason"]


def test_a_run_without_the_image_is_a_setup_refusal_not_a_check_result(bundle, monkeypatch):
    async def absent(image):
        return False

    spawned = []

    async def fake_exec(*argv, **_kw):
        spawned.append(argv)
        raise AssertionError("docker run must not be attempted without the image")

    monkeypatch.setattr(rs, "image_present", absent)
    monkeypatch.setattr(rs, "start_build", lambda image: True)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    c = _client()
    r = c.post("/api/internal/review-sandbox/run", json=_body(bundle), headers={"X-Review-Secret": "the-secret"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["infrastructure"] is True
    assert body["output"].startswith("SETUP:") and "not built" in body["output"] and "building it now" in body["output"]
    assert spawned == []


def test_a_build_starts_once_and_only_for_the_default_image_with_a_context(bundle, monkeypatch, tmp_path):
    ctx = tmp_path / "ctx"
    ctx.mkdir()
    (ctx / "Dockerfile").write_text("FROM scratch\n")
    monkeypatch.setattr(rs, "SANDBOX_CONTEXT", ctx)
    started = []

    async def fake_build(image):
        started.append(image)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(rs, "_build", fake_build)
    rs._builds.clear()

    async def go():
        assert rs.start_build("tektonix-sandbox:latest") is True
        assert rs.start_build("tektonix-sandbox:latest") is True, "a second ask joins the build in progress"
        assert rs.start_build("some-other:image") is False, "only the default image has a context to build from"
        await asyncio.sleep(0.1)
        assert started == ["tektonix-sandbox:latest"]

    asyncio.run(go())
    rs._builds.clear()


# --- the database checks, in the sandbox (2026-09-27) ---------------------------

def _db_env(monkeypatch, tmp_path):
    monkeypatch.setenv(rs.CHECKS_NETWORK_ENV, "tektonix_checks")
    monkeypatch.setenv(rs.CHECKS_POSTGRES_ENV, "postgresql://checks@checks-postgres:5432/postgres")
    monkeypatch.setenv(rs.CHECKS_REDIS_ENV, "redis://checks-redis:6379")
    monkeypatch.setenv(rs.CHECKS_POSTGRES_CONTAINER_ENV, "tektonix-checks-postgres")
    monkeypatch.setenv(rs.CHECKS_REDIS_CONTAINER_ENV, "tektonix-checks-redis")


def _db_project(bundle, monkeypatch):
    PROJECTS["shop"]["databaseCheck"] = {
        "apiDir": "apps/api",
        "driftCmd": {"cmd": "pnpm", "args": ["db:drift"]},
        "seedCmd": {"cmd": "pnpm", "args": ["db:seed"]},
        "e2eCmd": {"cmd": "pnpm", "args": ["test:e2e"]},
    }


def _fake_db_layer(monkeypatch, outcomes):
    """Record the SQL, the redis flush and every sandboxed step; `outcomes`
    maps a step name to ok/not."""
    sql, runs, flushed = [], [], []

    async def admin(statement):
        sql.append(statement)

    async def flush(db):
        flushed.append(db)

    async def run(req):
        runs.append(req)
        name = {"db:drift": "db-drift", "db:seed": "db-seed", "test:e2e": "e2e"}[req.args[0]]
        ok = outcomes.get(name, True)
        return {"ok": ok, "code": 0 if ok else 1, "output": f"{name} {'passed' if ok else 'FAILED'}",
                "image": "tektonix-sandbox:latest"}

    monkeypatch.setattr(rs, "_admin_sql", admin)
    monkeypatch.setattr(rs, "_flush_redis", flush)
    monkeypatch.setattr(rs, "run_check", run)
    return sql, runs, flushed


def test_the_three_steps_run_in_order_in_the_checks_container_against_a_throwaway_database(bundle, monkeypatch):
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)
    sql, runs, flushed = _fake_db_layer(monkeypatch, {})
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=bundle["wt"],
                                                                 mounts=[(f"{bundle['live']}/node_modules", "/workspace/node_modules")])))
    assert [r["name"] for r in rows] == ["db-drift", "db-seed", "e2e"] and all(r["ok"] for r in rows)
    assert [r.args for r in runs] == [["db:drift"], ["db:seed"], ["test:e2e"]]
    assert [r.timeout_ms for r in runs] == [120_000, 60_000, 300_000]
    assert all(r.network == rs.CHECKS_NETWORK and r.rel_dir == "apps/api" and r.mounts for r in runs)
    dsn = runs[0].env["DATABASE_URL"]
    assert dsn.startswith("postgresql://tektonix_ci_review_") and dsn.endswith("?schema=public")
    throwaway = dsn.split("/")[-1].split("?")[0]
    role, password = dsn.split("//")[1].split("@")[0].split(":")
    assert role == throwaway and len(password) == 32, "the run's own plain role, not the server's superuser"
    assert "checks" not in dsn.split("@")[0] and "@checks-postgres:5432/" in dsn
    redis_db = int(runs[0].env["REDIS_URL"].rsplit("/", 1)[1])
    assert runs[0].env["REDIS_URL"] == f"redis://checks-redis:6379/{redis_db}" and flushed == [redis_db]
    assert len(runs[0].env["JWT_ACCESS_SECRET"]) == 64 and runs[0].env["JWT_ACCESS_SECRET"] != runs[0].env["SECRETS_ENCRYPTION_KEY"]
    assert "PATH" not in runs[0].env and "REVIEW_CONTROL_SECRET" not in runs[0].env
    assert sql[0] == (f"CREATE ROLE {throwaway} LOGIN PASSWORD '{password}' "
                      f"NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;")
    assert sql[1] == f"CREATE DATABASE {throwaway} OWNER {throwaway} TEMPLATE template0;"
    assert sql[2] == f"REVOKE CONNECT ON DATABASE {throwaway} FROM PUBLIC;"
    assert sql[3] == f"DROP DATABASE IF EXISTS {throwaway} WITH (FORCE);" and sql[4] == f"DROP ROLE IF EXISTS {throwaway};"


def test_a_failing_step_stops_the_rest_and_the_database_is_still_dropped(bundle, monkeypatch):
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)
    sql, runs, _ = _fake_db_layer(monkeypatch, {"db-seed": False})
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=bundle["wt"])))
    assert [(r["name"], r["ok"]) for r in rows] == [("db-drift", True), ("db-seed", False)]
    assert [r.args for r in runs] == [["db:drift"], ["db:seed"]], "e2e needs a seeded schema; it was not run"
    assert sql[-2].startswith("DROP DATABASE IF EXISTS tektonix_ci_review_") and sql[-1].startswith("DROP ROLE IF EXISTS")


def test_the_checks_network_is_server_config_and_a_request_cannot_choose_it(bundle, monkeypatch):
    _db_env(monkeypatch, bundle["tmp"])
    argv = _argv(bundle, network=rs.CHECKS_NETWORK)
    assert argv[argv.index("--network") + 1] == "tektonix_checks"
    c = _client()
    r = c.post("/api/internal/review-sandbox/run", json=_body(bundle, network="checks"),
               headers={"X-Review-Secret": "the-secret"})
    assert r.status_code == 400 and "network" in r.json()["detail"]


def test_without_the_checks_services_the_route_is_503_and_the_runner_refuses(bundle, monkeypatch):
    for k in rs._CHECKS_ENVS:
        monkeypatch.delenv(k, raising=False)
    _db_project(bundle, monkeypatch)
    c = _client()
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": bundle["wt"]},
               headers={"X-Review-Secret": "the-secret"})
    assert r.status_code == 503 and rs.CHECKS_NETWORK_ENV in r.json()["detail"]
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=bundle["wt"])))
    assert rows[0]["name"] == "db-setup" and rows[0]["infrastructure"] is True and rows[0]["output"].startswith("SETUP:")


def test_the_route_returns_the_rows_and_a_project_without_database_checks_returns_none(bundle, monkeypatch):
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)
    _fake_db_layer(monkeypatch, {})
    c = _client()
    h = {"X-Review-Secret": "the-secret"}
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": bundle["wt"]}, headers=h)
    assert r.status_code == 200 and [x["name"] for x in r.json()["results"]] == ["db-drift", "db-seed", "e2e"]
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": "/etc"}, headers=h)
    assert r.status_code == 400
    del PROJECTS["shop"]["databaseCheck"]
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": bundle["wt"]}, headers=h)
    assert r.status_code == 200 and r.json() == {"results": []}


def test_a_database_that_cannot_be_created_is_a_setup_refusal(bundle, monkeypatch):
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)

    async def broken(statement):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(rs, "_admin_sql", broken)
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=bundle["wt"])))
    assert rows[0]["name"] == "db-setup" and rows[0]["infrastructure"] is True and "connection refused" in rows[0]["output"]


def test_the_setup_is_docker_exec_into_the_two_containers_never_a_connection_from_the_agent(bundle, monkeypatch):
    """The agent used to sit on the checks network and connect to the two
    services itself, which put its own API one hop from every check. Now it
    is not on that network at all: psql and redis-cli run inside the service
    containers, with the SQL on stdin."""
    _db_env(monkeypatch, bundle["tmp"])
    calls = []

    async def docker(*args, timeout_s=30, stdin=None):
        calls.append((args, stdin))
        return True, "OK"

    monkeypatch.setattr(rs, "_docker", docker)
    asyncio.run(rs._admin_sql("CREATE ROLE r;"))
    asyncio.run(rs._flush_redis(7))
    assert calls[0][0] == ("exec", "-i", "tektonix-checks-postgres", "psql", "-v", "ON_ERROR_STOP=1", "-q",
                           "-U", "checks", "-d", "postgres") and calls[0][1] == b"CREATE ROLE r;"
    assert calls[1] == (("exec", "tektonix-checks-redis", "redis-cli", "-n", "7", "FLUSHDB"), None)

    async def refused(*args, timeout_s=30, stdin=None):
        return False, "ERROR:  role already exists"

    monkeypatch.setattr(rs, "_docker", refused)
    with pytest.raises(RuntimeError, match="already exists"):
        asyncio.run(rs._admin_sql("CREATE ROLE r;"))


def test_concurrent_runs_get_their_own_redis_database_and_give_it_back(bundle, monkeypatch):
    """Two reviews at once shared database 15: one's flush emptied the
    other's keys mid-run."""
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)
    sql, runs, flushed = _fake_db_layer(monkeypatch, {})
    gate = {}

    async def slow_run(req):
        runs.append(req)
        gate.setdefault("started", asyncio.Event()).set()
        await gate.setdefault("release", asyncio.Event()).wait()
        return {"ok": True, "code": 0, "output": "fine", "image": "tektonix-sandbox:latest"}

    monkeypatch.setattr(rs, "run_check", slow_run)

    async def go():
        req = rs.DbCheckRequest(project="shop", worktree=bundle["wt"])
        a = asyncio.create_task(rs.run_database_check(req))
        b = asyncio.create_task(rs.run_database_check(req))
        await asyncio.sleep(0.05)
        gate["release"].set()
        return await asyncio.gather(a, b)

    before = set(rs._redis_pool["free"])
    rows_a, rows_b = asyncio.run(go())
    assert all(r["ok"] for r in rows_a + rows_b)
    dbs = {int(r.env["REDIS_URL"].rsplit("/", 1)[1]) for r in runs}
    assert len(dbs) == 2 and sorted(flushed) == sorted(dbs), "each run flushed and used its own database"
    assert set(rs._redis_pool["free"]) == before, "both databases are back in the pool"
    assert len({r.env["DATABASE_URL"] for r in runs}) == 2, "and each has its own role and database"


def test_a_long_check_keeps_the_connection_alive_with_whitespace_then_the_json(bundle, monkeypatch):
    """2026-09-29: the reviewer's fetch gives up on headers after five
    minutes; a project's test suite ran longer, and every review of it
    said the agent's sandbox endpoint did not answer."""
    from agent.routers import review_sandbox as route

    class _Proc:
        returncode = 0

        async def communicate(self):
            await asyncio.sleep(0.25)
            return b"slow but green\n", None

        async def wait(self):
            return 0

    async def fake_exec(*_argv, **_kw):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(route, "KEEPALIVE_S", 0.05)
    c = _client()
    r = c.post("/api/internal/review-sandbox/run", json=_body(bundle), headers={"X-Review-Secret": "the-secret"})
    assert r.status_code == 200
    assert r.text.startswith("  "), "keepalive spaces came before the answer"
    assert r.json() == {"ok": True, "code": 0, "output": "slow but green\n", "image": "tektonix-sandbox:latest"}
    assert r.headers["content-type"].startswith("application/json")


def test_a_node_modules_mount_from_the_agent_s_workspace_template_is_refused(tmp_path):
    """For a day (2026-09-29) the reviewer borrowed the template's Linux
    install when live had none a Linux check could run. Every task
    workspace hardlinks that template, so a task could rewrite the linter
    the review then ran; the reviewer installs its own instead, and the
    agent refuses the template whatever the project's config says."""
    from agent.review_sandbox import RejectedRequest, _checked_mounts
    live = tmp_path / "live"
    template = tmp_path / "ws" / "proj"
    (live / ".git").mkdir(parents=True)
    (template / "node_modules").mkdir(parents=True)
    nm = str(template / "node_modules")
    with pytest.raises(RejectedRequest, match="live checkout"):
        _checked_mounts([(nm, nm)], str(live))
    live_nm = live / "node_modules"
    live_nm.mkdir()
    assert _checked_mounts([(str(live_nm), str(live_nm))], str(live)) == [(os.path.realpath(str(live_nm)), str(live_nm))]


def test_a_same_path_mount_of_live_s_generated_code_is_accepted_only_from_the_project_s_rule(tmp_path):
    from agent.review_sandbox import RejectedRequest, _checked_mounts
    live = tmp_path / "live"
    (live / ".git").mkdir(parents=True)
    gen = live / "apps" / "api" / "generated"
    gen.mkdir(parents=True)
    (live / "apps" / "api" / "src").mkdir()
    g = str(gen)
    assert _checked_mounts([(g, g)], str(live), ["apps/api/generated"]) == [(os.path.realpath(g), g)]
    with pytest.raises(RejectedRequest):
        _checked_mounts([(g, g)], str(live), [])
    with pytest.raises(RejectedRequest):
        _checked_mounts([(str(live / "apps" / "api" / "src"),) * 2], str(live), ["apps/api/generated"])


def test_a_long_database_check_keeps_the_connection_alive_too(bundle, monkeypatch):
    """2026-09-29 audit, R5: /db-check answered in one piece, so a slow
    drift-and-seed run hit the reviewer's five-minute cut-off like /run did."""
    from agent.routers import review_sandbox as route

    async def slow(_req):
        await asyncio.sleep(0.25)
        return [{"name": "db-drift", "ok": True, "output": "clean"}]

    monkeypatch.setattr(route.rs, "run_database_check", slow)
    monkeypatch.setattr(route.rs, "db_checks_enabled", lambda: True)
    monkeypatch.setattr(route, "KEEPALIVE_S", 0.05)
    c, h = _client(), {"X-Review-Secret": "the-secret"}
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": bundle["wt"]}, headers=h)
    assert r.status_code == 200 and r.text.startswith("  ")
    assert r.json() == {"results": [{"name": "db-drift", "ok": True, "output": "clean"}]}
    # A bad worktree is still refused before the first byte.
    r = c.post("/api/internal/review-sandbox/db-check", json={"project": "shop", "worktree": "/etc"}, headers=h)
    assert r.status_code == 400 and "refused" in r.json()["detail"]


# --- cross-run isolation on the checks services (2026-09-29) --------------------

def test_a_throwaway_database_is_connectable_by_its_owner_alone(bundle, monkeypatch):
    """Postgres grants CONNECT on a new database to PUBLIC, so one run's role
    could open another run's database: nothing to read there, but a
    foothold. Revoked right after the create, before any check runs."""
    _db_env(monkeypatch, bundle["tmp"])
    _db_project(bundle, monkeypatch)
    sql, runs, _ = _fake_db_layer(monkeypatch, {})
    asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=bundle["wt"])))
    create = next(i for i, st in enumerate(sql) if st.startswith("CREATE DATABASE "))
    name = sql[create].split()[2]
    assert sql[create + 1] == f"REVOKE CONNECT ON DATABASE {name} FROM PUBLIC;"
    assert all(not st.startswith("REVOKE") for st in sql[create + 2:]), "revoked once, before the checks"
    assert runs, "the checks still ran after the revoke"
    assert not rs._in_flight, "the run's name is forgotten once it is dropped"


def test_the_startup_sweep_drops_what_a_crashed_agent_left_and_spares_a_run_in_flight(bundle, monkeypatch):
    """Each run drops its own throwaway in a finally; an agent that died
    mid-run never reached it, and the leftovers stayed until checks-postgres
    restarted."""
    _db_env(monkeypatch, bundle["tmp"])
    listed = {
        "pg_database": "tektonix_ci_review_deadbeef\ntektonix_ci_review_0badf00d\ntektonix_ci_review_ab12cd34\n",
        "pg_roles": "tektonix_ci_review_deadbeef\ntektonix_ci_review_0badf00d\ntektonix_ci_review_notours; DROP\n",
    }
    statements = []

    async def docker(*args, timeout_s=30, stdin=None):
        text = (stdin or b"").decode()
        statements.append(text)
        for key, rows in listed.items():
            if key in text:
                return True, rows
        return True, ""

    monkeypatch.setattr(rs, "_docker", docker)
    monkeypatch.setattr(rs, "_in_flight", {"tektonix_ci_review_ab12cd34"})
    swept = asyncio.run(rs.sweep_orphans())
    assert swept == ["tektonix_ci_review_deadbeef", "tektonix_ci_review_0badf00d"]
    dropped = [st for st in statements if st.startswith("DROP")]
    assert "DROP DATABASE IF EXISTS tektonix_ci_review_deadbeef WITH (FORCE);" in dropped
    assert "DROP ROLE IF EXISTS tektonix_ci_review_0badf00d;" in dropped
    assert not any("ab12cd34" in st for st in dropped), "a run in flight keeps its database"
    assert not any("notours" in st for st in dropped), "only names this module made are dropped"


def test_the_sweep_is_a_no_op_without_the_checks_services_and_never_raises(bundle, monkeypatch):
    for k in rs._CHECKS_ENVS:
        monkeypatch.delenv(k, raising=False)
    assert asyncio.run(rs.sweep_orphans()) == []

    _db_env(monkeypatch, bundle["tmp"])

    async def down(*args, timeout_s=30, stdin=None):
        return False, "Error response from daemon: No such container"

    monkeypatch.setattr(rs, "_docker", down)
    assert asyncio.run(rs.sweep_orphans()) == []


def test_the_checks_redis_denies_the_commands_that_reach_past_a_runs_own_index():
    """Redis cannot bind a user to a database index, so a run's index is a
    convention; FLUSHALL, CONFIG, DEBUG, SHUTDOWN and ACL are what a check
    could use to reach every other run's, and the one user is denied them.
    FLUSHDB stays: the agent clears one index per run with it."""
    import yaml
    from agent import paths
    compose = yaml.safe_load((paths.REPO_ROOT / "docker-compose.yml").read_text())
    cmd = compose["services"]["checks-redis"]["command"]
    assert cmd[0] == "redis-server"
    user = cmd.index("--user")
    rule = cmd[user + 1:]
    assert rule[:3] == ["default", "on", "nopass"]
    for denied in ("-flushall", "-config", "-debug", "-shutdown", "-acl"):
        assert denied in rule, denied
    assert "-flushdb" not in rule and "+@all" in rule
