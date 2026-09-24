"""File read/write, scoped strictly under a repo root — no path traversal."""

from agent.harness_voice import HARNESS
import re
from pathlib import Path


class PathEscapeError(Exception):
    pass


class BinaryFileError(Exception):
    pass


def _looks_binary(raw: bytes, sniff_bytes: int = 8192) -> bool:
    """Same heuristic git itself uses to classify a file as binary: a NUL
    byte anywhere in the first chunk essentially never appears in real text
    (source, config, docs) but appears immediately in nearly every binary
    format (images, archives, compiled output). Cheap and reliable -- no
    need to guess from the file extension.
    """
    return b"\x00" in raw[:sniff_bytes]


def _resolve(repo_root: str, rel_path: str) -> Path:
    root = Path(repo_root).resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        # Do NOT name repo_root here: this message reaches the LLM, and the
        # host-side sandbox path both leaks infrastructure detail and
        # directly contradicts the "/workspace is the repo root" story the
        # agent is (correctly) told everywhere else. Say what to do instead.
        raise PathEscapeError(
            f"{HARNESS} {rel_path!r} is not a valid repo path -- paths must be RELATIVE to the repo root "
            f"(e.g. \"src/App.tsx\"), and must stay inside it (no '..' escapes, no absolute host paths)."
        )
    return target


def read_file(repo_root: str, rel_path: str, max_chars: int = 40_000) -> str:
    path = _resolve(repo_root, rel_path)
    raw = path.read_bytes()
    if _looks_binary(raw):
        # Decoding a binary file with errors="replace" doesn't raise -- it
        # silently produces a same-length wall of garbage text, which then
        # sails straight through the size caps below (a real image's raw
        # bytes routinely land under even a generous inline-text threshold,
        # since those thresholds are tuned for legitimate large source
        # files, not binary content at all). That garbage then sits in the
        # conversation forever, making every subsequent call in the same
        # thread more expensive as it gets resent. Refuse outright instead.
        raise BinaryFileError(
            f"{rel_path!r} looks like a binary file, not text -- reading it here would dump raw "
            f"bytes into your context, not anything useful. For an image, use the describe_image "
            f"tool instead."
        )
    text = raw.decode("utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... [truncated, {len(text) - max_chars} more chars]"
    return text


def write_file(repo_root: str, rel_path: str, content: str) -> None:
    path = _resolve(repo_root, rel_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def str_replace(repo_root: str, rel_path: str, old: str, new: str) -> None:
    """Same contract as Claude Code's own Edit tool: old must be unique."""
    path = _resolve(repo_root, rel_path)
    if _looks_binary(path.read_bytes()):
        raise BinaryFileError(f"{rel_path!r} looks like a binary file -- it cannot be text-edited.")
    # No errors="replace" here, deliberately: a genuine decode failure on a
    # file that passed the binary sniff should abort loudly (caught as a
    # ValueError below), not silently write lossy replacement characters
    # back over whatever the original bytes actually were.
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count == 0:
        # A model can guess old_string's indentation wrong (e.g. matching a
        # nearby but different block) for a large file it only partially
        # paged through, and then retry the identical string instead of
        # re-checking. Whitespace-insensitive comparison catches the common
        # real cause (wrong indent width/tabs-vs-spaces) without ever
        # writing on a fuzzy match -- it only upgrades the diagnosis, never
        # the action taken.
        normalized_old = re.sub(r"[ \t]+", " ", old)
        normalized_text = re.sub(r"[ \t]+", " ", text)
        if normalized_old in normalized_text:
            raise ValueError(
                f"old_string not found verbatim in {rel_path}, but a whitespace-insensitive match "
                f"exists -- this is almost always wrong indentation (tabs vs spaces, or a different "
                f"indent width than you guessed). Re-read the exact target lines (e.g. `bash cat -A "
                f"<path>` to see whitespace explicitly, or `read`/`read_file` with the right offset) "
                f"before retrying -- resubmitting the same string will fail the same way."
            )
        raise ValueError(
            f"old_string not found in {rel_path}, not even ignoring whitespace -- you may be working "
            f"from a stale or incomplete read (e.g. only the head/tail preview of an offloaded file). "
            f"Re-read the exact target section first, don't guess its contents."
        )
    if count > 1:
        raise ValueError(f"old_string is not unique in {rel_path} ({count} occurrences)")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
