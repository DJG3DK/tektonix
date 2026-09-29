"""The review dashboard's Router tab reads the ledger the environment names.

In the bundle the router's ledger is a volume at a path compose sets
(MODEL_ROUTER_LEDGER, the same variable the agent's Analytics page uses),
not services/model-router/logs/routing.jsonl beside a router that is a
different container. The tab read the host path and showed nothing.

A real server.js on a free port, like tests/test_review_services_gated.py,
and skipped the same way where express is not installed -- unless
REQUIRE_SERVICE_TESTS=1, which CI sets.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.request

import pytest

from agent import paths

SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _express_installed() -> bool:
    return subprocess.run(["node", "-e", "require.resolve('express')"], cwd=paths.REPO_ROOT / "services/agent-review",
                          capture_output=True).returncode == 0


if not _express_installed():
    if os.environ.get("REQUIRE_SERVICE_TESTS") == "1":
        pytest.fail("REQUIRE_SERVICE_TESTS=1 but services/agent-review has no express installed")
    pytestmark = pytest.mark.skip(reason="services/agent-review has no node_modules")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def review_server(tmp_path):
    ledger = tmp_path / "somewhere-else" / "routing.jsonl"
    ledger.parent.mkdir()
    lines = [
        {"alias": "agent-coder", "routed_model": "x/one", "cost": 0.5, "prompt_tokens": 10, "completion_tokens": 5},
        {"alias": "agent-coder", "routed_model": "x/one", "cost": 0.25, "prompt_tokens": 4, "completion_tokens": 2},
        {"alias": "agent-planner", "routed_model": "y/two", "error": "boom"},
    ]
    ledger.write_text("".join(json.dumps(line) + "\n" for line in lines))
    projects = tmp_path / "projects.json"
    projects.write_text('{"projects": {}}')
    (tmp_path / "state").mkdir()
    port = _free_port()
    env = {**os.environ,
           "AGENT_PROJECTS_JSON": str(projects),
           "REVIEW_ONLY_PROJECTS_JSON": "1",
           "REVIEW_STATE_DIR": str(tmp_path / "state"),
           "REVIEW_WORKTREE_ROOT": str(tmp_path / "wt"),
           "REVIEW_CONTROL_SECRET": SECRET,
           "MODEL_ROUTER_KEY": "unused",
           "MODEL_ROUTER_LEDGER": str(ledger),
           "REVIEW_BIND_ADDRESS": "127.0.0.1",
           "REVIEW_SERVICE_PORT": str(port)}
    proc = subprocess.Popen(["node", str(paths.REPO_ROOT / "services/agent-review/server.js")], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read()
                break
            except OSError:
                if proc.poll() is not None:
                    pytest.fail("agent-review exited during startup")
                time.sleep(0.1)
        else:
            pytest.fail("agent-review never answered /health")
        yield port
    finally:
        proc.kill()
        proc.wait()


def test_the_router_tab_reads_the_ledger_named_by_the_environment(review_server):
    req = urllib.request.Request(f"http://127.0.0.1:{review_server}/api/router/stats",
                                 headers={"X-Review-Secret": SECRET, "Sec-Fetch-Site": "same-origin"})
    body = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert body["totals"]["requests"] == 2 and body["totals"]["errors"] == 1
    assert body["totals"]["cost"] == pytest.approx(0.75)
