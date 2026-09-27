#!/bin/sh
# Runs once per start of checks-postgres (its data directory is a tmpfs, so
# initdb runs every time), as the superuser over the local socket.
#
# The bootstrap password in docker-compose.yml is public. Nothing needs it:
# the agent runs psql in this container over `docker exec`, and each review's
# checks get a plain role of their own. So replace it with one nobody holds,
# and a check that tries the superuser with the password it read in the
# compose file gets nothing.
set -eu
pw=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d postgres -c "ALTER USER \"$POSTGRES_USER\" PASSWORD '$pw'"
echo "[checks-postgres] superuser password replaced; each run uses its own role"
