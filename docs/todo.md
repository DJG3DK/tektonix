# Todo

Work that is agreed but not built. One entry per item: what breaks, what
already exists, and the shape of the fix. `docs/roadmap-packaging.md` holds the
packaging/Windows/CLI milestones; this is everything else.

---

## Keep a task moving when live main moves under it

**Status:** not started. Raised 2026-09-18.

### What breaks

The merge is `git merge --ff-only` into the live repo
(`services/agent-review/server.js`). If main advances while a task is in
flight, the fast-forward is impossible and the merge fails outright — an
approved, reviewed branch with nowhere to land, and a task that stops.

The trigger is ordinary: the operator pushes to main, or another project's
merge lands, while an agent is mid-task.

### What already protects against this

Only the start of a task. `sync_workspace_to_base` (`agent/tools/git.py`) moves
an idle workspace to the live tip before the first edit, so a task no longer
branches from wherever the last one left HEAD. It refuses to act on a dirty
tree, so it cannot help once work is underway.

Nothing watches for drift *during* a task, which is the case left open.

### Shape of the fix

Detect at commit time, not at merge time. At commit the agent is still running
and has the context to resolve a conflict; at merge it is long gone.

1. **Detect.** Before committing in `verify_and_ship`, compare the live tip to
   the task branch's merge-base. Equal means nothing to do, which is the
   common case and must stay cheap.
2. **Rebase.** If main moved, rebase the task branch onto the new tip.
3. **Re-review only when the content changed.** A rebase changes every sha, and
   the reviewer discards review history when the prior sha is no longer an
   ancestor — so a naive rebase always costs a full re-review round. Compare
   the resulting tree against the pre-rebase tree; if identical, the review
   still describes the same change and can carry over.
4. **Conflicts go back to the agent**, as feedback, the same way review
   findings do. It has the task context. Escalate only if it cannot resolve
   them.
5. **Merge-time fallback.** If ff still fails, return a distinct reason
   (`diverged`) rather than a generic failure, and trigger a rebase round
   instead of stopping.

### Open questions

- Should a rebase mid-task be announced in the console? The diff a person
  approved would change underneath them.
- Whether to also detect drift periodically during long tasks, or only at
  commit. Only at commit is simpler and probably enough.
- `--ff-only` is deliberate: it guarantees what merged is exactly what was
  reviewed. Any fix has to preserve that, which is why this rebases the branch
  rather than relaxing the merge.
