#!/usr/bin/env python
"""Print the first-run admin password once, then remove the file.

agent/server.py stores the generated password for the seeded admin account
encrypted with AUTH_SECRET_KEY in `data/.initial-admin-password` (mode
0600) -- never in the log. Host install, from the repo root:

    .venv/bin/python scripts/show_initial_password.py

Compose bundle:

    docker compose exec agent python scripts/show_initial_password.py

`docker compose exec` does not run the entrypoint, so the signing key the
entrypoint derives is not in the environment there; this reads it from the
data volume the entrypoint wrote it to. Nothing here touches the database.

The password must be changed on first login; the file is deleted after it
has been shown, so this works exactly once.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import auth, paths  # noqa: E402

PATH = paths.DATA_DIR / ".initial-admin-password"
LEGACY_PATH = paths.REPO_ROOT / ".initial-admin-password"   # where it sat before 2026-09-28
KEY_FILE = paths.DATA_DIR / "auth_secret_key"               # the bundle's entrypoint writes it here


def secret_key() -> str | None:
    key = os.environ.get("AUTH_SECRET_KEY", "").strip()
    if key:
        return key
    try:
        return KEY_FILE.read_text().strip() or None
    except OSError:
        return None


def main() -> int:
    path = PATH if PATH.exists() else LEGACY_PATH
    if not path.exists():
        print(f"no {PATH.name} file: the initial password was already shown, or no admin was ever seeded", file=sys.stderr)
        return 1
    key = secret_key()
    if not key:
        print(f"AUTH_SECRET_KEY is not set and {KEY_FILE} does not exist; run this with the agent's .env", file=sys.stderr)
        return 2
    try:
        password = auth._decrypt_totp_secret(SimpleNamespace(auth_secret_key=key), path.read_text().strip())
    except Exception:  # noqa: BLE001
        print("could not decrypt: AUTH_SECRET_KEY differs from the one the agent booted with", file=sys.stderr)
        return 2
    print(password)
    path.unlink()
    print(f"({path.name} removed; change this password on first login)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
