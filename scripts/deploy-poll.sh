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
#   DEPLOYED_FILE the commit the running app was built from; written after a
#                 successful restart (default: beside LOG_FILE, .sha)
#
# "Deployed" means built and restarted, not pulled. The pull happens first,
# so after a failed install, build or restart the checkout is ahead of the
# running app; a poller that compared HEAD with origin then saw nothing to
# do and production stayed on the old build until someone looked. The
# commit that was last deployed is kept in DEPLOYED_FILE, and a tick whose
# origin is ahead of THAT retries the whole deploy, pull or no pull.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:?set PROJECT_DIR to the live checkout}"
BRANCH="${GIT_BRANCH:-main}"
PM2_APPS="${PM2_APPS:-}"
KEEP_DIRTY="${KEEP_DIRTY:-}"
RESET_DIRTY="${RESET_DIRTY:-package-lock.json}"
NAME="$(basename "$PROJECT_DIR")"
# The full path, not the basename: two projects called `app` in different
# directories shared one lock and skipped each other's deploys.
LOCK_FILE="/tmp/deploy-poll-${NAME}-$(printf '%s' "$PROJECT_DIR" | cksum | cut -d' ' -f1).lock"

if [[ -z "${LOG_FILE:-}" ]]; then
    if [[ -d "$PROJECT_DIR/data" ]]; then LOG_FILE="$PROJECT_DIR/data/auto-deploy.log"
    elif [[ -d "$PROJECT_DIR/logs" ]]; then LOG_FILE="$PROJECT_DIR/logs/auto-deploy.log"
    else LOG_FILE="/tmp/deploy-poll-${NAME}.log"; fi
fi
DEPLOYED_FILE="${DEPLOYED_FILE:-${LOG_FILE%.log}.sha}"

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
# The first run has no record: what is checked out is what runs.
DEPLOYED="$(cat "$DEPLOYED_FILE" 2>/dev/null || true)"
git cat-file -e "${DEPLOYED}^{commit}" 2>/dev/null || DEPLOYED="$LOCAL"
if [[ "$DEPLOYED" == "$REMOTE" ]]; then
    echo "up to date at ${REMOTE:0:12}"
    exit 0
fi

CHANGED="$(git diff --name-only "$DEPLOYED" "$REMOTE")"
if [[ "$LOCAL" == "$REMOTE" ]]; then
    log "retrying the deploy of ${REMOTE:0:12}: the checkout is there, the last deploy did not finish"
else
    log "main moved ${LOCAL:0:12} -> ${REMOTE:0:12}; $(echo "$CHANGED" | wc -l) file(s) since the deployed ${DEPLOYED:0:12}:"
    echo "$CHANGED" | sed 's/^/    /' | tee -a "$LOG_FILE"
fi

if [[ "$LOCAL" != "$REMOTE" ]]; then
    # Local edits: a file the running app rewrites must survive a deploy, and a
    # commit that also changes it is a conflict between state and code that a
    # script must not resolve. A regenerated file is thrown away when a commit
    # brings a new one. Anything else dirty is an operator's work in progress.
    # Measured against what the pull would bring, NUL-separated so a path with
    # a space or a rename is one path, not two.
    PULLING="$(git diff --name-only "$LOCAL" "$REMOTE")"
    while IFS= read -r -d '' f; do
        touched=0; echo "$PULLING" | grep -qxF -- "$f" && touched=1
        if echo " $KEEP_DIRTY " | grep -qF -- " $f "; then
            [[ $touched == 1 ]] && fail "$f is rewritten by the running app AND changed on main; merge that by hand"
        elif echo " $RESET_DIRTY " | grep -qF -- " $f "; then
            [[ $touched == 1 ]] && { log "resetting regenerated $f to take main's"; git checkout -q -- "$f"; }
        else
            [[ $touched == 1 ]] && fail "$f has local edits and changed on main; commit or discard them first"
        fi
    done < <(git diff --name-only -z HEAD)

    git pull -q --ff-only origin "$BRANCH" || fail "git pull --ff-only failed; main is not a fast-forward of this checkout"
fi

if echo "$CHANGED" | grep -qE '(^|/)package(-lock)?\.json$'; then
    log "installing dependencies"
    npm install --no-audit --no-fund >>"$LOG_FILE" 2>&1 || fail "npm install failed; see $LOG_FILE (retried next run)"
fi

BUILD="${BUILD_CMD-}"
if [[ -z "${BUILD_CMD+x}" ]] && [[ -f package.json ]] && grep -q '"build"' package.json; then
    BUILD="npm run build"
fi
if [[ -n "$BUILD" ]]; then
    log "building: $BUILD"
    bash -c "$BUILD" >>"$LOG_FILE" 2>&1 || fail "build failed; see $LOG_FILE (the checkout is at ${REMOTE:0:12}, the app still runs ${DEPLOYED:0:12}; retried next run)"
fi

if [[ -n "$PM2_APPS" ]]; then
    log "restarting: $PM2_APPS"
    # shellcheck disable=SC2086
    pm2 restart $PM2_APPS --update-env >>"$LOG_FILE" 2>&1 || fail "pm2 restart failed; see $LOG_FILE (retried next run)"
fi

mkdir -p "$(dirname "$DEPLOYED_FILE")"
printf '%s\n' "$REMOTE" > "$DEPLOYED_FILE"
log "deployed ${REMOTE:0:12}"
