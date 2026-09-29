"""One meaning of a review record, in Node and in Python.

`harnessFailed` lived three times: reviewer.js, review_gate.py and
verify_and_ship.py, and the third had drifted -- no verdict condition --
so a READY record that carried a failing infrastructure check sent the
merge path back to re-ask the reviewer (2026-09-29). The Node copy is now
services/shared/review-records.js, the Python copy is review_gate's, and
this feeds the same records to every one of them."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent.nodes import verify_and_ship as vs
from agent.tools import review_gate

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "services" / "shared" / "review-records.js"
REVIEWER = ROOT / "services" / "commit-reviewer" / "reviewer.js"

INFRA_FAIL = {"name": "test", "ok": False, "infrastructure": True, "output": "SETUP: no sandbox"}
REAL_FAIL = {"name": "lint", "ok": False, "output": "1 error"}
INFRA_PASS = {"name": "test", "ok": True, "infrastructure": True}

RECORDS = [
    ("harness failure", {"verdict": "NEEDS_FIXES", "checkResults": [INFRA_FAIL]}, True),
    ("harness failure beside a real one", {"verdict": "NEEDS_FIXES", "checkResults": [REAL_FAIL, INFRA_FAIL]}, True),
    ("a READY carrying a failing infrastructure check", {"verdict": "READY", "checkResults": [INFRA_FAIL]}, False),
    ("a real failure only", {"verdict": "NEEDS_FIXES", "checkResults": [REAL_FAIL]}, False),
    ("infrastructure that passed", {"verdict": "NEEDS_FIXES", "checkResults": [INFRA_PASS]}, False),
    ("no checks at all", {"verdict": "NEEDS_FIXES"}, False),
    ("checks that are not a list", {"verdict": "NEEDS_FIXES", "checkResults": "broken"}, False),
    ("a check that is not an object", {"verdict": "NEEDS_FIXES", "checkResults": [None, "x", INFRA_FAIL]}, True),
    ("no verdict field, harness failed", {"checkResults": [INFRA_FAIL]}, True),
    ("empty record", {}, False),
]


def _node_verdicts() -> dict:
    script = (
        f"const shared = require({json.dumps(str(SHARED))});"
        f"const r = require({json.dumps(str(REVIEWER))});"
        "const records = JSON.parse(process.argv[1]);"
        "console.log(JSON.stringify({"
        "  shared: records.map((x) => shared.harnessFailed(x)),"
        "  reviewer: records.map((x) => r.harnessFailed(x)),"
        "  same: r.harnessFailed === shared.harnessFailed && r.branchRecord === shared.branchRecord"
        "    && r.TASK_BRANCH_RE === shared.TASK_BRANCH_RE,"
        "}));"
    )
    out = subprocess.run(
        ["node", "-e", script, json.dumps([rec for _, rec, _ in RECORDS])],
        capture_output=True, text=True, timeout=60, cwd=str(ROOT),
        env={**os.environ, "REVIEW_STATE_DIR": str(ROOT / "tests" / ".scratch-review-state"), "MODEL_ROUTER_KEY": "unused"},
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("label, record, expected", RECORDS, ids=[r[0] for r in RECORDS])
def test_every_python_copy_agrees(label, record, expected):
    assert review_gate.harness_failed(record) is expected
    assert vs._harness_failed(record) is expected, "verify_and_ship reads the record like the gate does"


def test_the_node_copies_agree_with_python_on_the_same_table():
    got = _node_verdicts()
    expected = [e for _, _, e in RECORDS]
    assert got["shared"] == expected, list(zip([r[0] for r in RECORDS], got["shared"], strict=False))
    assert got["reviewer"] == expected
    assert got["same"] is True, "reviewer.js re-exports the shared helpers rather than keeping copies"


def test_verify_and_ship_has_no_copy_of_its_own():
    assert vs._harness_failed is review_gate.harness_failed
    src = (ROOT / "agent" / "nodes" / "verify_and_ship.py").read_text()
    assert "def _harness_failed" not in src
