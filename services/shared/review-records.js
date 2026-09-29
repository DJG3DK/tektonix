'use strict';
/**
 * What both review services, and the agent's Python gate, agree a review
 * record means. One definition per concept: the copies drifted once
 * (2026-09-29, a harnessFailed without the verdict condition let a READY
 * record carrying a failing infrastructure check be re-asked about), and
 * tests/test_review_verdict_helpers.py feeds the same records to every
 * copy that remains.
 */

// The branch a task works on, and the review unit: agent/<task uuid>. A
// backup branch beside it (`agent/<id>-pre-rebase-backup`) is not a task,
// and reviewing it clobbered the real task's record (2026-09-16).
const TASK_BRANCH_RE = /^agent\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

// On every git call, as in agent/tools/git.py: the tree git runs in is
// agent-authored. A project using husky has core.hooksPath=.husky in its
// config, and .husky/* is tracked content an agent commit changes -- so a
// hook would run agent code as this service. fsmonitor is the same shape:
// config that names a program git runs on status/diff.
const GIT_SAFE = ['-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false'];

// A review worktree is named <project>-<sha prefix> (worktree.js).
const WORKTREE_NAME_RE = /^(.+)-[0-9a-f]{7,40}$/;

/** The project a review worktree's basename was made for, or the name itself. */
function projectOfWorktree(basename) {
  const m = WORKTREE_NAME_RE.exec(basename);
  return m ? m[1] : basename;
}

/**
 * A verdict that blocked only because a check could not RUN (a fault in the
 * harness: sandbox unreachable, image missing) says nothing about the
 * commit. Such a record does not make the commit "already reviewed": the
 * next request reviews it again, which is the only way a fixed harness ever
 * gets to judge it. 2026-09-29: a reviewer whose sandbox call timed out
 * left a record the gate then refused to re-ask about, forever.
 *
 * A READY record is never a harness failure, whatever its checks carry: the
 * gate acted on it. agent/tools/review_gate.py's harness_failed is this
 * function in Python.
 */
function harnessFailed(record) {
  const checks = (record && Array.isArray(record.checkResults)) ? record.checkResults : [];
  return Boolean(record) && record.verdict !== 'READY' && checks.some((c) => c && c.infrastructure && !c.ok);
}

/**
 * The record for one branch of a project's review state. Per branch since
 * 2026-09-23; a state written before that has the record at the top level,
 * and it is this branch's only when it names it.
 */
function branchRecord(projectState, branch) {
  if (!projectState || !branch) return null;
  const rec = projectState.branches && Object.hasOwn(projectState.branches, branch)
    ? projectState.branches[branch] : null;
  if (rec) return rec;
  if (projectState.branch === branch && projectState.lastReviewedSha) {
    const { branches, inProgress, ...legacy } = projectState;
    return legacy;
  }
  return null;
}

module.exports = { TASK_BRANCH_RE, GIT_SAFE, WORKTREE_NAME_RE, projectOfWorktree, harnessFailed, branchRecord };
