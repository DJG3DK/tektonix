"""Memory freshness (agent/memory_freshness.py): a remembered fact that cites
a file is flagged once that file changes after the fact was first seen."""

import json
import subprocess

from agent.memory_freshness import (
    find_path_mentions, last_change_dates, render_flags, stale_flags, update_ledger,
)

TREE = ["src/strategies/trendSignal.js", "src/strategies/gridder.js", "src/core/bot.js", "src/util/bot.js", "README.md"]


def test_finds_exact_paths_and_unique_basenames_only():
    memory = """# notes
- LEVEL_RECENCY_WEIGHT is plumbed through `src/strategies/trendSignal.js` and sweepable.
- gridder.js grades levels with a fixed recencyWeight.
- bot.js is ambiguous (two of them) and must not resolve.
- README.md is documentation.
"""
    mentions = find_path_mentions(memory, TREE)
    paths = [p for _, p in mentions]
    assert "src/strategies/trendSignal.js" in paths
    assert "src/strategies/gridder.js" in paths, "a basename unique in the tree resolves"
    assert not any(p.endswith("bot.js") for p in paths), "an ambiguous basename is not evidence"
    assert "README.md" in paths


def test_ledger_keeps_first_seen_and_drops_vanished_lines():
    mentions = [("fact A about x.js", "x.js"), ("fact B about y.js", "y.js")]
    ledger = update_ledger({}, mentions, "2026-09-01")
    assert all(e["first_seen"] == "2026-09-01" for e in ledger.values())
    later = update_ledger(ledger, [("fact A about x.js", "x.js")], "2026-09-08")
    assert len(later) == 1
    assert next(iter(later.values()))["first_seen"] == "2026-09-01", "an unchanged line keeps its original date"


def test_stale_flags_only_when_the_file_changed_after_the_fact():
    ledger = {
        "a": {"path": "x.js", "line": "fact A", "first_seen": "2026-09-01"},
        "b": {"path": "y.js", "line": "fact B", "first_seen": "2026-09-08"},
    }
    flags = stale_flags(ledger, {"x.js": "2026-09-05", "y.js": "2026-09-05"})
    assert [f["path"] for f in flags] == ["x.js"]
    assert "x.js changed 2026-09-05" in render_flags(flags, "demo")
    assert render_flags([], "demo") == ""


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_last_change_dates_come_from_git(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    (tmp_path / "x.js").write_text("1")
    _git(tmp_path, "add", "x.js")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add x")
    dates = last_change_dates(str(tmp_path), {"x.js", "missing.js"})
    assert set(dates) == {"x.js"}
    assert len(dates["x.js"]) == 10  # YYYY-MM-DD


async def test_refresh_writes_ledger_and_rendered_flags(tmp_path):
    from agent.memory_freshness import LEDGER_PATH, STALE_PATH, refresh_memory_freshness

    class Backend:
        def __init__(self): self.files = {}
        async def aread(self, path):
            class R: pass
            r = R()
            r.error = None if path in self.files else "missing"
            r.file_data = {"content": [self.files[path]]} if path in self.files else None
            return r
        async def awrite(self, path, content): self.files[path] = content

    _git(tmp_path, "init", "-q")
    (tmp_path / "x.js").write_text("1")
    _git(tmp_path, "add", "x.js")
    _git(tmp_path, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "add x")
    backend = Backend()
    # the fact was "first seen" long before the file's commit: it must flag
    summary = await refresh_memory_freshness("demo", str(tmp_path), backend, "- x.js does a thing", ["x.js"], today="2000-01-01")
    assert summary == {"cited_paths": 1, "stale": 1}
    assert json.loads(backend.files[LEDGER_PATH])
    assert "POSSIBLY STALE" in backend.files[STALE_PATH]


# --- which section a stale fact lives in ------------------------------------
#
# A flag quotes the fact and names the repo file, which was enough while the
# whole memory sat in every prompt. Split (agent/memory_sections.py), the fact
# may be in a section the prompt is not carrying -- so the agent is told
# something it cannot see is stale, with nowhere to go.

_SPLIT_MEMORY = """# a project

Preamble.

## Build wiring

The build reads config/build.json before anything else.

## Deployment

Deploys run from scripts/deploy.sh on the host.
"""


def test_a_flagged_fact_names_the_section_it_lives_in():
    from agent.memory_freshness import section_of

    assert section_of(_SPLIT_MEMORY, "Deploys run from scripts/deploy.sh on the host.") == "deployment"
    assert section_of(_SPLIT_MEMORY, "The build reads config/build.json before anything else.") == "build-wiring"


def test_a_fact_in_no_section_is_left_unattributed():
    """Better unattributed than attributed wrongly -- the preamble is always
    in the prompt, so there is nothing to point at."""
    from agent.memory_freshness import section_of

    assert section_of(_SPLIT_MEMORY, "Preamble.") == ""
    assert section_of(_SPLIT_MEMORY, "a line that is not in this memory at all") == ""
    assert section_of("", "anything") == ""
    assert section_of(_SPLIT_MEMORY, "   ") == ""


def test_the_rendered_flag_carries_the_section_when_there_is_one():
    from agent.memory_freshness import render_flags

    out = render_flags([{
        "path": "scripts/deploy.sh", "changed": "2026-09-01", "first_seen": "2026-08-01",
        "line": "Deploys run from scripts/deploy.sh on the host.", "section": "deployment",
    }], "proj")
    assert "in section deployment" in out
    assert "Deploys run from scripts/deploy.sh" in out


def test_the_rendered_flag_is_unchanged_for_an_unsplit_project():
    """Most projects are under the split floor and have no sections; their
    flags must read exactly as they always did."""
    from agent.memory_freshness import render_flags

    flag = {"path": "a.py", "changed": "2026-09-01", "first_seen": "2026-08-01", "line": "A fact."}
    assert render_flags([flag], "proj") == render_flags([{**flag, "section": ""}], "proj")
    assert "in section" not in render_flags([flag], "proj")
