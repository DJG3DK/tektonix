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

2026-09-25, 50 tasks: 62 refusals, and most of them were compounds -- `git log
--all --oneline | head -5 && git status --short`, `grep -rn "has_key" django/
; find / -name json.py` -- where the legitimate half was thrown away with the
hunt. In one task the lost `grep` would have led straight to the regression
the task then failed on. So the command is split into its top-level segments
and only the hunting segments are dropped: the rest RUNS, with a note on the
front of the result naming what was refused and why. A pipeline is one
segment -- if any stage of it hunts, the whole pipeline goes.

The same run showed what walked past the patterns: `strings` and `marshal` on
stale `__pycache__` bytecode (listing the hidden tests' names), `git branch
-a && git tag | tail -5` (looking for a later release), `git for-each-ref`,
`gh api repos/.../pulls/13033/files`, `ls ~/.cache/pip`. Those are hunts too.
"""

from __future__ import annotations

import re

from agent.tools.bash_advice import BENCHMARK_REFUSED_PREFIX, split_top_level

_WHY = ("The fix for this issue does not exist anywhere in this environment: the repository has no "
        "commits after this point, there is no newer package and no network, and the tests that will "
        "grade the fix are not here. Searching for them only spends the budget. Work from the issue "
        "text and the code: reproduce the problem, fix it, verify the fix.")

_GIT = r"\bgit\s+(?:-C\s+\S+\s+)?"
# A compiled-bytecode path, as an argument: `x.pyc`, `__pycache__/x.pyc`. Not
# `*.pyc` in a `-name` or `--exclude` (no name before the dot), so a cleanup
# or a search that skips the cache is left alone -- only READING one is a hunt.
_PYC = r"(?:\.pyc\b|__pycache__/)"

_HUNTS = (
    (re.compile(rf"{_GIT}cat-file\b[^;&|]*--batch-all-objects"), "scanning git's whole object store"),
    (re.compile(rf"{_GIT}fsck\b"), "looking for unreachable git objects"),
    (re.compile(rf"{_GIT}(?:log|rev-list|show-branch|shortlog)\b[^;&|]*\s--(?:all|branches|tags|remotes|reflog)\b"),
     "searching other branches and tags for later commits"),
    # `git branch -a`, `-r`, `--all`, `--remotes`, and any flag bundle with a
    # or r in it (`-avv`); a local `git branch` or `-vv` is fine.
    (re.compile(rf"{_GIT}branch\b[^;&|]*\s-(?:-all|-remotes|[a-zA-Z]*[ar][a-zA-Z]*)(?=\s|$)"),
     "listing other branches"),
    (re.compile(rf"{_GIT}(?:tag|for-each-ref|ls-remote)(?=\s|$)"), "looking for later tags or refs"),
    (re.compile(r"(?:^|[\s;&|(])gh\s+\w"), "asking GitHub"),
    (re.compile(r"\b(?:pip3?|python3?\s+-m\s+pip|conda|mamba)\s+(?:download|install)\b"), "fetching a package"),
    (re.compile(r"\b(?:curl|wget)\b"), "fetching from the network"),
    (re.compile(r"\b(?:find|grep|rg|locate|du|ls\s+-R)\b[^;&|]*\s/(?=\s|$|\*)"), "searching the whole filesystem"),
    (re.compile(r"(?:^|[\s\"'=])(?:~|\$HOME|/root)/\.cache\b|\.cache/pip\b|\s(?:/opt/miniconda3/pkgs|/tmp/tektonix)"),
     "searching caches outside the repository"),
    # A reader with a .pyc argument in the same pipeline stage: `strings
    # x.pyc`, `xxd __pycache__/x.pyc`, `python -m dis x.pyc`. The argument may
    # not start with `-`, so `grep -r x --exclude-dir=__pycache__ .` is not it.
    (re.compile(rf"\b(?:strings|xxd|hexdump|od|cat|zcat|less|more|head|tail|grep|python[\d.]*)\b[^|;&]*?\s(?!-)[^\s|;&=]*{_PYC}"),
     "reading compiled bytecode"),
    # Decompiling it from Python: marshal/dis with a .pyc anywhere in the
    # command (the path is usually inside an open() string).
    (re.compile(rf"(?:\bmarshal\b|\bdis\.\w+|\bimport\s+dis\b|\buncompyle6?\b|\bdecompyle3?\b|\bpycdc\b).*?{_PYC}"
                rf"|{_PYC}.*?(?:\bmarshal\b|\bdis\.\w+)", re.S),
     "decompiling stale bytecode"),
)

# Where one segment ends and the next begins, outside quotes. A newline is a
# separator too: the model writes multi-line commands as one call.
_SEPS = ("&&", "||", ";", "\n")
# Shell syntax that the segment splitter cannot see through -- a heredoc body,
# a subshell, a loop, a continuation line. Dropping one segment out of those
# would hand the shell a broken command, so such a command is judged whole.
_UNSPLITTABLE = re.compile(r"<<|[(){}]|\\\n|\b(?:do|done|then|fi|case|esac|while|until|for|if)\b")
# A segment that only sets the working directory for the ones after it.
_BARE_CD = re.compile(r"^\s*cd\s+\S+\s*$")


def hunt_reason(command: str) -> str | None:
    """Why this command is a search for the answer, or None.

    Judged on the whole text: any hunt anywhere. `screen` is the finer view.
    """
    for rx, why in _HUNTS:
        if rx.search(command or ""):
            return why
    return None


def refusal(command: str) -> str | None:
    """The whole-command refusal, for a command that hunts and nothing else."""
    why = hunt_reason(command)
    return f"{BENCHMARK_REFUSED_PREFIX} on a benchmark task ({why}). {_WHY}" if why else None


def _partial(refused: list[tuple[str, str]]) -> str:
    named = "; ".join(f"`{seg}` ({why})" for seg, why in refused)
    return (f"{BENCHMARK_REFUSED_PREFIX} part of that command on a benchmark task: {named}. {_WHY} "
            f"The rest of the command ran; its result follows.")


def screen(command: str) -> tuple[str | None, str | None]:
    """What of `command` may run, and what to tell the model about the rest.

    Returns (remaining_command, note). Nothing hunts: (command, None). Every
    segment hunts: (None, the refusal). Some do: (the other segments joined
    back with their own separators, a note naming the refused ones and why).
    """
    text = command or ""
    if not hunt_reason(text):
        return text, None
    if _UNSPLITTABLE.search(text):
        return None, refusal(text)
    kept: list[str] = []
    refused: list[tuple[str, str]] = []
    prev_sep = ""
    for seg, sep in split_top_level(text, _SEPS, keep_seps=True):
        if seg.strip():
            why = hunt_reason(seg)
            if why:
                refused.append((seg.strip(), why))
            else:
                # Joined by the separator that led into it originally, so
                # `A && B && C` minus B is `A && C`.
                if kept:
                    kept.append(prev_sep)
                kept.append(seg)
        prev_sep = sep
    # `cd /workspace && git fsck` minus the hunt is a bare `cd`: nothing
    # left worth a container, so that is a whole refusal too.
    if not any(not _BARE_CD.match(seg) for seg in kept[::2]):
        return None, refusal(text)
    return "".join(kept).strip(), _partial(refused)
