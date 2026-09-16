#!/bin/bash
# Cron wrapper for the service health watchdog (scripts/health_watchdog.py).
#
# Every minute. The script itself decides whether anything is wrong and whether
# a restart is the right answer -- see its docstring for why readiness failures
# deliberately do NOT restart.
#
# Output goes to a log rather than cron's mailer, but the exit code is
# preserved so a broken watchdog is still detectable as a failing cron job
# (the same reasoning as consolidation-cron.sh: a run that always exits 0 is
# indistinguishable from a healthy one).
AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOG="${AGENT_LOG_DIR:-$AGENT_HOME/data}/health_watchdog.log"
mkdir -p "$(dirname "$LOG")"
cd "$AGENT_HOME" || exit 1
# Only successful-but-silent runs are dropped; anything it prints is an action
# it took or a failure to take one, and both belong in the log.
OUT=$(.venv/bin/python scripts/health_watchdog.py 2>&1); RC=$?
[ -n "$OUT" ] && printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$OUT" >> "$LOG"
exit $RC
