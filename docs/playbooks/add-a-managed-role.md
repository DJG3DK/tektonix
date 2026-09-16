# Add a managed role

A **role** is a named model seat: `agent-coder`, `agent-summarizer`,
`agent-reviewer`. Code asks for a role, never for a model, and the router
resolves the alias — so repinning a role on the Models page changes which
model does that job, live, with no restart.

Adding one means adding an alias the router can resolve *and* making it
visible on the Models page. Miss the second half and the pin exists but
nobody can change it: that happened on 2026-09-09 with the two frontend
seats, which were pinned in `config.yaml`, described in `ROLE_REQUIREMENTS`,
and absent from `MANAGED_ROLES` — invisible for weeks.

## Run this first

```bash
.venv/bin/python -m pytest tests/test_managed_roles_complete.py -q
```

Green now. It goes red the moment you add the alias, and stays red until
every consumer knows about it:

```
AssertionError: aliases invisible on the Models page: ['agent-your-role']
```

## The edits

1. **`services/model-router/config.yaml`** — a `model_list` entry whose
   `model_name` is `agent-<role>`. Copy the nearest existing seat, including
   its `model_info` costs: the analytics page bills from those numbers, and a
   missing cost silently reports the role as free. Read this file fresh; the
   Models page writes to it, so whatever you remember about the pins is
   probably stale.

   **And add the same entry to `config.example.yaml`**, which *is* tracked.
   The live file is gitignored — it is the operator's, rewritten on every
   repin — so a role that exists only there ships to nobody. A test asserts
   the example covers every managed role.

2. **`agent/model_config.py` → `MANAGED_ROLES`** — `"agent-<role>": "Human
   Label"`. This is what puts it on the Models page. The label is what the
   operator sees, so write the seat, not the implementation
   (`"Planning Chat (Hard)"`, not `"planning_chat_hard"`).

3. **`agent/model_config.py` → `ROLE_REQUIREMENTS`** — what this seat asks a
   model to *do*, on the three axes that fail independently: `tools`,
   `structured`, `strict`. Be accurate rather than generous. `strict` is the
   one that eliminates most models, and the page uses these flags to warn
   before a repin that cannot work.

4. **`README.md`** — the role list. An operator who cannot find the alias in
   the docs cannot repin it, which is the same silence the test exists for.

5. **The call site** — `llm_for_role(config, "agent-<role>")`. A role nothing
   asks for is a seat on a page that changes nothing.

## Verify

```bash
.venv/bin/python -m pytest tests/test_managed_roles_complete.py tests/test_model_config.py -q
curl -s 127.0.0.1:4000/v1/models | grep agent-<role>      # after a router restart
```

The router only learns the alias on restart — and restarting it kills any
model call in flight, which escalates the task that was making it. Check
that nothing is running first (`docs/runbooks/stuck-task.md` §2), or wait.

## What this does not change

Nothing about the gate. A new role is a new seat, not new authority: whatever
it does still runs through the same review, the same budget ceiling, and the
same approval rules. If your new role needs its own middleware, see
[docs/middleware.md](../middleware.md) — subagents do not inherit a chain.
