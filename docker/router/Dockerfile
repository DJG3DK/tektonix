# The model router. Every model call from every process resolves its `agent-*`
# alias here, so it starts before the agent -- and the agent depends on it only
# in the sense that its first call fails without it.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The router runs as this user (the entrypoint drops to it after seeding
# the config): it reads a key and a config and appends to a ledger, none
# of which needs root. The two directories it touches are its own from the
# start, so a volume first created from this image is owned by it too.
RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin router \
    && mkdir -p /app/logs /app/router-config \
    && chown router:router /app/logs /app/router-config

WORKDIR /app
COPY services/model-router/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY services/model-router/router ./router
# config.yaml is the OPERATOR's file (it holds their model pins) and is
# gitignored, so the image ships the example and compose mounts the real one
# over it when there is one.
COPY services/model-router/config.example.yaml ./config.seed.yaml
COPY docker/router/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    MODEL_ROUTER_CONFIG=/app/router-config/config.yaml \
    MODEL_ROUTER_LEDGER=/app/logs/routing.jsonl
EXPOSE 4001
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=6 \
    CMD curl -fsS http://127.0.0.1:4001/health/liveliness || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
