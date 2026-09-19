#!/usr/bin/env bash
# One-time baseline setup for the Tektonix Cloud Agent development environment.
#
# Idempotent and safe to re-run. With environment builds this runs once to
# create the snapshot; per-boot service startup lives in .cursor/start.sh.
#
# It prepares everything needed to run the stack locally: PostgreSQL, the two
# Python virtualenvs (agent + model router), the built dashboard, the
# deployment-specific config files, and the Docker sandbox image the agent uses
# to run LLM-issued bash commands.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"

log() { printf '\n=== %s ===\n' "$*"; }

# --- System packages -------------------------------------------------------
log "system packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  postgresql postgresql-contrib \
  ripgrep \
  python3-venv python3-dev build-essential libpq-dev \
  docker.io fuse-overlayfs iptables uidmap
# The fuse packages can stop half-configured on an interactive conffile prompt
# in a headless build; finish them non-interactively, keeping existing configs.
sudo dpkg --configure -a --force-confold || true

# Let the ubuntu user reach the Docker socket without sudo. Membership is
# written to /etc/group, so it persists into the snapshot and every later boot.
sudo usermod -aG docker ubuntu || true

# --- Python virtualenvs ----------------------------------------------------
log "agent virtualenv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

log "model-router virtualenv"
[ -d services/model-router/venv ] || python3 -m venv services/model-router/venv
services/model-router/venv/bin/pip install -q --upgrade pip
services/model-router/venv/bin/pip install -q -r services/model-router/requirements.txt

# --- Dashboard -------------------------------------------------------------
log "dashboard build"
( cd frontend && npm ci && npm run build )

# --- Deployment-specific config (gitignored) -------------------------------
log "config files"
bash .cursor/gen-config.sh

# --- PostgreSQL cluster + database ----------------------------------------
log "postgres bootstrap"
sudo pg_ctlcluster 16 main start 2>/dev/null || sudo service postgresql start || true
for _ in $(seq 1 30); do sudo -u postgres pg_isready -q && break; sleep 1; done
sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname='agent'" | grep -q 1 \
  || sudo -u postgres psql -c "CREATE ROLE agent LOGIN PASSWORD 'agent';"
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname='three_d_agent'" | grep -q 1 \
  || sudo -u postgres createdb -O agent three_d_agent

# --- Docker sandbox image --------------------------------------------------
# The agent runs every LLM-issued bash command inside this image. It is only
# exercised during live tasks (which also need an OpenRouter key), but building
# it here makes the app's own /api/health report fully green.
log "docker sandbox image"
sudo mkdir -p /etc/docker
# Nested-container friendly: fuse-overlayfs storage, and no host iptables/bridge
# management (the outer VM does not support a functional container bridge).
echo '{"storage-driver":"fuse-overlayfs","iptables":false,"bridge":"none"}' | sudo tee /etc/docker/daemon.json >/dev/null
if ! sudo docker info >/dev/null 2>&1; then
  sudo bash -c 'nohup dockerd >/var/log/dockerd.log 2>&1 &'
  for _ in $(seq 1 30); do sudo docker info >/dev/null 2>&1 && break; sleep 1; done
fi
if ! sudo docker image inspect tektonix-sandbox:latest >/dev/null 2>&1; then
  # --network=host so the build's apt/pip/playwright downloads use the host's
  # working egress rather than the (non-functional) container bridge.
  sudo docker build --network=host -t tektonix-sandbox:latest docker/agent-sandbox/
fi

log "install complete"
