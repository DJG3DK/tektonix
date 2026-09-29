#!/bin/bash
# Cron wrapper for nightly memory consolidation. OPTIONAL since 2026-09-28:
# the agent schedules this job itself (agent/jobs.py), once a day at the
# first quiet moment, and the memory panel has a Run now button. A host
# install may keep this cron line; both write the same marker, and a run
# from either side means the other is simply not due again for a day.
#
# The bare cron line appended to a log and exited 0 regardless, so a broken run
# was indistinguishable from a healthy one -- which is how a provider
# incompatibility silently skipped consolidation for months. This keeps the log
# but also leaves a failure marker the dashboard/health check can see, and
# preserves the non-zero exit so cron's own mailer has something to report.
# Resolve the installation from this script's own location, so the same file
# works wherever the repo is checked out (AGENT_HOME overrides).
AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="${AGENT_LOG_DIR:-$AGENT_HOME/data}/consolidation.log"
MARK="$AGENT_HOME/data/last_consolidation.json"

cd "$AGENT_HOME" || exit 1

# One run at a time, across this wrapper AND the agent's in-process schedule
# (agent/jobs.py takes the same flock): the marker below is stamped only when
# a run finishes, so while one ran the agent saw the job as still due and
# started a second on the same store (2026-09-29).
# The log directory too, before the redirect below: on a fresh tree data/
# does not exist yet, and `>> "$LOG"` into a missing directory fails first.
mkdir -p "$AGENT_HOME/data" "$(dirname "$LOG")"
exec 9>"$AGENT_HOME/data/consolidation.lock"
if ! flock -n 9; then
    echo "[consolidation] already running (data/consolidation.lock is held); skipping" >&2
    exit 0
fi
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{ echo "=== $START ==="; .venv/bin/python scripts/run_consolidation.py; } >> "$LOG" 2>&1
RC=$?

printf '{"ran_at":"%s","exit_code":%d,"ok":%s}\n' \
    "$START" "$RC" "$([ $RC -eq 0 ] && echo true || echo false)" > "$MARK"

if [ $RC -ne 0 ]; then
    echo "[consolidation] FAILED at $START (exit $RC) — see $LOG" >&2
fi
exit $RC
