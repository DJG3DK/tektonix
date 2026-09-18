#!/usr/bin/env bash
# Per-boot startup for the Tektonix Cloud Agent development environment.
#
# Brings up the infrastructure the app depends on (PostgreSQL and the Docker
# daemon) and makes sure the gitignored config exists. The application
# processes themselves (model router + agent) run as visible terminals, so they
# are NOT started here. Safe to run repeatedly.
set -euo pipefail
cd "$(dirname "$0")/.."

log() { printf '\n=== %s ===\n' "$*"; }

# Config files are gitignored, so a fresh checkout may not have them. Generate
# any that are missing (existing ones, incl. baked-in secrets, are untouched).
log "config files"
bash .cursor/gen-config.sh

# --- PostgreSQL ------------------------------------------------------------
log "postgres"
sudo pg_ctlcluster 16 main start 2>/dev/null || sudo service postgresql start || true
for _ in $(seq 1 30); do sudo -u postgres pg_isready -q && break; sleep 1; done
# Role/DB live in the snapshot's data dir, but recreate them if this is a fresh
# cluster (e.g. a build without a prior snapshot).
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='agent'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE agent LOGIN PASSWORD 'agent';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='three_d_agent'" | grep -q 1 \
  || sudo -u postgres createdb -O agent three_d_agent

# --- Docker daemon ---------------------------------------------------------
log "docker daemon"
if ! sudo docker info >/dev/null 2>&1; then
  sudo bash -c 'nohup dockerd >/var/log/dockerd.log 2>&1 &'
  for _ in $(seq 1 30); do sudo docker info >/dev/null 2>&1 && break; sleep 1; done
fi
# Make the socket usable without a fresh login picking up the docker group, so
# the agent terminal can shell out to `docker` for its sandbox health check.
sudo chmod 666 /var/run/docker.sock 2>/dev/null || true

log "start complete"
