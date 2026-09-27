"""End to end: a project's db:drift, db:seed and test:e2e run in the sandbox
against a throwaway Postgres and Redis on an internal network, the way the
compose bundle runs them (agent/review_sandbox.py, checks.js's bundle branch):
the agent off that network, setting the run up over `docker exec`, each run
as its own plain role, the bootstrap superuser password rotated away.

Real containers, the real sandbox image, real `pg` and `redis` clients in
the fixture project, and the reviewer's own module calling the agent's
route over HTTP. Skipped where docker or the sandbox image is missing (CI),
run on the box that has them.
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from agent import paths
from agent import review_sandbox as rs
from agent.config import PROJECTS

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "image", "inspect", "tektonix-sandbox:latest"], capture_output=True).returncode != 0
    or shutil.which("npm") is None,
    reason="needs docker, the sandbox image and npm",
)

SCRIPTS = {
    "drift.js": """
const { Client } = require('pg');
(async () => {
  const c = new Client({ connectionString: process.env.DATABASE_URL.replace(/\\?schema=public$/, '') });
  await c.connect();
  await c.query('CREATE TABLE items (id serial primary key, name text not null)');
  const r = await c.query('SELECT current_database() AS db');
  console.log('migrated into ' + r.rows[0].db + '; no drift');
  await c.end();
})().catch((e) => { console.error('drift failed:', e.message); process.exit(1); });
""",
    "seed.js": """
const { Client } = require('pg');
(async () => {
  const c = new Client({ connectionString: process.env.DATABASE_URL.replace(/\\?schema=public$/, '') });
  await c.connect();
  await c.query("INSERT INTO items (name) VALUES ('a'), ('b')");
  console.log('seeded 2');
  await c.end();
})().catch((e) => { console.error('seed failed:', e.message); process.exit(1); });
""",
    "e2e.js": """
const { Client } = require('pg');
const { createClient } = require('redis');
const net = require('net');
const reach = (host, port) => new Promise((resolve) => {
  const s = net.connect({ host, port, timeout: 3000 });
  s.on('connect', () => { s.destroy(); resolve('REACHED'); });
  s.on('error', (e) => resolve('unreachable ' + e.code));
  s.on('timeout', () => { s.destroy(); resolve('unreachable timeout'); });
});
(async () => {
  const c = new Client({ connectionString: process.env.DATABASE_URL.replace(/\\?schema=public$/, '') });
  await c.connect();
  const r = await c.query('SELECT count(*)::int AS n FROM items');
  if (r.rows[0].n !== 2) throw new Error('expected 2 rows, got ' + r.rows[0].n);
  await c.end();
  const redis = createClient({ url: process.env.REDIS_URL });
  await redis.connect();
  if (await redis.get('left-over')) throw new Error('the scratch redis database was not flushed');
  await redis.set('e2e', 'ok');
  const back = await redis.get('e2e');
  await redis.quit();
  if (back !== 'ok') throw new Error('redis roundtrip failed');
  const u = new URL(process.env.DATABASE_URL);
  const me = new Client({ connectionString: process.env.DATABASE_URL.replace(/\\?schema=public$/, '') });
  await me.connect();
  const who = await me.query('SELECT current_user AS u, rolsuper AS su FROM pg_roles WHERE rolname = current_user');
  await me.end();
  const su = new Client({ host: u.hostname, port: Number(u.port || 5432), user: 'checks', password: 'checks', database: 'postgres' });
  const suLogin = await su.connect().then(() => su.end().then(() => 'ACCEPTED'), (e) => 'refused ' + e.code);
  console.log('e2e ok; internet ' + await reach('1.1.1.1', 53) + '; role ' + who.rows[0].u + (who.rows[0].su ? ' SUPERUSER' : ' plain') + '; superuser login ' + suLogin);
  if (!process.env.JWT_ACCESS_SECRET || process.env.REVIEW_CONTROL_SECRET) throw new Error('env not built for the run');
})().catch((e) => { console.error('e2e failed:', e.message); process.exit(1); });
""",
}


def _sh(*args, **kw):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kw).stdout.strip()


def _git(*args, cwd):
    _sh("git", "-c", "user.email=t@t", "-c", "user.name=t", *args, cwd=cwd)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def checks_services():
    """A postgres and a redis on an internal docker network, like the bundle's."""
    tag = secrets.token_hex(4)
    net = f"tektonix-e2e-checks-{tag}"
    pg, rd = f"{net}-pg", f"{net}-redis"
    _sh("docker", "network", "create", "--internal", net)
    try:
        init = paths.REPO_ROOT / "docker" / "checks-postgres" / "init.sh"
        _sh("docker", "run", "-d", "--rm", "--name", pg, "--network", net, "-e", "POSTGRES_USER=checks",
            "-e", "POSTGRES_PASSWORD=checks", "-v", f"{init}:/docker-entrypoint-initdb.d/init.sh:ro", "postgres:16-alpine")
        _sh("docker", "run", "-d", "--rm", "--name", rd, "--network", net, "redis:7-alpine")
        for _ in range(60):
            # The entrypoint restarts the server after the init scripts; ready
            # means the log says so, not just that a socket answers.
            up = subprocess.run(["docker", "logs", pg], capture_output=True, text=True)
            if "superuser password replaced" in up.stdout + up.stderr and subprocess.run(
                    ["docker", "exec", pg, "pg_isready", "-U", "checks"], capture_output=True).returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("checks postgres did not come up")
        ip = lambda name: _sh("docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", name)  # noqa: E731
        yield {"network": net, "pg": pg, "redis": rd, "pg_ip": ip(pg), "redis_ip": ip(rd)}
    finally:
        subprocess.run(["docker", "rm", "-f", pg, rd], capture_output=True)
        subprocess.run(["docker", "network", "rm", net], capture_output=True)


