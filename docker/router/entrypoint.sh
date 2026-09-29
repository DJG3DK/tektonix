#!/bin/sh
# The router's config lives in the routerconfig volume, shared with the
# agent so the Models page can write pins the router then re-reads. On the
# first boot the volume is empty: copy the seed in (the operator's own
# ROUTER_CONFIG, or the committed example). After that the volume's file
# is the operator's and is never overwritten by a start or an upgrade.
#
# Then the router runs as its own unprivileged user. Nothing it does needs
# root: it reads its key and its config and appends to its ledger. The
# ledger's directory is chowned first, because a volume created by a
# release that ran as root is root-owned, and a ledger write that fails is
# logged at debug level and otherwise silent (router/ledger.py) -- the
# Analytics page would simply have stopped counting.
set -eu
seed=${MODEL_ROUTER_CONFIG_SEED:-/app/config.seed.yaml}
live=${MODEL_ROUTER_CONFIG:-/app/router-config/config.yaml}
ledger=${MODEL_ROUTER_LEDGER:-/app/logs/routing.jsonl}
mkdir -p "$(dirname "$live")" "$(dirname "$ledger")"
if [ ! -s "$live" ]; then
    cp "$seed" "$live"
    echo "[router] seeded $live from $seed"
fi
# (The user exists in the image; a checkout running this script directly,
# as the tests do, has no such user and no privilege to drop.)
if [ "$(id -u)" = 0 ] && getent passwd router >/dev/null 2>&1; then
    chown -R router:router "$(dirname "$ledger")"
    # The agent writes the live config; a copy written before 2026-09-29
    # is 0600 root. Readable once here, and the agent keeps it so.
    chmod a+r "$live" 2>/dev/null || true
    exec setpriv --reuid=router --regid=router --clear-groups \
        uvicorn router.app:app --host 0.0.0.0 --port "${MODEL_ROUTER_PORT:-4001}"
fi
exec uvicorn router.app:app --host 0.0.0.0 --port "${MODEL_ROUTER_PORT:-4001}"
