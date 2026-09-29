"""The final commit's message, from the task's goal.

A goal is whatever the operator typed or pasted: one line, or a whole plan
document with a markdown heading. 2026-09-28: a pasted plan became the commit
message verbatim, subject line "# Plan: ...", body the entire document. A
commit wants a one-line subject and a short body; the full text lives in the
task record.
"""
from __future__ import annotations

import re

SUBJECT_MAX = 72
# A short goal is quoted whole under the subject; a long one (a pasted plan)
# contributes only its first paragraph.
_SHORT_LINES = 8
_SHORT_CHARS = 600

_LABEL = re.compile(r"^(?:plan|task|goal|title)\s*:\s*", re.IGNORECASE)


def _clean_line(line: str) -> str:
    line = line.strip()
    line = re.sub(r"^#+\s*", "", line)            # markdown heading
    line = line.strip("*_` ").strip()             # emphasis around the line
    line = _LABEL.sub("", line).strip("*_` ")     # "**Task:** ..." leaves its close
    return re.sub(r"\s+", " ", line).strip()


def subject(goal: str) -> str:
    """One line, at most SUBJECT_MAX characters, cut at a word."""
    for raw in (goal or "").splitlines():
        line = _clean_line(raw)
        if line:
            break
    else:
        return "Task change"
    if len(line) <= SUBJECT_MAX:
        return line
    cut = line[:SUBJECT_MAX].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return (cut or line[:SUBJECT_MAX]).rstrip() + "..."


def body(goal: str) -> str:
    """What follows the subject: the rest of a short goal, or the first
    paragraph of a long one. Empty when the subject already says it all."""
    lines = (goal or "").splitlines()
    while lines and not _clean_line(lines[0]):
        lines.pop(0)
    rest = lines[1:] if lines else []
    text = "\n".join(rest).strip()
    if not text:
        return ""
    if len(rest) <= _SHORT_LINES and len(text) <= _SHORT_CHARS:
        return text
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    first = paragraphs[0] if paragraphs else ""
    # A heading as the first paragraph says nothing on its own.
    if first.startswith("#") and len(paragraphs) > 1:
        first = paragraphs[1]
    first = re.sub(r"\s+", " ", first)
    if len(first) > _SHORT_CHARS:
        first = first[:_SHORT_CHARS].rsplit(" ", 1)[0] + "..."
    return first + "\n\nThe full task text is in the task record."


def message(goal: str) -> str:
    """Subject, a blank line and the body when there is one."""
    b = body(goal)
    return subject(goal) + ("\n\n" + b if b else "")