@pytest.fixture(scope="module")
def fixture_project(tmp_path_factory):
    """A live checkout with apps/api scripts that need pg and redis, its
    dependencies installed once on the host, and a review worktree of it."""
    root = tmp_path_factory.mktemp("e2e")
    live = root / "projects" / "shop"
    (live / "apps" / "api" / "scripts").mkdir(parents=True)
    (live / "apps" / "api" / "package.json").write_text(json.dumps({"name": "api", "private": True, "scripts": {
        "db:drift": "node scripts/drift.js", "db:seed": "node scripts/seed.js", "test:e2e": "node scripts/e2e.js"}}))
    for name, body in SCRIPTS.items():
        (live / "apps" / "api" / "scripts" / name).write_text(body)
    (live / "package.json").write_text(json.dumps({"name": "shop", "private": True}))
    (live / ".gitignore").write_text("node_modules\n")
    _sh("npm", "install", "--no-audit", "--no-fund", "--silent", "pg@8", "redis@4", cwd=str(live))
    _git("init", "-q", "-b", "main", cwd=live)
    _git("add", ".", cwd=live)
    _git("commit", "-qm", "the project", cwd=live)
    wt_root = root / "projects" / ".tektonix-review-worktrees"
    wt_root.mkdir()
    wt = wt_root / f"shop-{secrets.token_hex(6)}"
    _git("worktree", "add", "-q", "--detach", str(wt), "HEAD", cwd=live)
    return {"live": os.path.realpath(live), "root": os.path.realpath(wt_root), "wt": str(wt)}


@pytest.fixture
def bundle_env(monkeypatch, checks_services, fixture_project):
    monkeypatch.setitem(PROJECTS, "shop", {
        "live": fixture_project["live"], "sandbox": fixture_project["live"],
        "dependencyDirs": ["node_modules"],
        "databaseCheck": {"apiDir": "apps/api",
                          "driftCmd": {"cmd": "npm", "args": ["run", "--silent", "db:drift"]},
                          "seedCmd": {"cmd": "npm", "args": ["run", "--silent", "db:seed"]},
                          "e2eCmd": {"cmd": "npm", "args": ["run", "--silent", "test:e2e"]}},
    })
    monkeypatch.setenv(rs.WORKTREE_ROOT_ENV, fixture_project["root"])
    monkeypatch.setenv("REVIEW_CONTROL_SECRET", "e2e-secret")
    monkeypatch.setenv(rs.CHECKS_NETWORK_ENV, checks_services["network"])
    monkeypatch.setenv(rs.CHECKS_POSTGRES_ENV, f"postgresql://checks@{checks_services['pg_ip']}:5432/postgres")
    monkeypatch.setenv(rs.CHECKS_REDIS_ENV, f"redis://{checks_services['redis_ip']}:6379")
    monkeypatch.setenv(rs.CHECKS_POSTGRES_CONTAINER_ENV, checks_services["pg"])
    monkeypatch.setenv(rs.CHECKS_REDIS_CONTAINER_ENV, checks_services["redis"])
    monkeypatch.delenv("AGENT_HOST_PATH_MAP", raising=False)
    return checks_services


def _databases(pg):
    return _sh("docker", "exec", pg, "psql", "-U", "checks", "-d", "postgres", "-tAc",
               "SELECT datname FROM pg_database WHERE datname LIKE 'tektonix_ci_review_%'")


def _roles(pg):
    return _sh("docker", "exec", pg, "psql", "-U", "checks", "-d", "postgres", "-tAc",
               "SELECT rolname FROM pg_roles WHERE rolname LIKE 'tektonix_ci_review_%'")


def _plant_redis_leftover(rd):
    for db in range(rs.CHECKS_REDIS_DATABASES):
        _sh("docker", "exec", rd, "redis-cli", "-n", str(db), "set", "left-over", "yes")


