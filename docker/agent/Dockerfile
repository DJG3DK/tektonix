# The agent, as one image.
#
# Two things here are not the obvious choice, and both are load-bearing:
#
#  * the docker CLI is installed but no daemon runs. The container talks to the
#    HOST's daemon over the mounted socket, so the sandbox containers it starts
#    are its SIBLINGS, not its children. Docker-in-docker would need privileged
#    mode and would nest a second storage driver for no benefit.
#  * because those siblings are created by the host daemon, every bind mount
#    the agent asks for is resolved against the HOST filesystem. That is what
#    AGENT_HOST_PATH_MAP is for (agent/tools/sandbox.py) and why compose sets
#    it from the same variable it mounts the projects with.

FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build


FROM python:3.12-slim AS runtime

# git: the agent's own worktree/commit operations run in this container.
# ripgrep: the planner's repo search.
# docker-cli: talks to the host daemon, see above. No daemon, no compose plugin.
# curl + ca-certificates: healthcheck and TLS to the model provider.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ripgrep curl ca-certificates gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ ./agent/
COPY scripts/ ./scripts/
COPY skills/ ./skills/
COPY memory/ ./memory/
COPY pytest.ini ruff.toml ./
COPY --from=frontend /build/dist ./frontend/dist

COPY docker/agent/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    API_PORT=8100 \
    # AGENT_PROJECT_ROOTS, not AGENT_PROJECTS_ROOT. provisioning.py reads the
    # first; the second was set here and read nowhere, so the allow-list fell
    # back to its /home default and onboarding REFUSED every path under
    # /projects -- the only place the bundle puts anything.
    AGENT_PROJECT_ROOTS=/projects

EXPOSE 8100
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=6 \
    CMD curl -fsS http://127.0.0.1:8100/api/health || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
