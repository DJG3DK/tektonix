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
    out = asyncio.run(rs.run_check(_req(bundle)))
    assert out["ok"] is False and out["code"] == 124
    assert killed and killed[0].startswith("rvw-")
