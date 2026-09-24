"""docs/middleware.md must describe the chains that actually run.

The incident comments in agent/middleware/ are an asset and a terrible index:
they answer "why does this exist" in full and "which agent has the budget
guard" not at all. The inventory page answers the second question, which
makes it worth exactly as much as it is current.

So this test reads both sides. The chains come out of the source with ast --
every `middleware=[...]` list, whether it is a create_deep_agent call or a
subagent spec dict -- and the table comes out of the markdown. They must
agree, in both directions, for every agent.

A middleware attached to an agent and left out of the page fails here. So
does a row for a chain that no longer has it: a page that over-promises is
how someone concludes a subagent is budget-guarded when it is not, which is
the exact hole that let a general-purpose subagent spend invisibly.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "middleware.md"

# Where each agent is built. The keys are the column names in the doc.
_SOURCES = {
    "agent/deep_agent.py": ("coordinator", "investigator", "test-writer", "general-purpose", "verifier"),
    "agent/planning_chat.py": ("planner",),
    "agent/consolidation.py": ("consolidation",),
}


def _names_in(node: ast.List) -> set[str]:
    out = set()
    for el in node.elts:
        f = el.func if isinstance(el, ast.Call) else el
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def _chains_in(path: pathlib.Path) -> dict[str, set[str]]:
    """chain name -> middleware class names, read from the source itself."""
    tree = ast.parse(path.read_text())
    found: dict[str, set[str]] = {}

    # A subagent spec is a dict literal with a "middleware" key. Its name is
    # its own "name" entry, or -- for general-purpose, which spreads the
    # library's spec -- the variable it is assigned to.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            target = node.targets[0]
            var = target.id if isinstance(target, ast.Name) else None
            spec_name, mw = None, None
            for k, v in zip(node.value.keys, node.value.values, strict=False):
                if not isinstance(k, ast.Constant):
                    continue
                if k.value == "name" and isinstance(v, ast.Constant):
                    spec_name = v.value
                if k.value == "middleware" and isinstance(v, ast.List):
                    mw = _names_in(v)
            if mw is not None:
                found[spec_name or (var or "?").replace("_", "-")] = mw

    # The agent itself: the create_deep_agent call's own middleware kwarg.
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
            if fn != "create_deep_agent":
                continue
            for kw in node.keywords:
                if kw.arg == "middleware" and isinstance(kw.value, ast.List):
                    found["__main__"] = _names_in(kw.value)
    return found


def _live_chains() -> dict[str, set[str]]:
    chains: dict[str, set[str]] = {}
    for rel, names in _SOURCES.items():
        found = _chains_in(REPO / rel)
        main = names[0]
        assert "__main__" in found, f"{rel}: no create_deep_agent(middleware=[...]) found"
        chains[main] = found.pop("__main__")
        for name in names[1:]:
            assert name in found, f"{rel}: no subagent spec named {name!r} with middleware"
            chains[name] = found[name]
    return chains


def _doc_table() -> tuple[list[str], dict[str, set[str]]]:
    """(column order, middleware -> the chains marked with a dot)."""
    rows = [ln for ln in DOC.read_text().splitlines()
            if ln.startswith("|") and not re.fullmatch(r"\|[-:| ]+\|", ln.strip())]
    assert rows, "docs/middleware.md has no table"
    header = [c.strip() for c in rows[0].strip("|").split("|")]
    columns = header[2:]
    table: dict[str, set[str]] = {}
    for row in rows[1:]:
        cells = [c.strip() for c in row.strip("|").split("|")]
        m = re.match(r"`([A-Za-z]+)`", cells[0])
        if not m:
            continue
        table[m.group(1)] = {col for col, cell in zip(columns, cells[2:], strict=False) if cell}
    return columns, table


def test_the_doc_lists_every_agent_that_exists():
    columns, _ = _doc_table()
    assert columns == list(_live_chains()), "the table's columns are no longer the agents that run"


@pytest.mark.parametrize("chain", sorted(_live_chains()))
def test_every_middleware_on_this_agent_is_in_the_inventory(chain):
    _, table = _doc_table()
    documented = {name for name, chains in table.items() if chain in chains}
    live = _live_chains()[chain]
    missing = live - documented
    assert not missing, f"{chain} runs {sorted(missing)}, which docs/middleware.md does not list for it"


@pytest.mark.parametrize("chain", sorted(_live_chains()))
def test_the_inventory_does_not_promise_middleware_the_agent_lacks(chain):
    """Over-promising is the dangerous direction: it is how someone concludes
    a subagent is budget-guarded when nothing attached the guard."""
    _, table = _doc_table()
    documented = {name for name, chains in table.items() if chain in chains}
    extra = documented - _live_chains()[chain]
    assert not extra, f"docs/middleware.md claims {sorted(extra)} for {chain}, which does not have them"


def test_every_local_middleware_module_is_described():
    """A module under agent/middleware/ that the page never mentions is a
    rule nobody extending this system will know exists."""
    doc = DOC.read_text()
    for path in sorted((REPO / "agent" / "middleware").glob("*.py")):
        if path.name.startswith("_"):
            continue
        classes = [n.name for n in ast.walk(ast.parse(path.read_text()))
                   if isinstance(n, ast.ClassDef) and n.name.endswith("Middleware")]
        if not classes:
            continue
        assert path.name in doc, f"{path.name} is not referenced in docs/middleware.md"
        for cls in classes:
            assert cls in doc, f"{cls} ({path.name}) is not in docs/middleware.md"
