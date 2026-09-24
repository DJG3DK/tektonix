"""On a benchmark project, the agent does not go looking for the answer.

2026-09-24, SWE-bench sample: tasks spent most of their budget searching for
the published fix instead of writing one -- git's object store (`cat-file
--batch-all-objects`, `fsck --lost-found`), `git log --all`, `pip download` of
the fixed release, `grep -r` and `find` across the whole filesystem, even
`find / -iname "*eval*"` for the grader. Nothing was there to find: the
repository has no later commits, the sandbox has no network, and the grading
tests are not in the environment. But one task spent 41 turns on it, made no
edit at all, and a trajectory like that is what disqualifies a leaderboard
submission.

The task statement says so up front (agent/evals/swebench.py); this refuses
the searches themselves, with the same explanation, for benchmark projects
only. Reading the repository, its own history of a file, and installed
dependencies stays allowed -- that is ordinary work.
"""

from __future__ import annotations

import re

from agent.harness_voice import HARNESS

_WHY = ("The fix for this issue does not exist anywhere in this environment: the repository has no "
        "commits after this point, there is no newer package and no network, and the tests that will "
        "grade the fix are not here. Searching for them only spends the budget. Work from the issue "
        "text and the code: reproduce the problem, fix it, verify the fix.")

_HUNTS = (
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?cat-file\b[^;&|]*--batch-all-objects"), "scanning git's whole object store"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?fsck\b"), "looking for unreachable git objects"),
    (re.compile(r"\bgit\s+(?:-C\s+\S+\s+)?(?:log|rev-list|show-branch|shortlog)\b[^;&|]*\s--(?:all|branches|tags|remotes|reflog)\b"),
     "searching other branches and tags for later commits"),
    (re.compile(r"\b(?:pip3?|python3?\s+-m\s+pip|conda|mamba)\s+(?:download|install)\b"), "fetching a package"),
    (re.compile(r"\b(?:curl|wget)\b"), "fetching from the network"),
    (re.compile(r"\b(?:find|grep|rg|locate|du|ls\s+-R)\b[^;&|]*\s/(?=\s|$|\*)"), "searching the whole filesystem"),
    (re.compile(r"\s(?:/root/\.cache|/opt/miniconda3/pkgs|/tmp/tektonix)"), "searching caches outside the repository"),
)


def hunt_reason(command: str) -> str | None:
    """Why this command is a search for the answer, or None."""
    for rx, why in _HUNTS:
        if rx.search(command or ""):
            return why
    return None


def refusal(command: str) -> str | None:
    why = hunt_reason(command)
    return f"{HARNESS} REFUSED on a benchmark task ({why}). {_WHY}" if why else None
