"""Two properties of the repository itself that nothing else would notice.

Both of these were reported by a reviewer rather than by any check:

* CONTRIBUTING.md promises "the exact five jobs CI runs, in order" and then
  drifted from .github/workflows/ci.yml -- it named `tsc -b --noEmit` while
  CI ran `tsc --noEmit -p tsconfig.app.json`, and two whole CI steps were
  missing from it. A contributor who follows a stale list and then watches CI
  fail stops trusting the document, which is worse than having no list.
* A raw NUL byte in a TypeScript source made git classify the module as
  binary, so every diff of it read "Binary files differ" and no reviewer ever
  saw a line of it change.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parent.parent
CI = REPO / ".github" / "workflows" / "ci.yml"
CONTRIBUTING = REPO / "CONTRIBUTING.md"

# Source extensions worth asserting are text. Binary fixtures (images, the
# screenshots under docs/) are deliberately not in this list.
_TEXT_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html",
                  ".md", ".json", ".yml", ".yaml", ".sh", ".toml"}
_SKIP_DIRS = {".git", "node_modules", "dist", ".venv", "__pycache__", "backups",
              "logs", ".pytest_cache", ".mypy_cache", "coverage"}


def _ci_run_lines() -> list[str]:
    """Every shell line CI actually executes, flattened across steps."""
    doc = yaml.safe_load(CI.read_text())
    lines: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        for step in job.get("steps") or []:
            run = step.get("run")
            if not run:
                continue
            for raw in str(run).splitlines():
                line = raw.strip()
                if line and not line.startswith("#"):
                    lines.append(line)
    return lines


def _contributing_block() -> list[str]:
    """The commands inside CONTRIBUTING's 'exactly what CI does' block."""
    text = CONTRIBUTING.read_text()
    marker = "The exact five jobs CI runs"
    assert marker in text, "CONTRIBUTING no longer claims to mirror CI -- update this test with it"
    block = text.split(marker, 1)[1].split("```bash", 1)[1].split("```", 1)[0]
    # unwrap `\`-continued lines so a wrapped command reads as one command
    block = block.replace("\\\n", " ")
    return [ln.strip() for ln in block.splitlines() if ln.strip() and not ln.strip().startswith("#")]


# The commands a contributor most needs to get right: each is a gate whose
# local spelling must match CI's, or the contributor's green run means
# nothing. Matched as substrings of a CI line so env prefixes do not matter.
_MUST_MIRROR = [
    "pytest -q",
    "ruff check .",
    "pytest -q services/model-router/tests",
    "ruff check services/model-router",
    "npx tsc --noEmit -p tsconfig.app.json",
    "npm run lint",
    "npm run build",
    "test_newsletter.py",
    "node --check",
    "scripts/doctor.py --quiet",
    "install.sh --dry-run --yes",
    "scripts/verify_stack_checks.py --no-docker",
    "REQUIRE_MOUNT_TESTS=1 node tests/test_reviewer_dependency_dirs.js",
]


@pytest.mark.parametrize("command", _MUST_MIRROR)
def test_contributing_names_the_command_ci_actually_runs(command):
    ci = "\n".join(_ci_run_lines())
    assert command in ci, f"{command!r} is no longer in ci.yml -- update CONTRIBUTING and this list"
    assert any(command in line for line in _contributing_block()), \
        f"CI runs {command!r} but CONTRIBUTING's block does not"


def test_every_node_test_ci_runs_is_listed_for_contributors():
    """The node suites are the easiest thing to add to CI and forget to
    document: they are one line each, and a contributor who never runs them
    only finds out from a red push."""
    in_ci = {re.search(r"node (tests/\S+\.js)", line).group(1)
             for line in _ci_run_lines() if re.search(r"node tests/\S+\.js", line)}
    assert in_ci, "no node tests in ci.yml -- did the step move?"
    listed = "\n".join(_contributing_block())
    missing = sorted(t for t in in_ci if t not in listed)
    assert not missing, f"CI runs these and CONTRIBUTING does not list them: {missing}"


def test_contributing_does_not_promise_a_command_ci_skips():
    """The other direction: a contributor running something CI does not is
    harmless, but a command that no longer exists anywhere is a trap."""
    ci = "\n".join(_ci_run_lines())
    for line in _contributing_block():
        m = re.match(r"node (tests/\S+\.js)", line)
        if m:
            assert m.group(1) in ci, f"CONTRIBUTING lists {m.group(1)}, which CI does not run"
            assert (REPO / m.group(1)).is_file(), f"{m.group(1)} does not exist"


