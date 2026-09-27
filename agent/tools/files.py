"""File read/write, scoped strictly under a repo root — no path traversal."""

from agent.harness_voice import HARNESS
import errno
import os
import re
import stat
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


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FDS = os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and bool(_NOFOLLOW)


def _swapped(rel_path: str) -> PathEscapeError:
    return PathEscapeError(
        f"{HARNESS} {rel_path!r} became a symbolic link while it was being opened -- "
        f"paths are opened without following links, so it was refused. Use the real path."
    )


def _open_contained(repo_root: str, rel_path: str, flags: int, *, mkdirs: bool = False) -> int:
    """An fd for `rel_path`, opened so that nothing can redirect it outside
    `repo_root` between the check and the open.

    _resolve alone is check-then-use: it resolves symlinks, confirms the
    result is inside the root, and the caller opens the path afterwards. The
    workspace is written concurrently by the agent's own sandboxed shell, so
    a directory swapped for a symlink in that window sent reads and writes to
    any file this process could reach -- measured at about one read in four
    returning a host secret under a tight swap loop.

    So the resolved path is walked one component at a time, each opened
    relative to its parent's fd with O_NOFOLLOW. _resolve has already
    followed every legitimate in-repo link, so the walk meets none; one that
    appears mid-walk is the race, and is refused.
    """
    target = _resolve(repo_root, rel_path)
    if not _DIR_FDS:
        return os.open(target, flags | _CLOEXEC, 0o666)
    root = Path(repo_root).resolve()
    parts = target.relative_to(root).parts
    if not parts:
        raise IsADirectoryError(f"{rel_path!r} is the repo root, not a file")

    def _refuse_if_link(err: OSError, name: str, dirfd: int) -> None:
        if err.errno in (errno.ELOOP, errno.ENOTDIR, errno.EMLINK):
            try:
                if stat.S_ISLNK(os.lstat(name, dir_fd=dirfd).st_mode):
                    raise _swapped(rel_path) from err
            except OSError:
                pass

    dirfd = os.open(root, os.O_RDONLY | _DIRECTORY | _CLOEXEC)
    try:
        for name in parts[:-1]:
            dir_flags = os.O_RDONLY | _DIRECTORY | _NOFOLLOW | _CLOEXEC
            try:
                nxt = os.open(name, dir_flags, dir_fd=dirfd)
            except FileNotFoundError:
                if not mkdirs:
                    raise
                try:
                    os.mkdir(name, 0o777, dir_fd=dirfd)
                except FileExistsError:
                    pass
                try:
                    nxt = os.open(name, dir_flags, dir_fd=dirfd)
                except OSError as e:
                    _refuse_if_link(e, name, dirfd)
                    raise
            except OSError as e:
                _refuse_if_link(e, name, dirfd)
                raise
            os.close(dirfd)
            dirfd = nxt
        try:
            return os.open(parts[-1], flags | _NOFOLLOW | _CLOEXEC, 0o666, dir_fd=dirfd)
        except OSError as e:
            _refuse_if_link(e, parts[-1], dirfd)
            raise
    finally:
        os.close(dirfd)


def read_bytes(repo_root: str, rel_path: str) -> bytes:
    """The bytes of a file inside `repo_root`, race-free (see _open_contained)."""
    # O_NONBLOCK so a FIFO planted in the workspace cannot hang the open;
    # it has no effect on the regular file this insists on below.
    fd = _open_contained(repo_root, rel_path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise IsADirectoryError(f"{rel_path!r} is not a regular file")
        return fh.read()


def _write_bytes(repo_root: str, rel_path: str, data: bytes) -> None:
    fd = _open_contained(repo_root, rel_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mkdirs=True)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def read_file(repo_root: str, rel_path: str, max_chars: int = 40_000) -> str:
    raw = read_bytes(repo_root, rel_path)
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
    _write_bytes(repo_root, rel_path, content.encode("utf-8"))


def str_replace(repo_root: str, rel_path: str, old: str, new: str) -> None:
    """Same contract as Claude Code's own Edit tool: old must be unique."""
    raw = read_bytes(repo_root, rel_path)
    if _looks_binary(raw):
        raise BinaryFileError(f"{rel_path!r} looks like a binary file -- it cannot be text-edited.")
    # No errors="replace" here, deliberately: a genuine decode failure on a
    # file that passed the binary sniff should abort loudly (caught as a
    # ValueError below), not silently write lossy replacement characters
    # back over whatever the original bytes actually were.
    text = raw.decode("utf-8")
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
    _write_bytes(repo_root, rel_path, text.replace(old, new, 1).encode("utf-8"))
