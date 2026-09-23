"""The review gate must never pass a commit it did not check, and must review
the commit it was asked about.

Both failures surfaced on 2026-09-22 on one project, the same night.

1. A missing tool read as a pre-existing failure. Lint, build and test failed
   with `oxlint: not found` / `vite: not found`; the base failed identically,
   because the environment is the same; every check was marked pre-existing,
   and every commit came back "READY -- no issues found" having run nothing.
   The infrastructure escalation existed for exactly this and never fired,
   because it filtered on `!c.preexisting`.

2. A re-review asked for a branch the reviewer would never pick. One review
   unit per project, "older unmerged branches are never visited" -- and with a
   queue and pull-request shipping, several branches sit parked at once. An
   approved one waited 900s for a review that was never going to happen.
"""
import json
import subprocess

from agent import paths

REVIEWER = paths.REPO_ROOT / "services" / "commit-reviewer" / "reviewer.js"


def node(expr: str) -> str:
    out = subprocess.run(["node", "-e", f"const r=require({json.dumps(str(REVIEWER))});{expr}"],
                         capture_output=True, text=True, timeout=60, cwd=str(REVIEWER.parent))
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def _classify_then_baseline(checks: list[dict], baseline: dict) -> list[dict]:
    """Run the two steps in the order reviewProject runs them."""
    return json.loads(node(
        f"const c={json.dumps(checks)};"
        f"r.classifyInfrastructureFailures(c); r.applyBaseline(c, {json.dumps(baseline)});"
        f"console.log(JSON.stringify(c))"))


def test_a_missing_tool_is_never_marked_preexisting():
    """The rubber stamp, exactly as it happened."""
    checks = [
        {"name": "lint", "ok": False, "output": "> frontend@0.0.0 lint\n> oxlint\nsh: 1: oxlint: not found"},
        {"name": "build", "ok": False, "output": "> vite build\nsh: 1: vite: not found"},
    ]
    out = _classify_then_baseline(checks, {"lint": False, "build": False})   # base "also failed"
    for c in out:
        assert c.get("infrastructure") is True, f"{c['name']} not recognised as a missing tool"
        assert not c.get("preexisting"), (
            f"{c['name']} was marked pre-existing -- that is what made every review READY")


def test_a_genuine_code_failure_on_base_still_counts_as_preexisting():
    """The baseline exists for a real reason: a check already broken on main
    must not be blamed on this commit. Only missing TOOLS are excluded."""
    checks = [{"name": "test", "ok": False, "output": "AssertionError: expected 3 to equal 4"}]
    out = _classify_then_baseline(checks, {"test": False})
    assert out[0].get("preexisting") is True
    assert not out[0].get("infrastructure")


def test_a_module_resolution_error_stays_the_agents_problem():
    """`Cannot find module './x'` is usually a real defect in the code under
    review, so it is deliberately NOT treated as a missing tool."""
    checks = [{"name": "test", "ok": False, "output": "Error: Cannot find module './helpers'"}]
    out = _classify_then_baseline(checks, {})
    assert not out[0].get("infrastructure")


def test_the_escalation_that_should_have_fired_now_can():
    """The filter that let it through: infrastructure AND not preexisting.
    With applyBaseline no longer setting preexisting on a missing tool, a
    missing tool reaches the escalation."""
    src = REVIEWER.read_text()
    assert "!c.ok && !c.preexisting && c.infrastructure" in src   # the escalation's own filter
    assert "if (c.infrastructure) continue;" in src               # ...which applyBaseline now respects



def test_a_read_only_filesystem_is_never_marked_preexisting():
    """The second disguise, found while verifying the first fix: `vite build`
    died with EROFS writing its cache into a read-only mount, the base did
    the same, and the check was filed as pre-existing without ever running."""
    checks = [{"name": "build", "ok": False, "output":
               "Error: EROFS: read-only file system, open "
               "'/workspace/frontend/node_modules/.vite-temp/vite.config.js.timestamp.mjs'\n"
               "  code: 'EROFS'"}]
    out = _classify_then_baseline(checks, {"build": False})
    assert out[0].get("infrastructure") is True
    assert not out[0].get("preexisting")


def test_build_caches_are_not_linked_but_dependency_structure_is():
    """What makes EROFS happen in the first place: a cache left in live's
    node_modules, linked into the worktree, mounted read-only."""
    caches = set(json.loads(node("console.log(JSON.stringify([...r.NM_BUILD_CACHES]))")))
    assert {".vite", ".vite-temp", ".cache", ".tmp"} <= caches
    # pnpm's store and the bin shims are how the checks find their tools at all.
    assert not caches & {".bin", ".pnpm", ".modules.yaml", ".package-lock.json"}
    src = REVIEWER.read_text()
    assert "if (NM_BUILD_CACHES.has(entry)) continue;" in src

# --- reviewing the branch you were asked about ------------------------------

def test_check_splits_the_query_before_matching_the_project():
    """`[^/]+` would read "proj?branch=agent/..." as the project name."""
    src = REVIEWER.read_text()
    assert "new URL(req.url" in src
    assert "parsed.searchParams.get('branch')" in src


def test_a_requested_branch_wins_over_the_guess():
    src = REVIEWER.read_text()
    assert "let ref = (requested && candidates.includes(requested)) ? requested : null;" in src


def test_only_a_real_task_branch_can_be_requested():
    """Anything else falls back to the old choice rather than reviewing an
    arbitrary ref -- `candidates` is already filtered by TASK_BRANCH_RE."""
    src = REVIEWER.read_text()
    i = src.index("const candidates = all.filter((r) => TASK_BRANCH_RE.test(r));")
    j = src.index("let ref = (requested && candidates.includes(requested))")
    assert i < j


def test_the_agent_names_the_branch_when_it_asks_for_a_review():
    import inspect

    from agent.nodes import verify_and_ship as vs
    src = inspect.getsource(vs._review_and_deploy)
    assert "task_branch_name(state[\"task_id\"])" in src
    assert "await trigger_check(repo, branch)" in src


async def test_trigger_check_sends_the_branch_as_a_query_param(monkeypatch):
    from agent.tools import review_gate as rg
    seen = {}

    class _Resp:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "started": True}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, headers=None, params=None):
            seen.update(url=url, params=params)
            return _Resp()

    monkeypatch.setattr(rg.httpx, "AsyncClient", _Client)
    await rg.trigger_check("proj", "agent/0b5f2c1a-1111-2222-3333-444455556666")
    assert seen["url"].endswith("/check/proj")
    assert seen["params"] == {"branch": "agent/0b5f2c1a-1111-2222-3333-444455556666"}
    await rg.trigger_check("proj")
    assert seen["params"] is None, "no branch means the old behaviour, not an empty param"


def test_one_definition_of_a_task_branch_name():
    """It was written out in three places. The reviewer accepts only this
    shape, so a copy that drifted would name a branch it refuses -- silently."""
    import re

    from agent.evals import fixtures
    from agent.tools.git import task_branch_name
    tid = "0b5f2c1a-1111-2222-3333-444455556666"
    assert task_branch_name(tid) == f"agent/{tid}" == fixtures.task_branch_name(tid)
    for f in ("agent/nodes/verify_and_ship.py", "agent/evals/fixtures.py"):
        src = (paths.REPO_ROOT / f).read_text()
        assert not re.search(r'f"agent/\{', src), f"{f} still builds the name inline"
