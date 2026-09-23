# Two-factor: a lost authenticator

**What you see.** The sign-in asks for a two-factor code and the person no
longer has the app that makes them — a new phone, a wiped one, a deleted
entry.

**What to check first.** A code that is simply *rejected* is usually the
phone's clock: TOTP codes are time-based, and a phone more than about a
minute off produces codes the server never accepts. Set the phone to network
time and try again before anything below.

## Sign in with a recovery code

Every account that turned 2FA on was shown ten recovery codes once, looking
like `a1b2c3-d4e5f6`. Type or paste one into the same two-factor box the
6-digit code goes in. Each works once.

- **A regular user** can then turn 2FA off and on again under Settings →
  Account, which issues a new secret and a fresh set of recovery codes.
- **An admin** cannot turn 2FA off — it is required on admin accounts — so
  the way to a new authenticator is the reset below, after which the next
  sign-in walks them through setup again.

## No recovery codes left: reset it on the server

This is a database operation on purpose. Dropping a second factor is exactly
what a stolen session would want, so there is no button for it that a session
alone can press.

```bash
cd "${AGENT_HOME:-/home/3d-agent}"   # wherever install.sh put the agent -- see README.md
set -a; . ./.env; set +a
EMAIL='person@example.com'
psql "$LANGGRAPH_PG_DSN" -v email="$EMAIL" <<'SQL'
UPDATE agent_users
   SET totp_enabled = FALSE, totp_secret_enc = NULL, totp_last_used_step = NULL
 WHERE email = :'email';
DELETE FROM agent_recovery_codes
 WHERE user_id = (SELECT id FROM agent_users WHERE email = :'email');
SQL
```

That is what `/api/auth/2fa/disable` does (`agent/auth.py`, `disable_totp`).
For an admin, the next sign-in is sent straight to 2FA setup — the forced
screen does not let an admin past without it.

**If the authenticator was lost because the device was stolen**, also end
every session that account has, so a phone that is still signed in stops
being:

```bash
psql "$LANGGRAPH_PG_DSN" -v email="$EMAIL" \
  -c "DELETE FROM agent_sessions WHERE user_id = (SELECT id FROM agent_users WHERE email = :'email')"
```
