# model-router

The agent's model router. Every model call from every process on the box —
the agent, the demo bot, the mail agent, the trading gate — resolves its
`agent-*` alias here, under its own key.

```
./venv/bin/uvicorn router.app:app --host 127.0.0.1 --port 4001
pm2 start ecosystem.config.js          # the managed form
```

## What it does

**Alias resolution with ordered fallbacks.** `config.yaml` maps a role name to
a model and, optionally, to a chain to try when that model refuses. Retries
happen on the SAME deployment first, and only for genuinely transient failures
(429, 5xx, timeouts) — moving to a fallback on the first 429 throws away the
model the operator pinned because a provider asked us to wait a moment.

**Hot reload.** The file's mtime is checked per request and a changed table is
swapped in between requests, so repinning from the dashboard does not require
a restart — and a restart kills every model call in flight across every
service sharing the router.

**Per-alias timeouts.** Each deployment sets its own. Without one, a coder call
once sat upstream for 1,802 seconds and returned 280 tokens.

**Billed cost, not estimates.** OpenRouter returns `usage.cost` per call; that
figure goes straight into `logs/routing.jsonl` and is what every spend number
in the dashboard reads. A rate table drifts the moment a provider reprices.

**A key per consumer.** Each caller holds its own key, recorded on the call as
`caller`, so spend is answerable per consumer and any one key can be revoked
without touching the others.

## Compatibility

It reads `config.yaml` — the operator's pins, the same file the Models page
writes.

Four things are contracts, not choices, each with a caller that breaks:

| contract | who depends on it |
|---|---|
| `x-router-call-id` response header | `budget_guard.call_id_of` matches spend on this exact name |
| `metadata.agent_task_id` in the body | without it the ledger prices a call but cannot total a task |
| `routing.jsonl` field names | `router_ledger.py`, `metrics.py`, `model_rates.py` all parse it |
| `/health/liveliness`, unauthenticated | `agent/health.py` polls it with no key |

## Endpoints

| route | notes |
|---|---|
| `POST /v1/chat/completions` | streaming and buffered; fallbacks on the buffered path |
| `GET /health/liveliness` | no auth, by design |
| `GET /health/readiness` | 503 when there are no deployments or no upstream key |
| `GET /v1/model/info` | the deployment table |
| `GET /v1/models` | alias list |

## Behaviour worth knowing

**Fallbacks apply up to the first byte**, on both paths. A response is
committed the moment a byte reaches the client, and an upstream refusing with a
429 does so before any body exists — so falling back there is as safe for a
stream as for a buffered call. Only a failure *part-way through* a stream is
unrecoverable, because retrying would splice two completions into one
response.

**Every attempt is a ledger line**, sharing one `call_id`. A fallback therefore
cannot double-charge a task, and the Analytics error rate sees the failure that
actually happened.

**Costs are OpenRouter's `usage.cost`**, never computed from the rate table in
`config.yaml`. The estimate and the bill disagree; that is why the ledger
exists.

**A broken config keeps the running table.** The moment an operator saves a bad
file is exactly the moment to keep serving the one known to work.

## Tests

```
./venv/bin/python -m pytest tests/ -q      # 48, no network
```

Live checks live in `docs/runbooks/model-router-cutover.md`.
