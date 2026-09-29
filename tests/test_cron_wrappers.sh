#!/usr/bin/env bash
# The two cron wrappers on a fresh tree: the log directory did not exist
# yet, and `>> "$LOG"` into it failed before the run started, so the first
# scheduled run on a new host wrote no log and no marker. Now the marker
# records the run whatever happened to it, and a failed run says so.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "  ok   $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

run_wrapper() {   # wrapper marker-name runner-exit
    local home="$T/$1-$3"
    mkdir -p "$home/scripts" "$home/.venv/bin"
    cp "$ROOT/scripts/$1-cron.sh" "$home/scripts/"
    # The runner itself is stubbed: it is the wrapper's bookkeeping under test.
    printf '#!/bin/sh\necho "runner ran"\nexit %s\n' "$3" > "$home/.venv/bin/python"
    chmod +x "$home/.venv/bin/python"
    AGENT_HOME="$home" bash "$home/scripts/$1-cron.sh" 2>"$T/err"
    echo "$?" > "$T/rc"
    echo "$home"
}

for pair in consolidation:last_consolidation.json cartographer:last_cartography.json; do
    wrapper="${pair%%:*}"; marker="${pair##*:}"
    echo "$wrapper-cron.sh"
    home="$(run_wrapper "$wrapper" "$marker" 0)"
    check "a fresh tree gets its data/ directory and the log" '[[ -s "$home/data/$wrapper.log" ]] && grep -q "runner ran" "$home/data/$wrapper.log"'
    check "the marker records a successful run" 'grep -q "\"ok\":true" "$home/data/$marker" && [[ "$(cat "$T/rc")" == 0 ]]'
    home="$(run_wrapper "$wrapper" "$marker" 3)"
    check "a failed run keeps its exit code and says so" '[[ "$(cat "$T/rc")" == 3 ]] && grep -q "\"exit_code\":3" "$home/data/$marker" && grep -q FAILED "$T/err"'
done

echo "$pass passed, $fail failed"
[[ $fail == 0 ]]
