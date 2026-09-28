#!/usr/bin/env bash
# scripts/deploy-poll.sh against a real git pair, with pm2 and npm stubbed:
# it pulls only what is a fast-forward, keeps a running app's own file even
# when the tree is dirty with it, refuses when main also changed that file,
# throws away a regenerated lockfile, and builds and restarts on a change.
set -uo pipefail
cd "$(dirname "$0")/.."
SCRIPT="$PWD/scripts/deploy-poll.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT
export HOME="$T/home"; mkdir -p "$HOME"
git config --global user.email t@t; git config --global user.name t; git config --global init.defaultBranch main

# Stubs on PATH: each call is recorded.
mkdir -p "$T/bin"
printf '#!/bin/sh\necho "pm2 $*" >> "%s/calls"\n' "$T" > "$T/bin/pm2"
printf '#!/bin/sh\necho "npm $*" >> "%s/calls"\n' "$T" > "$T/bin/npm"
chmod +x "$T/bin/pm2" "$T/bin/npm"
export PATH="$T/bin:$PATH"

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "  ok   $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# An origin and a live checkout, with a build script and a runtime file.
git init -q --bare "$T/origin.git"
git clone -q "$T/origin.git" "$T/work"
( cd "$T/work" && printf '{"scripts":{"build":"true"}}\n' > package.json && echo '{"lock":1}' > package-lock.json \
  && mkdir -p config && echo '{"live":"state-v1"}' > config/pairs.json && echo 'v1' > src.txt \
  && git add -A && git commit -qm one && git push -q origin main )
git clone -q "$T/origin.git" "$T/live"; mkdir -p "$T/live/data"
export PROJECT_DIR="$T/live" PM2_APPS="app app-worker" KEEP_DIRTY="config/pairs.json"

echo "deploy-poll"
out="$("$SCRIPT" 2>&1)"; rc=$?
check "nothing to do when main is unchanged" '[[ $rc == 0 && "$out" == *"up to date"* && ! -e "$T/calls" ]]'

# The app rewrites its file; a commit that does not touch it deploys around it.
echo '{"live":"state-v2-runtime"}' > "$T/live/config/pairs.json"
( cd "$T/work" && echo 'v2' > src.txt && git commit -qam two && git push -q origin main )
out="$("$SCRIPT" 2>&1)"; rc=$?
check "a change on main is pulled, built and restarted" '[[ $rc == 0 && "$(cat "$T/live/src.txt")" == v2 ]]'
check "both pm2 apps are restarted with --update-env" 'grep -q "^pm2 restart app app-worker --update-env" "$T/calls"'
check "no dependency install when package files did not change" '! grep -q "^npm install" "$T/calls"'
check "the running app's own file keeps its runtime content" '[[ "$(cat "$T/live/config/pairs.json")" == *state-v2-runtime* ]]'
check "the log records the deploy" 'grep -q "deployed" "$T/live/data/auto-deploy.log"'

# main also changed the runtime file: refuse, pull nothing.
( cd "$T/work" && echo '{"live":"state-v3-code"}' > config/pairs.json && echo 'v3' > src.txt && git commit -qam three && git push -q origin main )
out="$("$SCRIPT" 2>&1)"; rc=$?
check "a commit touching the app's own dirty file is refused" '[[ $rc == 1 && "$out" == *"merge that by hand"* ]]'
check "and nothing was pulled" '[[ "$(cat "$T/live/src.txt")" == v2 ]]'
( cd "$T/live" && git checkout -q -- config/pairs.json )   # the operator merged it by hand

# A regenerated lockfile is thrown away when main brings a new one, and deps install.
echo '{"lock":"regenerated-locally"}' > "$T/live/package-lock.json"
( cd "$T/work" && echo '{"lock":2}' > package-lock.json && git commit -qam four && git push -q origin main )
: > "$T/calls"
out="$("$SCRIPT" 2>&1)"; rc=$?
check "a regenerated lockfile is reset and main's taken" '[[ $rc == 0 && "$(cat "$T/live/package-lock.json")" == *"\"lock\":2"* ]]'
check "dependencies are installed when package files changed" 'grep -q "^npm install" "$T/calls"'
check "the build ran" 'grep -q "^npm run build" "$T/calls"'

# Any other local edit that main also changed stops the deploy.
echo 'local edit' > "$T/live/src.txt"
( cd "$T/work" && echo 'v5' > src.txt && git commit -qam five && git push -q origin main )
out="$("$SCRIPT" 2>&1)"; rc=$?
check "an operator's uncommitted edit on a file main changed is refused" '[[ $rc == 1 && "$out" == *"commit or discard"* ]]'

echo "$pass passed, $fail failed"
[[ $fail == 0 ]]
