#!/bin/sh
# Root only long enough to make what the service writes the operator's.
#
# Both review services write into PROJECTS_DIR, the operator's own folder
# on the host: review worktrees, and the merges themselves. As root, every
# file they create there is root-owned on Linux. With PUID/PGID set in
# .env (`id -u`, `id -g`) this drops to that user first, after chowning
# the two places inside the container it writes to -- the verdict volume
# and the per-project credential copies -- which a release that ran as
# root left root-owned. Unset (Docker Desktop, where the bind mount does
# not care; every install before 2026-09-29), it stays root.
set -eu
puid=${PUID:-0}
pgid=${PGID:-0}
state=${REVIEW_STATE_DIR:-/app/review-state}
secrets=/app/services/commit-reviewer/review-secrets
if [ "$puid" != 0 ] && [ "$(id -u)" = 0 ]; then
    getent group "$pgid" >/dev/null || groupadd -g "$pgid" tektonix
    getent passwd "$puid" >/dev/null || useradd -u "$puid" -g "$pgid" -M -d "$state" -s /usr/sbin/nologin tektonix
    mkdir -p "$state" "$secrets"
    chown -R "$puid:$pgid" "$state" "$secrets"
    # git reads and may write ~/.gitconfig; somewhere the user owns.
    export HOME="$state"
    exec setpriv --reuid="$puid" --regid="$pgid" --clear-groups "$@"
fi
exec "$@"
