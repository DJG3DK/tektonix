#!/usr/bin/env bash
# scripts/backup.sh with pg_dump, pg_restore and docker stubbed: the bundle
# mode dumps through the postgres container and tars the volumes out of the
# agent's, without reading .env; the host mode reads one value from .env
# and never runs it as shell.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
SCRIPT="$PWD/scripts/backup.sh"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "  ok   $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL $1"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

mkdir -p "$T/bin"
# docker: records the call; answers the three compose exec shapes the script uses.
cat > "$T/bin/docker" <<'EOF'
#!/bin/bash
echo "docker $*" >> "$CALLS"
case "$*" in
    *"exec -T postgres sh -c"*pg_dump*) printf 'PGDMP-bytes';;
    *"exec -T postgres pg_restore --list"*) cat >/dev/null; for i in 1 2 3 4 5 6; do echo "; 1 2 TABLE DATA public t$i"; done;;
    *"exec -T agent tar czf -"*) printf 'tar-bytes';;
esac
EOF
printf '#!/bin/bash\necho "pg_dump $*" >> "$CALLS"\nfor a in "$@"; do case "$a" in --file=*) printf PGDMP > "${a#--file=}";; esac; done\n' > "$T/bin/pg_dump"
printf '#!/bin/bash\necho "pg_restore $*" >> "$CALLS"\nfor i in 1 2 3 4 5 6; do echo "; 1 2 TABLE DATA public t$i"; done\n' > "$T/bin/pg_restore"
chmod +x "$T/bin/"*
export PATH="$T/bin:$PATH" CALLS="$T/calls"

echo "backup.sh --bundle"
mkdir -p "$T/bundle"
: > "$CALLS"
out="$(AGENT_HOME="$T/bundle" "$SCRIPT" --bundle "$T/bundle/backups" 2>&1)"; rc=$?
check "runs without any .env at all" '[[ $rc == 0 ]]'
check "pg_dump runs inside the postgres container, custom format, no owner" \
    'grep -q "^docker compose exec -T postgres sh -c pg_dump -U .*--format=custom --no-owner" "$CALLS"'
dump="$(ls "$T/bundle/backups"/agent-*.dump 2>/dev/null | head -1)"
check "the dump is what the container wrote, readable by the owner only" \
    '[[ -n "$dump" && "$(cat "$dump")" == PGDMP-bytes && "$(stat -c %a "$dump")" == 600 ]]'
check "the listing check runs in the container too (the host may have no pg_restore)" \
    'grep -q "^docker compose exec -T postgres pg_restore --list" "$CALLS" && [[ "$out" == *"6 tables"* ]]'
vols="$(ls "$T/bundle/backups"/agent-*-volumes.tgz 2>/dev/null | head -1)"
check "the volumes the dump is useless without are tarred out of the agent container" \
    '[[ -n "$vols" && "$(cat "$vols")" == tar-bytes ]] && grep -q "^docker compose exec -T agent tar czf - -C / app/data app/router-config app/shared run/tektonix-pg run/tektonix-router" "$CALLS"'
check "nothing on the host's pg tools was called" '! grep -q "^pg_" "$CALLS"'

# Retention prunes the tarball with its dump.
for s in 20200101T000000Z 20200102T000000Z; do : > "$T/bundle/backups/agent-$s.dump"; : > "$T/bundle/backups/agent-$s-volumes.tgz"; done
AGENT_HOME="$T/bundle" BACKUP_KEEP=1 "$SCRIPT" --bundle "$T/bundle/backups" >/dev/null 2>&1
check "pruning keeps the newest N and takes each old tarball with its dump" \
    '[[ "$(ls "$T/bundle/backups" | wc -l)" == 2 && ! -e "$T/bundle/backups/agent-20200101T000000Z-volumes.tgz" ]]'

echo "backup.sh on a host install"
mkdir -p "$T/host"
# A DSN saved through the Settings page can hold anything; this one holds
# a command. `. ./.env` would have run it.
printf 'OTHER="x"\nLANGGRAPH_PG_DSN="postgresql://u:p$(touch %s/executed)@localhost/db"\n' "$T" > "$T/host/.env"
: > "$CALLS"
out="$(AGENT_HOME="$T/host" "$SCRIPT" "$T/host/backups" 2>&1)"; rc=$?
check "the dump runs against the DSN from .env" '[[ $rc == 0 ]] && grep -q "^pg_dump --dbname=postgresql://u:p" "$CALLS"'
check "the value is read, never executed" '[[ ! -e "$T/executed" ]]'
check "and it reaches pg_dump verbatim, \$( and all" 'grep -qF "p\$(touch $T/executed)@localhost/db" "$CALLS"'
rm -f "$T/host/.env"
out="$(AGENT_HOME="$T/host" "$SCRIPT" "$T/host/backups" 2>&1)"; rc=$?
check "without a DSN it says so and stops" '[[ $rc != 0 && "$out" == *"LANGGRAPH_PG_DSN"* ]]'

echo "$pass passed, $fail failed"
[[ $fail == 0 ]]
