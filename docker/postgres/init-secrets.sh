#!/bin/sh
# The bundle's generated secrets, written before Postgres starts.
#
# The database password and the model router's key used to be compose
# defaults -- `agent` and `sk-local-dev` -- identical in every installation
# and printed in the example .env. This writes a random one of each on first
# boot into the `bundlesecrets` volume, and every service that needs one
# reads it from there (POSTGRES_PASSWORD_FILE, MODEL_ROUTER_KEY_FILE).
#
# It runs here because postgres is the first service to start: everything
# else waits for it to be healthy, so the files exist before anyone reads
# them.
#
# An operator's own value in .env still wins -- except `sk-local-dev`, the
# old published default, which is treated as unset.
set -eu

dir=${TEKTONIX_SECRETS_DIR:-/run/tektonix-secrets}
mkdir -p "$dir"
umask 022

rand() {
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

write() {
    # 0444: the router and the reviewer do not run as the postgres user.
    printf '%s' "$2" > "$dir/$1.tmp"
    chmod 0444 "$dir/$1.tmp"
    mv "$dir/$1.tmp" "$dir/$1"
}

if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    write postgres_password "$POSTGRES_PASSWORD"
elif [ ! -s "$dir/postgres_password" ]; then
    if [ -s "$PGDATA/PG_VERSION" ]; then
        # A database initialised before this script existed was created with
        # the old default. Keep it working; say how to rotate it.
        write postgres_password agent
        echo "[init-secrets] existing database uses the old default password 'agent';" \
             "set POSTGRES_PASSWORD in .env and ALTER USER agent to rotate it" >&2
    else
        write postgres_password "$(rand)"
        echo "[init-secrets] generated a database password in the bundlesecrets volume"
    fi
fi

if [ -n "${MODEL_ROUTER_KEY:-}" ] && [ "$MODEL_ROUTER_KEY" != "sk-local-dev" ]; then
    write model_router_key "$MODEL_ROUTER_KEY"
elif [ ! -s "$dir/model_router_key" ]; then
    write model_router_key "sk-$(rand)"
    echo "[init-secrets] generated a model router key in the bundlesecrets volume"
fi

unset POSTGRES_PASSWORD MODEL_ROUTER_KEY
export POSTGRES_PASSWORD_FILE="$dir/postgres_password"
exec docker-entrypoint.sh "$@"
