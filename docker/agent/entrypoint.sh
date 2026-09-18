#!/usr/bin/env sh
# First-run setup, then the server. Everything here is idempotent: the bundle
# is meant to survive `docker compose down && up` without the operator having
# to remember which of these steps they already did.
set -e

state=/app/data
mkdir -p "$state" /app/logs

# 1. The signing key. Generated once and kept in the volume rather than in the
#    compose file, so `up` in a fresh checkout does not silently invalidate
#    every existing session -- and so it never lands in the operator's shell
#    history or a committed .env.
if [ -z "$AUTH_SECRET_KEY" ]; then
    if [ ! -f "$state/auth_secret_key" ]; then
        python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())" \
            > "$state/auth_secret_key"
        chmod 600 "$state/auth_secret_key"
        echo "[entrypoint] generated a new AUTH_SECRET_KEY in the data volume"
    fi
    AUTH_SECRET_KEY=$(cat "$state/auth_secret_key")
    export AUTH_SECRET_KEY
fi

# 1b. The review-control secret. Generated the same way and for the same
#     reason: unset means every merge is refused at the end of a task that has
#     already been paid for (agent/health.py), and the deployment reports
#     itself unhealthy until it is set. Generating it now means it already
#     matches when the review service joins the bundle.
if [ -z "$REVIEW_CONTROL_SECRET" ]; then
    if [ ! -f "$state/review_control_secret" ]; then
        python -c "import secrets;print(secrets.token_urlsafe(32))" > "$state/review_control_secret"
        chmod 600 "$state/review_control_secret"
        echo "[entrypoint] generated a new REVIEW_CONTROL_SECRET in the data volume"
    fi
    REVIEW_CONTROL_SECRET=$(cat "$state/review_control_secret")
    export REVIEW_CONTROL_SECRET
fi

# 2. projects.json. The agent needs the file to exist; what is IN it comes from
#    the dashboard's own onboarding, so an empty one is the correct start.
#
#    It lives in the data volume, not beside the code: the two review services
#    are separate containers that read the same file, and a project onboarded
#    from the dashboard has to be visible to them without a rebuild. Both
#    sides honour AGENT_PROJECTS_JSON.
PROJECTS_FILE=${AGENT_PROJECTS_JSON:-/app/projects.json}
if [ ! -f "$PROJECTS_FILE" ]; then
    mkdir -p "$(dirname "$PROJECTS_FILE")"
    echo '{"projects": {}}' > "$PROJECTS_FILE"
    echo "[entrypoint] created an empty $PROJECTS_FILE -- add projects from Settings"
fi

# 3. Wait for Postgres. compose's depends_on only waits for the container, not
#    for the database inside it, and the agent retries its pool forever rather
#    than failing loudly -- which looks like a hang.
if [ -n "$LANGGRAPH_PG_DSN" ]; then
    i=0
    until python -c "
import sys, psycopg
try:
    psycopg.connect('$LANGGRAPH_PG_DSN', connect_timeout=3).close()
except Exception as e:
    print(e, file=sys.stderr); sys.exit(1)
" 2>/dev/null; do
        i=$((i + 1))
        if [ "$i" -ge 60 ]; then
            echo "[entrypoint] postgres did not become reachable; starting anyway" >&2
            break
        fi
        [ "$i" = 1 ] && echo "[entrypoint] waiting for postgres..."
        sleep 2
    done
fi

# 4. The sandbox image. Built here rather than in compose because it is the
#    agent's own dependency, and a bundle that starts without it fails on the
#    first tool call of the first task instead of at boot.
if ! docker image inspect tektonix-sandbox:latest >/dev/null 2>&1; then
    if [ -d /app/docker/agent-sandbox ]; then
        echo "[entrypoint] building the sandbox image (first run only)..."
        docker build -q -t tektonix-sandbox:latest /app/docker/agent-sandbox >/dev/null \
            && echo "[entrypoint] sandbox image ready" \
            || echo "[entrypoint] sandbox build failed -- tasks will fail on their first command" >&2
    else
        echo "[entrypoint] no sandbox context mounted; expecting tektonix-sandbox:latest on the host" >&2
    fi
fi

echo "[entrypoint] dashboard on http://localhost:${API_PORT:-8100}"
exec uvicorn agent.server:app --host 0.0.0.0 --port "${API_PORT:-8100}"
