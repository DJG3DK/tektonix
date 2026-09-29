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
( cd "$T/live" && git checkout -q -- src.txt )

# A build that fails leaves the checkout ahead of the running app. The next
# run must retry the deploy, not report "up to date" because HEAD == origin.
( cd "$T/work" && echo 'v6' > src.txt && git commit -qam six && git push -q origin main )
printf '#!/bin/sh\nif [ -e "%s/fail-once" ]; then rm "%s/fail-once"; echo "boom"; exit 1; fi\necho "build $*" >> "%s/calls"\n' "$T" "$T" "$T" > "$T/build.sh"
chmod +x "$T/build.sh"
touch "$T/fail-once"; : > "$T/calls"
out="$(BUILD_CMD="$T/build.sh" "$SCRIPT" 2>&1)"; rc=$?
check "a failing build stops the deploy and says it will be retried" '[[ $rc == 1 && "$out" == *"build failed"* && "$out" == *"retried"* ]]'
check "nothing was restarted onto the failed build" '! grep -q "^pm2 restart" "$T/calls"'
check "the pull itself happened" '[[ "$(cat "$T/live/src.txt")" == v6 ]]'
out="$(BUILD_CMD="$T/build.sh" "$SCRIPT" 2>&1)"; rc=$?
check "the next run retries the deploy instead of saying up to date" '[[ $rc == 0 && "$out" == *"retrying the deploy"* && "$out" != *"up to date"* ]]'
check "the retried build ran and the apps were restarted" 'grep -q "^build" "$T/calls" && grep -q "^pm2 restart" "$T/calls"'
out="$(BUILD_CMD="$T/build.sh" "$SCRIPT" 2>&1)"; rc=$?
check "and after the retry succeeds it is up to date" '[[ $rc == 0 && "$out" == *"up to date"* ]]'

# A tracked path with a space in it is one path, dirty or changed.
( cd "$T/work" && mkdir -p docs && echo 'n1' > "docs/my notes.txt" && git add -A && git commit -qm seven && git push -q origin main )
BUILD_CMD="$T/build.sh" "$SCRIPT" >/dev/null 2>&1
echo 'local' > "$T/live/docs/my notes.txt"
( cd "$T/work" && echo 'n2' > "docs/my notes.txt" && git commit -qam eight && git push -q origin main )
out="$(BUILD_CMD="$T/build.sh" "$SCRIPT" 2>&1)"; rc=$?
check "a dirty path with a space that main changed is refused by name" '[[ $rc == 1 && "$out" == *"docs/my notes.txt has local edits"* ]]'

echo "$pass passed, $fail failed"
[[ $fail == 0 ]]
