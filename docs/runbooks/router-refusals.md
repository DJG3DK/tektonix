# The router refused, or a role answered from its fallback

## What you see

One of these, and they are the same family:

- A task or turn that produced nothing, with `No endpoints found that can
  handle the requested parameters` somewhere in a log.
- A role that works but is obviously the wrong model — the fallback answered
  every call and nothing said so.
- The reviewer returning no verdict, or the consolidator failing, when the
  model behind them is fine for chat but cannot do a forced tool call.

The router (`model-router`, `services/model-router` on :4001) fronts every model call under an
`agent-*` alias. A refusal there surfaces as a missing capability much further
downstream, which is why it reads as a broken feature rather than a bad pin.

---

## Check

**Recent failures, with the provider's own words.** `error_detail` is the
reason the row failed; it is the single most useful field on this page:

```bash
cd /home/3d-agent
python3 - <<'PY'
import json, time, datetime
rows = [json.loads(l) for l in open('services/model-router/logs/routing.jsonl') if l.strip()]
recent = [r for r in rows if r['ts'] >= time.time() - 24*3600]
bad = [r for r in recent if r.get('error') or r.get('error_detail')]
f = lambda t: datetime.datetime.fromtimestamp(t, datetime.UTC).strftime('%m-%d %H:%M')
print(f"{len(bad)} failed calls of {len(recent)} in 24h")
for r in bad[-15:]:
    print(' ', f(r['ts']), (r.get('requested_model') or '?'), '->', (r.get('routed_model') or '-'),
          '|', str(r.get('error_detail'))[:110])
PY
```

**Which model each role is actually pinned to** — the config is the truth, the
dashboard is a view of it:

```bash
grep -A3 'model_name: agent-' /home/3d-agent/services/model-router/config.yaml | grep -E 'model_name|model:'
```

**Is the router even up?**

```bash
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:4001/health/liveliness   # 200 expected
pm2 logs model-router --nostream --lines 40
```

---

## Act

### "No endpoints found that can handle the requested parameters"

The alias asked for something the pinned model, or the pinned *provider*, does
not offer. Three usual causes, in order:

1. **A provider pin that is too narrow.** `extra_body.provider.only: ["X"]`
   with `allow_fallbacks: false` means one provider or nothing. If that
   provider is rate-limited or has dropped the model, every call fails. Widen
   it (drop `only`, or set `allow_fallbacks: true`) on the Models page.
2. **`require_parameters: true` plus an unsupported parameter.** Tool calling,
   a response format, or a sampling parameter the model does not accept. The
   Anthropic family needs its extras (`additional_drop_params`) that other
   families must not have — `agent/model_config.py` manages that pairing when
   the pin changes from the dashboard.
3. **A dated slug that no longer exists.** `…-0423` style names retire. The
   catalog is the authority: an undated alias may resolve to nothing.

After repinning, confirm with the next real call rather than assuming:

```bash
python3 - <<'PY'
import json
rows = [json.loads(l) for l in open('/home/3d-agent/services/model-router/logs/routing.jsonl') if l.strip()]
print(rows[-1]['requested_model'], '->', rows[-1].get('routed_model'), '| err:', rows[-1].get('error_detail'))
PY
```

### A role is silently answering from its fallback

This is the quiet one. The alias fails, the router falls back, the work
succeeds, and the only evidence is that the wrong model's name is in the log.

Compare what each alias is **pinned** to against what actually **answered**.
(The log's two model fields are not reliably "alias" and "backend" — either
order shows up — so this reads both and matches whichever one names an alias.
A naive string comparison of the two fields reports false alarms.)

```bash
cd /home/3d-agent
.venv/bin/python - <<'PY'
import json, time, yaml
from collections import defaultdict
cfg = yaml.safe_load(open('services/model-router/config.yaml'))
pins = {e['model_name']: (e.get('params') or e['litellm_params'])['model'].split('/', 1)[-1]
        for e in cfg['model_list'] if e['model_name'].startswith('agent-')}
rows = [json.loads(l) for l in open('services/model-router/logs/routing.jsonl') if l.strip()]
recent = [r for r in rows if r['ts'] >= time.time() - 24*3600 and not r.get('error')]
seen = defaultdict(set)
for r in recent:
    names = [str(r.get('requested_model') or ''), str(r.get('routed_model') or '')]
    alias = next((n for n in names if n in pins), None)
    if not alias:
        continue
    backend = next((n for n in names if n != alias and n), None)
    if backend:
        seen[alias].add(backend.lstrip('~').split('/', 1)[-1])
for alias, backends in sorted(seen.items()):
    pin = pins[alias].lstrip('~')
    odd = [b for b in backends if b not in pin and pin not in b]
    print(f"{alias:<28} pinned={pin:<30} answered={sorted(backends)}"
          f"{'   <-- FALLBACK ANSWERED' if odd else ''}")
PY
```

Healthy output has `answered` matching `pinned` for every alias:

```
agent-cartographer           pinned=z-ai/glm-flash-latest        answered=['glm-flash-latest']
agent-reviewer               pinned=anthropic/claude-sonnet-5    answered=['claude-sonnet-5']
```

A line marked `<-- FALLBACK ANSWERED` is a role whose own pin is not
answering. Fix the pin; the fallback is a safety net, not a configuration.
An alias missing from the output entirely has simply not been called in the
window — widen the 24h if you need it.

### The reviewer or consolidator produces nothing

Both need a model that will make a **forced tool call**. A model that chats
happily can still refuse that, and the failure looks like an empty result
rather than an error.

```bash
pm2 logs commit-reviewer --nostream --lines 60 | grep -iE 'error|refus|tool'
.venv/bin/python scripts/probe_forced_tool_call.py   # asks each managed role directly
```

Repin the role to something with the tool-call badge on the Models page — that
badge exists for this exact failure.

---

## What not to do

- **Do not restart `model-router` to clear a refusal while a task is running.**
  A model call in flight dies with the process and the task escalates with
  "peer closed connection". Stop the task, restart, then resume it.
- **Do not hand-edit `config.yaml` for a pin you can change on the Models
  page.** The page does a surgical text replace that keeps the file's comments;
  those comments are the operator log for why each pin is what it is.
- **Do not remove a fallback to "force" the right model.** You lose the safety
  net and gain a hard outage the next time that provider hiccups.
