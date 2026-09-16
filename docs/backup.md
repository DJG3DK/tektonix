# Backup and restore

One Postgres database holds everything that cannot be rebuilt from this repo:

- every task and its checkpoints, which is what makes a task resumable
- planning sessions and their plans
- project memory, org memory and the episodes consolidation feeds on
- runtime limits, the GitHub inbox and its **encrypted** tokens
- user accounts, sessions, 2FA secrets and recovery codes

Lose it and the box still runs. It just has no history, no memory and no
accounts.

> **The dump is not enough on its own.** `AUTH_SECRET_KEY` in `.env` is what
> decrypts the TOTP secrets and the stored GitHub tokens. A restore onto a box
> with a different key comes back with unreadable secrets and every 2FA user
> locked out. Keep `.env` with the dump, or at minimum keep that one value
> somewhere you trust and separate.

---

## Taking a backup

```bash
cd /home/3d-agent
./scripts/backup.sh                 # writes backups/agent-<UTC timestamp>.dump
./scripts/backup.sh /mnt/elsewhere  # or somewhere off this box
```

It uses `pg_dump --format=custom`, which is compressed and restorable table by
table. The script refuses to call the result a backup unless the dump lists at
least five tables, so a truncated or half-written file fails now rather than on
the day you need it. It keeps the last 14 by default (`BACKUP_KEEP`).

Nightly, next to the existing jobs:

```
30 3 * * * /home/3d-agent/scripts/backup.sh >> /home/3d-agent/data/backup.log 2>&1
```

**Put a copy somewhere else.** A dump sitting on the same disk as the database
survives exactly the failures that do not matter.

---

## Proving the backup works

A dump nobody has restored is a hope. This restores one into a scratch
database, checks the rows are really there, and drops it again. It never
writes to the live database:

```bash
./scripts/verify_backup_restore.sh              # newest dump
./scripts/verify_backup_restore.sh backups/agent-20260911T173239Z.dump
```

Real output from this box, on an 811 MB database:

```
verifying agent-20260911T173239Z.dump -> scratch database restore_check_20260911173350
  ok    task checkpoints: 74222
  ok    checkpoint payloads: 37748
  ok    store rows (memory, sessions, settings, inbox): 258
  ok    user accounts: 2
  ok    settings in the store: 2
  ok    GitHub settings restored (encrypted with AUTH_SECRET_KEY -- keep .env with the dump)
restore verified: agent-20260911T173239Z.dump is usable
```

Roughly a minute to dump, forty seconds to restore, at that size.

The scratch database is created by the local postgres superuser over peer
auth, because the agent's own role usually cannot `CREATE DATABASE`. That is
the only step that needs more privilege than the agent itself has.

---

## Restoring for real

Onto a box where the agent is **not running** — a restore into a database the
agent is using will fight it.

```bash
# 1. stop everything that writes
pm2 stop tektonix commit-reviewer agent-review

# 2. restore into a fresh database, then point .env at it
sudo -u postgres psql -c 'CREATE DATABASE langgraph_agent_restored OWNER langgraph_agent'
pg_restore --dbname="postgresql://langgraph_agent:...@localhost:5432/langgraph_agent_restored" \
           --no-owner --exit-on-error backups/agent-<stamp>.dump

# 3. edit .env: LANGGRAPH_PG_DSN -> ...langgraph_agent_restored
#    and make sure AUTH_SECRET_KEY is the one that went with this dump

# 4. start the agent and check it can actually see the data
pm2 start tektonix
curl -s 127.0.0.1:8100/api/health | python3 -m json.tool
pm2 start agent-review commit-reviewer
```

Restoring into a **new** database rather than over the old one means the
original is still there if the restore turns out to be the wrong dump.

### After a restore, check these by hand

- **Log in.** If 2FA rejects your code, `AUTH_SECRET_KEY` does not match the
  one the dump was taken with. Stop and find the right key; there is no way
  around it.
- **Settings → GitHub → Test** on each token. A token that fails to decrypt is
  the same key mismatch.
- **Open a recent task.** Its stream, plan and diff come from the checkpoints.
- **The projects themselves are not in the dump.** They are git checkouts on
  disk (`/home/<project>`), plus `projects.json`, deploy keys under `keys/`,
  and `services/commit-reviewer/review-secrets/`. Those need their own copy —
  the database knows a project by name, not by content.

---

## What is not backed up here

| Not in the dump | Where it lives | What to do |
|---|---|---|
| The projects' code | `/home/<project>` git checkouts | they have a remote; that is the backup |
| `projects.json`, deploy keys, review secrets | on disk in the install | copy them with `.env` |
| Router config and pins | `services/model-router/config.yaml` | **not in git** — gitignored since the Models page rewrites it on every repin. Keep a copy; `config.example.yaml` is only a starting point |
| Logs (`routing.jsonl`, pm2 logs) | on disk | rotate them; they are evidence, not state |
