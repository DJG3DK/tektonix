#!/bin/sh
# The router's config lives in the routerconfig volume, shared with the
# agent so the Models page can write pins the router then re-reads. On the
# first boot the volume is empty: copy the seed in (the operator's own
# ROUTER_CONFIG, or the committed example). After that the volume's file
# is the operator's and is never overwritten by a start or an upgrade.
set -eu
seed=${MODEL_ROUTER_CONFIG_SEED:-/app/config.seed.yaml}
live=${MODEL_ROUTER_CONFIG:-/app/router-config/config.yaml}
mkdir -p "$(dirname "$live")"
if [ ! -s "$live" ]; then
    cp "$seed" "$live"
    echo "[router] seeded $live from $seed"
fi
exec uvicorn router.app:app --host 0.0.0.0 --port "${MODEL_ROUTER_PORT:-4001}"
