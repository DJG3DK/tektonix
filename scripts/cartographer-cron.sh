#!/usr/bin/env bash
# Scheduled codebase mapping. OPTIONAL since 2026-09-28: the agent schedules
# this job itself (agent/jobs.py), once a day at the first quiet moment,
# and after every merge. A host install may keep a cron line; both write
# the same marker. Cheap by design: each project's inventory is hashed and
# the model is only called when a repo's structure actually changed, so
# running this often costs nothing on quiet days.
# Resolve the installation from this script's own location, so the same file
# works wherever the repo is checked out (AGENT_HOME overrides).
AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="${AGENT_LOG_DIR:-$AGENT_HOME/data}/cartographer.log"
MARK="$AGENT_HOME/data/last_cartography.json"

cd "$AGENT_HOME" || exit 1

# One run at a time, across this wrapper AND the agent's in-process schedule
# (agent/jobs.py takes the same flock; see consolidation-cron.sh).
# The log directory too, before the redirect below: on a fresh tree data/
# does not exist yet, and `>> "$LOG"` into a missing directory fails first.
mkdir -p "$AGENT_HOME/data" "$(dirname "$LOG")"
exec 9>"$AGENT_HOME/data/cartography.lock"
if ! flock -n 9; then
    echo "[cartographer] already running (data/cartography.lock is held); skipping" >&2
    exit 0
fi
START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
{ echo "=== $START ==="; .venv/bin/python scripts/run_cartographer.py; } >> "$LOG" 2>&1
RC=$?

printf '{"ran_at":"%s","exit_code":%d,"ok":%s}\n' \
    "$START" "$RC" "$([ $RC -eq 0 ] && echo true || echo false)" > "$MARK"

# Same contract as the consolidation cron: fail loudly. A silent exit 0 on a
# broken run is exactly how the nightly consolidation stayed dead for months.
if [ $RC -ne 0 ]; then
    echo "[cartographer] FAILED at $START (exit $RC) — see $LOG" >&2
fi
exit $RC
