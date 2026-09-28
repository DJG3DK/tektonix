#!/usr/bin/env bash
# Pull a project's GitHub main and deploy it on this machine when it moved.
#
# For a project whose production runs here while its main is merged
# elsewhere (a pull request merged on GitHub, an agent on another machine).
# The agent deploys only its own merges; this notices everyone else's. Safe
# for cron: one instance at a time, nothing to do when main is unchanged.
#
#   PROJECT_DIR=/srv/app PM2_APPS="app app-worker" scripts/deploy-poll.sh
#
#   PROJECT_DIR   the live checkout (required)
#   PM2_APPS      pm2 process names to restart after a deploy, space-separated
#   BUILD_CMD     run after the pull and any install, e.g. "npm run build"
#                 (default: "npm run build" when package.json has a build script)
#   GIT_BRANCH    main
#   KEEP_DIRTY    tracked files the running app rewrites (space-separated),
#                 never reset; the deploy refuses if a commit touches one
#   RESET_DIRTY   tracked files regenerated on install (default:
#                 package-lock.json), reset when a commit touches them
#   LOG_FILE      default $PROJECT_DIR/data/auto-deploy.log, else logs/, else /tmp
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:?set PROJECT_DIR to the live checkout}"
BRANCH="${GIT_BRANCH:-main}"
PM2_APPS="${PM2_APPS:-}"
KEEP_DIRTY="${KEEP_DIRTY:-}"
RESET_DIRTY="${RESET_DIRTY:-package-lock.json}"
NAME="$(basename "$PROJECT_DIR")"
LOCK_FILE="/tmp/deploy-poll-${NAME}.lock"

if [[ -z "${LOG_FILE:-}" ]]; then
    if [[ -d "$PROJECT_DIR/data" ]]; then LOG_FILE="$PROJECT_DIR/data/auto-deploy.log"
    elif [[ -d "$PROJECT_DIR/logs" ]]; then LOG_FILE="$PROJECT_DIR/logs/auto-deploy.log"
    else LOG_FILE="/tmp/deploy-poll-${NAME}.log"; fi
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }
fail() { log "ERROR: $*"; exit 1; }

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    log "another deploy is running; skipped"
    exit 0
fi

[[ -d "$PROJECT_DIR/.git" ]] || fail "$PROJECT_DIR is not a git checkout"
cd "$PROJECT_DIR"

git fetch -q origin "$BRANCH" || fail "git fetch origin $BRANCH failed"
LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"
if [[ "$LOCAL" == "$REMOTE" ]]; then
    echo "up to date at ${LOCAL:0:12}"
    exit 0
fi

CHANGED="$(git diff --name-only "$LOCAL" "$REMOTE")"
log "main moved ${LOCAL:0:12} -> ${REMOTE:0:12}; $(echo "$CHANGED" | wc -l) file(s):"
echo "$CHANGED" | sed 's/^/    /' | tee -a "$LOG_FILE"

# Local edits: a file the running app rewrites must survive a deploy, and a
# commit that also changes it is a conflict between state and code that a
# script must not resolve. A regenerated file is thrown away when a commit
# brings a new one. Anything else dirty is an operator's work in progress.
DIRTY="$(git status --porcelain --untracked-files=no | awk '{print $2}')"
for f in $DIRTY; do
    touched=0; echo "$CHANGED" | grep -qxF "$f" && touched=1
    if echo " $KEEP_DIRTY " | grep -qF " $f "; then
        [[ $touched == 1 ]] && fail "$f is rewritten by the running app AND changed on main; merge that by hand"
    elif echo " $RESET_DIRTY " | grep -qF " $f "; then
        [[ $touched == 1 ]] && { log "resetting regenerated $f to take main's"; git checkout -q -- "$f"; }
    else
        [[ $touched == 1 ]] && fail "$f has local edits and changed on main; commit or discard them first"
    fi
done

git pull -q --ff-only origin "$BRANCH" || fail "git pull --ff-only failed; main is not a fast-forward of this checkout"

if echo "$CHANGED" | grep -qE '(^|/)package(-lock)?\.json$'; then
    log "installing dependencies"
    npm install --no-audit --no-fund >>"$LOG_FILE" 2>&1 || fail "npm install failed; see $LOG_FILE"
fi

BUILD="${BUILD_CMD-}"
if [[ -z "${BUILD_CMD+x}" ]] && [[ -f package.json ]] && grep -q '"build"' package.json; then
    BUILD="npm run build"
fi
if [[ -n "$BUILD" ]]; then
    log "building: $BUILD"
    bash -c "$BUILD" >>"$LOG_FILE" 2>&1 || fail "build failed; see $LOG_FILE (the checkout is at ${REMOTE:0:12}, the app still runs the old build)"
fi

if [[ -n "$PM2_APPS" ]]; then
    log "restarting: $PM2_APPS"
    # shellcheck disable=SC2086
    pm2 restart $PM2_APPS --update-env >>"$LOG_FILE" 2>&1 || fail "pm2 restart failed; see $LOG_FILE"
fi

log "deployed ${REMOTE:0:12}"