def _source_files():
    for path in REPO.rglob("*"):
        if not path.is_file() or path.suffix not in _TEXT_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        yield path


def test_no_source_file_contains_a_raw_nul_byte():
    """One NUL is enough for git to treat a whole file as binary: no diff in
    review, no `git diff` locally, no blame that means anything. Write the
    escape (`\\u0000`, `\\0`) instead -- the runtime value is identical."""
    offenders = [str(p.relative_to(REPO)) for p in _source_files() if b"\x00" in p.read_bytes()]
    assert not offenders, f"raw NUL byte in: {offenders}"


def test_the_landing_page_is_not_part_of_an_installation():
    """site/ is tektonix.io's public page: marketing, not product.

    Somebody self-hosting the agent wants the console, not a page selling it
    to them, so the release tarball drops the directory and install.sh never
    looks at it. Both halves are checked because either one alone would let it
    back in: the packager could stop deleting it, or the installer could start
    building it.
    """
    packager = (REPO / "scripts" / "package_release.sh").read_text()
    assert 'rm -rf "$STAGE/$NAME/site"' in packager, (
        "package_release.sh no longer drops site/ from the tarball")

    installer = (REPO / "install.sh").read_text()
    for line in installer.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert "site/" not in stripped and "cd site" not in stripped, (
            f"install.sh references the landing page: {stripped!r}")


def test_the_console_and_the_landing_page_do_not_share_source():
    """The split is only real if neither build reaches into the other.

    frontend/ importing from site/ would put marketing code in the console's
    bundle; site/ importing from frontend/ would mean the landing page cannot
    be lifted out and hosted on its own, which is half the point.
    """
    for src, other in (("frontend", "site"), ("site", "frontend")):
        root = REPO / src / "src"
        for f in list(root.rglob("*.ts")) + list(root.rglob("*.tsx")) + list(root.rglob("*.css")):
            text = f.read_text()
            assert f"../../{other}/" not in text and f"/{other}/src/" not in text, (
                f"{src}/{f.relative_to(root)} reaches into {other}/")


# The maintainer's own software must not appear in the public tree.
#
# Each needle is assembled from halves so that this file, which is itself
# scanned, does not trip its own check. That is not cleverness for its own
# sake: an exclusion list is a hole, and the one file guaranteed to contain
# every banned string should not be the one file nobody checks.
_FORBIDDEN = [
    ("3D", "Steals"),
    ("3d-", "bot"),
    ("3dweb", "catchers"),
    ("3dcrypto", "bots"),
    ("agent", "Email"),
    ("Agent_", "Email"),
    ("mail-", "chat"),
    ("mail-", "triage"),
    ("trade-", "gate"),
    ("trading ", "bot"),
    ("trading-", "bot"),
]

# Where the agent's own name legitimately appears, and the one place history is
# allowed to keep its own wording.
_SCAN_SKIP_PREFIXES = ("docs/history/",)


def _tracked_text_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True)
    for rel in out.stdout.split():
        if rel.startswith(_SCAN_SKIP_PREFIXES):
            continue
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            yield rel, p.read_text()
        except (UnicodeDecodeError, OSError):
            continue


def test_no_private_project_names_anywhere_in_the_public_tree():
    """This repository is public, and everything in it ships.

    The seed router config carried three aliases belonging to two applications
    that are not this product, with comments naming their checkouts, and the
    Models page introduced them by name to every installation. Somebody reading
    a fresh install was looking at an inventory of one maintainer's other
    software.

    A grep is the whole defence. Nothing else catches a name that arrives in a
    comment written to explain a real incident, which is exactly how these got
    in -- each one was true, useful, and nobody's business.
    """
    hits = []
    for rel, text in _tracked_text_files():
        low = text.lower()
        for a, b in _FORBIDDEN:
            needle = (a + b).lower()
            if needle in low:
                for n, line in enumerate(text.splitlines(), 1):
                    if needle in line.lower():
                        hits.append(f"{rel}:{n}: {line.strip()[:90]}")
    assert not hits, (
        "the maintainer's own projects appear in the public tree:\n  "
        + "\n  ".join(hits[:20])
    )