def test_the_three_checks_run_contained_and_the_throwaway_database_is_dropped(bundle_env, fixture_project, monkeypatch):
    import asyncio

    _plant_redis_leftover(bundle_env["redis"])
    mounts = [(f"{fixture_project['live']}/node_modules", "/workspace/node_modules")]
    # The e2e script also proves the boundary: the internet is unreachable
    # from the checks container, the check runs as a plain role of its own,
    # and the superuser with the compose file's bootstrap password is refused.
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=fixture_project["wt"], mounts=mounts)))
    print("\n[direct] " + json.dumps(rows, indent=1))
    assert [(r["name"], r["ok"]) for r in rows] == [("db-drift", True), ("db-seed", True), ("e2e", True)], rows
    assert "no drift" in rows[0]["output"] and "seeded 2" in rows[1]["output"]
    assert "e2e ok" in rows[2]["output"]
    assert "internet unreachable" in rows[2]["output"], rows[2]["output"]
    assert "role tektonix_ci_review_" in rows[2]["output"] and " plain;" in rows[2]["output"], rows[2]["output"]
    assert "superuser login refused" in rows[2]["output"], rows[2]["output"]
    assert _databases(bundle_env["pg"]) == "", "the throwaway database must be dropped afterwards"
    assert _roles(bundle_env["pg"]) == "", "and its role with it"


def test_a_failing_seed_stops_before_e2e_and_still_drops_the_database(bundle_env, fixture_project, monkeypatch):
    import asyncio

    PROJECTS["shop"]["databaseCheck"]["seedCmd"] = {"cmd": "node", "args": ["-e", "console.error('seed exploded'); process.exit(3)"]}
    mounts = [(f"{fixture_project['live']}/node_modules", "/workspace/node_modules")]
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=fixture_project["wt"], mounts=mounts)))
    print("\n[failing seed] " + json.dumps(rows, indent=1))
    assert [(r["name"], r["ok"]) for r in rows] == [("db-drift", True), ("db-seed", False)], rows
    assert "seed exploded" in rows[1]["output"]
    assert _databases(bundle_env["pg"]) == "" and _roles(bundle_env["pg"]) == ""


def test_the_reviewer_s_own_module_gets_the_rows_from_the_agent_over_http(bundle_env, fixture_project):
    """checks.js in bundle mode -> POST /api/internal/review-sandbox/db-check
    on a real uvicorn serving the agent's router -> docker -> rows back."""
    import uvicorn
    from fastapi import FastAPI

    from agent.routers import review_sandbox as route

    app = FastAPI()
    app.include_router(route.router)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)
    try:
        cfg = {"name": "shop", "live": fixture_project["live"], "dependencyDirs": ["node_modules"],
               "databaseCheck": PROJECTS["shop"]["databaseCheck"]}
        checks_js = paths.REPO_ROOT / "services" / "commit-reviewer" / "checks.js"
        script = (f"const c=require({json.dumps(str(checks_js))});"
                  f"c.runDatabaseCheck({json.dumps(cfg)}, {json.dumps(fixture_project['wt'])})"
                  f".then(r=>console.log('ROWS '+JSON.stringify(r)))")
        env = {**os.environ, "TEKTONIX_BUNDLE": "1", "AGENT_SANDBOX_URL": f"http://127.0.0.1:{port}",
               "REVIEW_CONTROL_SECRET": "e2e-secret"}
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=600, env=env)
        assert out.returncode == 0, out.stderr
        rows = json.loads(out.stdout.split("ROWS ", 1)[1])
        print("\n[reviewer over HTTP] " + json.dumps(rows, indent=1))
        assert [(r["name"], r["ok"]) for r in rows] == [("db-drift", True), ("db-seed", True), ("e2e", True)], rows
        assert "delegating the database checks" in out.stderr + out.stdout
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert _databases(bundle_env["pg"]) == ""


def test_the_agent_never_connects_to_the_checks_services_itself(bundle_env, fixture_project, monkeypatch):
    """The setup goes through `docker exec`: with every outbound connection
    from this process forbidden, the run still succeeds. This is what lets
    the bundle keep the agent off the checks network."""
    import asyncio

    real = asyncio.open_connection

    async def forbidden(*a, **k):
        raise AssertionError(f"the agent opened a connection itself: {a} {k}")

    monkeypatch.setattr(asyncio, "open_connection", forbidden)
    try:
        import psycopg  # noqa: PLC0415

        monkeypatch.setattr(psycopg.AsyncConnection, "connect", classmethod(lambda cls, *a, **k: forbidden()))
    except ImportError:
        pass
    mounts = [(f"{fixture_project['live']}/node_modules", "/workspace/node_modules")]
    rows = asyncio.run(rs.run_database_check(rs.DbCheckRequest(project="shop", worktree=fixture_project["wt"], mounts=mounts)))
    monkeypatch.setattr(asyncio, "open_connection", real)
    print("\n[no connections from the agent] " + json.dumps([(r["name"], r["ok"]) for r in rows]))
    assert [(r["name"], r["ok"]) for r in rows] == [("db-drift", True), ("db-seed", True), ("e2e", True)], rows
