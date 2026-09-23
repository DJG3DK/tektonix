"""One task per project on an installation that has no database to ask.

The server holds a project with a Postgres session-level advisory lock: a
claim every process pointed at that database can see, dropped by the kernel
when the holding connection dies. A local installation has no database
server, so the equivalent here is an OS file lock beside the state file.

flock, specifically, and not an O_CREAT|O_EXCL lock file: the kernel drops
an flock when the fd closes OR the process dies, which is the one property
that made the advisory lock better than the in-process asyncio.Lock it
replaced. A lock file has to be deleted by its holder, so a SIGKILL leaves a
file that claims a project forever and needs a stale-PID heuristic to
clean up -- and a stale-PID heuristic is a guess about another process.

WHAT THIS DOES NOT PROTECT AGAINST. It is a smaller claim than the Postgres
one, and the difference is worth stating rather than discovering:

* **It keys on a path, not on a name.** `pg_try_advisory_lock(0x3D46, key)`
  is global to a database: every process pointed at that Postgres contends
  for one claim on a project, wherever it runs from. This lock is global
  only to a STATE DIRECTORY -- the `locks/` directory beside the sqlite file
  named by the DSN. Two checkouts of one repository with two state
  directories are two independent locks and both will happily run against
  the same worktree. "Same state dir, same lock" is the honest unit. It is
  the right unit for one person with one checkout, and it is not a
  replacement for the server's.
* **Network filesystems.** flock over NFSv3 and over SMB is unreliable and
  in some configurations a silent no-op -- a lock that appears to work and
  protects nothing, which is exactly the failure the in-process lock had.
  The state directory belongs on local disk.
* **Anything that does not take the lock.** An operator running a script
  against the worktree directly is unaffected. The Postgres lock has the
  same hole.
* **Anyone deleting the lock file out from under a running task.** The
  lock is on an INODE, so a caller arriving after `rm locks/*.lock` creates
  a fresh file and takes an uncontended lock on it -- two processes, one
  project, no warning. `hold` re-checks the inode after acquiring and goes
  round again when it has been replaced, which closes the window between
  two callers but cannot help the task that is already holding the old
  inode. Do not tidy this directory while anything is running; nothing here
  ever needs cleaning up, which is why flock was chosen.
* **It does not serialise SQLite writes.** SQLite does that itself, with WAL
  and busy_timeout. This guards the WORKTREE, which is what the lock has
  always been for.
* **POSIX only.** There is no Windows half here; see `hold` for why the
  half-written one was left out rather than shipped unverified.

The lock file is created empty, never deleted, and carries the holder's pid
as text for a human reading it. Nothing decides anything from its contents:
the moment a lock is a file whose BODY has to be interpreted, a crashed
holder becomes a parsing problem again.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from agent.backends import sqlite_path_from_dsn

logger = logging.getLogger("tektonix")

try:
    import fcntl
except ImportError:  # pragma: no cover -- exercised only off POSIX
    fcntl = None  # type: ignore[assignment]

# Matches the Postgres branch's warning threshold so a long wait reads the
# same in the log whichever backend produced it.
_LOCK_WAIT_WARN_S = 5.0

# How often the wait re-tries the non-blocking flock. Short enough that a
# handover is not noticeable against a task that runs for minutes, long
# enough that a queued task costs nothing measurable while it waits.
_POLL_S = 0.15

# Everything that is not safe in a filename, collapsed. The project name is
# for the human who runs `ls`; the hashed key below is what actually makes
# the name unique, so flattening a character here cannot collide two
# projects into one lock.
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME = 64


def lock_dir(dsn: str) -> Path:
    """The directory the locks live in: `locks/` beside the database file.

    Beside the database rather than in a fixed place like /var/lock or the
    temp directory, because the state directory is what defines the unit
    this lock covers. Two state directories are meant to be two independent
    installations, and a lock in a shared location would silently make them
    contend -- the mirror image of the limitation above, and just as
    surprising.
    """
    return sqlite_path_from_dsn(dsn).parent / "locks"


def lock_path(dsn: str, repo: str, key: int) -> Path:
    """The lock file for one project.

    `key` is agent.graph.advisory_key(repo) -- the same blake2b digest the
    Postgres branch locks on -- so the two backends derive a project's
    identity from one function and a rename means a new lock on both.
    """
    name = _UNSAFE.sub("_", repo).strip("._-")[:_MAX_NAME] or "project"
    return lock_dir(dsn) / f"{name}-{key & 0xFFFFFFFF:08x}.lock"


def _try_acquire(fd: int) -> bool:
    """Take the lock if it is free, return False if someone holds it."""
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _is_current(fd: int, path: Path) -> bool:
    """Is the file we hold still the file at `path`?

    The lock lives on the inode. If anyone unlinked or replaced the lock
    file while we waited, the lock we just won guards a file nobody else
    will ever open, and the next caller takes an uncontended lock on the
    new one. Cheap to check, and the check is the only thing standing
    between a tidied `locks/` directory and two tasks in one worktree.
    """
    try:
        here = os.stat(path)
    except OSError:
        return False
    ours = os.fstat(fd)
    return (here.st_ino, here.st_dev) == (ours.st_ino, ours.st_dev)


def _note_holder(fd: int, repo: str) -> None:
    """Leave the pid in the file for whoever is wondering who has it.

    Diagnostics only, and best-effort: a failure to write this must not fail
    a lock we already hold.
    """
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {repo}\n".encode())
    except OSError:
        pass


@asynccontextmanager
async def hold(dsn: str, repo: str, key: int):
    """Hold `repo` for the duration of the block, or wait for whoever has it.

    The wait POLLS the non-blocking flock rather than parking a thread on
    the blocking one, and the reason is an incident rather than a
    preference: LOCK_EX is an uninterruptible syscall, so a cancelled wait
    (a ctrl-c, a task torn down) left a worker thread of the default
    executor blocked in it forever, and asyncio.run() then hung in
    loop.shutdown_default_executor() joining that thread -- the process
    could never exit. A private executor would not have helped; since 3.9
    its threads are non-daemon and joined at interpreter exit too.

    Polling is fully cancellable, parks nothing, and keeps the property
    that chose flock in the first place: the kernel drops the lock when the
    fd closes or the process dies, so a SIGKILLed holder releases instantly
    and there is never a stale lock to clean up.

    Windows is deliberately absent rather than half-present. msvcrt.locking
    is a byte-range lock whose blocking mode gives up after roughly ten
    seconds instead of waiting, and an untested lock that returns instead of
    waiting hands two processes the same worktree, which is worse than a
    clear refusal.
    """
    if fcntl is None:
        raise NotImplementedError(
            f"holding a project with a file lock needs POSIX flock, which {sys.platform} does "
            "not have -- a local installation on this platform cannot guarantee one task per "
            "project yet; run against Postgres, where the lock is an advisory lock"
        )
    path = lock_path(dsn, repo, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = await _wait_for_lock(path, repo)
    try:
        _note_holder(fd, repo)
        yield
    finally:
        # Closing the fd is what releases the flock. The file itself stays:
        # unlinking it would race a process that has already opened it and
        # is waiting, which would give two callers two different inodes and
        # therefore two different locks.
        os.close(fd)


async def _wait_for_lock(path: Path, repo: str) -> int:
    """An open fd holding the flock on `path`. Cancellable at every await."""
    started = time.monotonic()
    warned = False
    while True:
        # O_CLOEXEC: a task shells out constantly (git, npm, the sandbox),
        # and a child inheriting this fd would keep the project locked for
        # as long as it lived, which for a daemonised child is forever.
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            while not _try_acquire(fd):
                if not warned:
                    # Say so, for the same reason the Postgres branch does:
                    # a task that simply sits there is otherwise
                    # unexplainable.
                    logger.warning(
                        "project %s is locked by another process; waiting for it to finish "
                        "(lock file %s)", repo, path)
                    warned = True
                await asyncio.sleep(_POLL_S)
            if _is_current(fd, path):
                waited = time.monotonic() - started
                if waited > _LOCK_WAIT_WARN_S:
                    logger.warning("project %s acquired after waiting %.0fs", repo, waited)
                return fd
        except BaseException:
            os.close(fd)
            raise
        # The file was replaced while we waited -- see _is_current. Drop
        # this lock, which guards an inode nobody can reach any more, and
        # contend for the file that is there now.
        os.close(fd)


def try_acquire(dsn: str, repo: str, key: int) -> int | None:
    """Take the lock only if it is free right now: an fd holding it, or None.

    The slot lock (agent.graph.project_slot) asks this of each of a project's
    N slots in turn, and must never wait on any one of them -- a slot that
    frees up while it waits on another is a task needlessly queued. Release
    with release()."""
    if fcntl is None:
        raise RuntimeError(f"a file lock needs POSIX flock, which {sys.platform} does not have")
    path = lock_path(dsn, repo, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    if _try_acquire(fd) and _is_current(fd, path):
        _note_holder(fd, repo)
        return fd
    os.close(fd)
    return None


def release(fd: int) -> None:
    os.close(fd)
