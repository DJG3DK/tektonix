# The two review services. One image, two commands: agent-review serves the
# dashboard's status/diff/merge API, commit-reviewer polls for new task
# branches and runs the gate. They share services/shared and have the same
# runtime needs, so building the image twice would only mean two of everything
# to keep in step.
#
# Why this can live in a container at all, when it could not before: the only
# genuinely host-bound thing either service does is `pm2 restart` after a
# merge, and that is a separate endpoint from the merge itself. It reports
# itself unavailable here rather than failing (server.js, IN_CONTAINER) -- so
# the review gate works in the bundle and deploying stays the host install's.
FROM node:24-slim

# git does the actual work: worktrees of the live repo, the fixed base..sha
# range every review is measured over, and the --ff-only merge. ca-certificates
# for the router call, curl for the healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# A worktree of somebody's repository is not this image's to own, and git
# refuses to operate on a tree owned by another uid unless told. The bundle
# mounts the operator's projects, so the uid inside will not match theirs.
RUN git config --system --add safe.directory '*'

WORKDIR /app
COPY services/agent-review/package.json services/agent-review/package-lock.json ./services/agent-review/
RUN cd services/agent-review && npm ci --omit=dev --ignore-scripts

COPY services/agent-review/ ./services/agent-review/
COPY services/commit-reviewer/ ./services/commit-reviewer/
COPY services/shared/ ./services/shared/

# projects.json is the OPERATOR's file. The image ships the example so a
# container started without one still answers rather than crashing on a
# missing path; compose mounts the real one over it when there is one.
COPY projects.example.json ./projects.json

ENV AGENT_HOME=/app \
    NODE_ENV=production \
    TEKTONIX_BUNDLE=1
EXPOSE 4100 4101
CMD ["node", "services/agent-review/server.js"]
