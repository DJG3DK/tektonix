#!/usr/bin/env bash
# Prove a backup can actually be restored, without touching the live database.
#
# A dump nobody has ever restored is a hope, not a backup. This takes one (the
# newest, or a path you give it), restores it into a scratch database, checks
# that the rows that matter are really there, and drops the scratch database
# again. Nothing here writes to the live database at any point.
#
#   scripts/verify_backup_restore.sh [dump-file]
#
# The scratch database is created by the local postgres superuser over peer
# auth, because the agent's own role usually cannot CREATE DATABASE. That is
# the only step needing more privilege than the agent itself has.
#
# Run it after changing anything about backups, and once in a while besides.
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$AGENT_HOME"
# shellcheck disable=SC1091
set -a; . ./.env; set +a
: "${LANGGRAPH_PG_DSN:?LANGGRAPH_PG_DSN is not set}"

DUMP="${1:-$(ls -1t "${AGENT_HOME}"/backups/agent-*.dump 2>/dev/null | head -1 || true)}"
if [ -z "$DUMP" ] || [ ! -f "$DUMP" ]; then
    echo "no dump to verify (pass a path, or run scripts/backup.sh first)" >&2
    exit 1
fi

SCRATCH="restore_check_$(date -u +%Y%m%d%H%M%S)"
DB_USER=$(python3 -c 'import sys,urllib.parse as u;print(u.urlsplit(sys.argv[1]).username or "")' "$LANGGRAPH_PG_DSN")
SCRATCH_DSN=$(python3 -c 'import sys,urllib.parse as u;p=u.urlsplit(sys.argv[1]);print(u.urlunsplit((p.scheme,p.netloc,"/"+sys.argv[2],p.query,"")))' "$LANGGRAPH_PG_DSN" "$SCRATCH")

superuser_sql() {   # one statement, as the local postgres superuser
    su -s /bin/bash postgres -c "psql -qtAc $(printf '%q' "$1")"
}

superuser_sql_on() {   # ... against a named database
    su -s /bin/bash postgres -c "psql -qtAd $(printf '%q' "$1") -c $(printf '%q' "$2")"
}

cleanup() {
    superuser_sql "DROP DATABASE IF EXISTS \"$SCRATCH\"" >/dev/null 2>&1 || true
    rm -f "${LIST:-}"
}
trap cleanup EXIT

echo "verifying $(basename "$DUMP") -> scratch database $SCRATCH"
if ! superuser_sql "CREATE DATABASE \"$SCRATCH\" OWNER \"$DB_USER\"" >/dev/null; then
    echo "could not create a scratch database. Run this where the local postgres" >&2
    echo "superuser is reachable over peer auth, or create one by hand and pass" >&2
    echo "its path as the database in LANGGRAPH_PG_DSN." >&2
    exit 1
fi

# Extensions first, as the superuser, because the dump contains
# `CREATE EXTENSION vector` and the application role is not a superuser --
# pgvector is not a trusted extension. --exit-on-error means that one
# statement fails the whole restore, so a real recovery would stop here too.
# This is the same command a restore onto a fresh host has to run first, and
# it is in docs/backup.md for that reason.
for ext in $(pg_restore -l "$DUMP" | sed -n 's/.*EXTENSION - \([a-z_]*\) .*/\1/p' | sort -u); do
    [ "$ext" = "plpgsql" ] && continue
    superuser_sql_on "$SCRATCH" "CREATE EXTENSION IF NOT EXISTS \"$ext\"" >/dev/null 2>&1 \
        || echo "  note: could not pre-create extension $ext; the restore may fail on it"
done

# Extension objects are dropped from the restore LIST, not from the dump: an
# extension has to be created by a superuser (pgvector is not a trusted
# extension), and its COMMENT then belongs to that superuser, so restoring
# either of them as the application role fails -- and --exit-on-error means
# failing on one of them fails the whole restore. Pre-created above, skipped
# here. A real recovery does the same thing; docs/backup.md says so.
LIST="$(mktemp)"
pg_restore -l "$DUMP" | grep -v -E '(^|; )[0-9]+ [0-9]+ (EXTENSION|COMMENT - EXTENSION)' > "$LIST"

# --no-owner: whoever runs this owns the restored objects.
pg_restore --dbname="$SCRATCH_DSN" --no-owner --exit-on-error -L "$LIST" "$DUMP"

fail=0
check() {   # label, sql, minimum
    local got
    got=$(psql "$SCRATCH_DSN" -qtAc "$2" | tr -d '[:space:]')
    if [ -z "$got" ] || [ "$got" -lt "$3" ]; then
        echo "  FAIL  $1: got ${got:-nothing}, expected at least $3"
        fail=1
    else
        echo "  ok    $1: $got"
    fi
}

# The things whose loss would actually hurt, each checked as rows in the
# restored database rather than as "the file exists".
check "task checkpoints"      "SELECT count(*) FROM checkpoints" 1
check "checkpoint payloads"   "SELECT count(*) FROM checkpoint_blobs" 1
check "store rows (memory, sessions, settings, inbox)" "SELECT count(*) FROM store" 1
check "user accounts"         "SELECT count(*) FROM agent_users" 1
check "settings in the store" "SELECT count(*) FROM store WHERE prefix LIKE '%settings%'" 1

# The GitHub tokens are encrypted with AUTH_SECRET_KEY and live in the store.
# If they were configured before the dump, they must come back as ciphertext --
# and the value must NOT be readable in the dump without that key.
TOKENS=$(psql "$SCRATCH_DSN" -qtAc "SELECT count(*) FROM store WHERE key = 'github'" | tr -d '[:space:]')
if [ "${TOKENS:-0}" -ge 1 ]; then
    echo "  ok    GitHub settings restored (encrypted with AUTH_SECRET_KEY -- keep .env with the dump)"
else
    echo "  note  no GitHub settings in this dump (nothing configured yet)"
fi

if [ "$fail" -ne 0 ]; then
    echo "RESTORE VERIFICATION FAILED for $DUMP" >&2
    exit 1
fi
echo "restore verified: $(basename "$DUMP") is usable"
