#!/usr/bin/env bash
# Back up everything that cannot be rebuilt from the repo.
#
# One Postgres database holds all of it: tasks and their checkpoints, planning
# sessions, project and org memory, episodes, runtime limits, the GitHub inbox
# and its encrypted tokens, users, sessions and 2FA secrets. Lose it and the
# box still runs -- with no history, no memory and no accounts.
#
# The dump is useless on its own. AUTH_SECRET_KEY (in .env) is what decrypts
# the TOTP secrets and the stored GitHub tokens, so a restore onto a box with a
# different key comes back with unreadable secrets and locked-out 2FA. Keep the
# .env beside the dump, or at least keep that one value somewhere you trust.
#
#   scripts/backup.sh [destination-dir]            default: $AGENT_HOME/backups
#   scripts/backup.sh --bundle [destination-dir]   the compose bundle
#
# --bundle is for `docker compose up` installs, where Postgres is not
# published to the host and its password lives in a volume: pg_dump runs
# inside the postgres container, and the small volumes that go with the
# database (the signing key, the model pins, the generated secrets,
# projects.json) are tarred out of the agent container beside it. Nothing
# in .env is read in that mode.
#
# Restoring is documented in docs/backup.md, and exercised end to end by
# scripts/verify_backup_restore.sh, which restores into a scratch database and
# checks the rows are really there.
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
MODE=host
if [ "${1:-}" = "--bundle" ]; then MODE=bundle; shift; fi
DEST="${1:-$AGENT_HOME/backups}"
KEEP="${BACKUP_KEEP:-14}"          # how many dumps to retain

cd "$AGENT_HOME"
mkdir -p "$DEST"
chmod 700 "$DEST"
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
OUT="$DEST/agent-$STAMP.dump"

if [ "$MODE" = host ]; then
    # Only the one value, parsed the way the agent parses it -- never sourced
    # as shell. A value saved from the Settings page may contain `$(` or a
    # backtick, and `. ./.env` would have executed it.
    PY_BIN="$AGENT_HOME/.venv/bin/python"; [ -x "$PY_BIN" ] || PY_BIN=python3
    DSN=$("$PY_BIN" - "$AGENT_HOME/.env" <<'PY'
import os, sys
value = ""
if os.path.exists(sys.argv[1]):
    try:
        from dotenv import dotenv_values
        value = dotenv_values(sys.argv[1]).get("LANGGRAPH_PG_DSN") or ""
    except ImportError:  # the agent's venv is not on PATH: a plain KEY=value read
        for line in open(sys.argv[1]):
            if line.startswith("LANGGRAPH_PG_DSN="):
                value = line.split("=", 1)[1].strip().strip("\"'")
sys.stdout.write(value)
PY
)
    : "${DSN:?LANGGRAPH_PG_DSN is not set in $AGENT_HOME/.env}"
    # Custom format (-Fc): compressed, and restorable table by table with
    # pg_restore. --no-owner so a restore into a differently-named role works.
    pg_dump --dbname="$DSN" --format=custom --no-owner --file="$OUT"
    chmod 600 "$OUT"
    # A dump that cannot be listed is not a backup. This catches a truncated
    # or half-written file now, rather than on the day you need it.
    TABLES=$(pg_restore --list "$OUT" | grep -c 'TABLE DATA' || true)
else
    umask 077
    docker compose exec -T postgres sh -c \
        'pg_dump -U "${POSTGRES_USER:-agent}" --format=custom --no-owner "${POSTGRES_DB:-tektonix}"' > "$OUT"
    TABLES=$(docker compose exec -T postgres pg_restore --list < "$OUT" | grep -c 'TABLE DATA' || true)
    # The volumes the dump is useless without: /app/data (AUTH_SECRET_KEY),
    # /app/router-config (the pins), /app/shared (projects.json and the
    # review secret), and the two generated secrets. Paths are as the agent
    # container sees them, so `tar x -C /` in the same container restores.
    VOLS="$DEST/agent-$STAMP-volumes.tgz"
    docker compose exec -T agent tar czf - -C / app/data app/router-config app/shared run/tektonix-pg run/tektonix-router > "$VOLS"
    echo "backup: wrote $VOLS ($(du -h "$VOLS" | cut -f1); the key, the pins, the secrets, projects.json)"
fi

if [ "$TABLES" -lt 5 ]; then
    echo "backup: $OUT lists only $TABLES tables -- refusing to call that a backup" >&2
    exit 1
fi

SIZE=$(du -h "$OUT" | cut -f1)
echo "backup: wrote $OUT ($SIZE, $TABLES tables)"

# Keep the last N. Deliberately by name, which sorts chronologically.
mapfile -t OLD < <(ls -1 "$DEST"/agent-*.dump 2>/dev/null | head -n "-$KEEP" || true)
for f in "${OLD[@]:-}"; do
    [ -n "$f" ] || continue
    rm -f "$f" "${f%.dump}-volumes.tgz"
    echo "backup: pruned $(basename "$f")"
done
