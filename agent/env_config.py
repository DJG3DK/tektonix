"""Editing the credentials the deployment runs on, from the Settings page.

Why this is narrow on purpose
-----------------------------
This writes secrets to disk from an HTTP request, so the surface is kept as
small as it can be while still doing the job:

* **An allow-list, not arbitrary keys.** Only the entries in MANAGED_KEYS can be
  read or written. A caller cannot invent a variable name and have it land in a
  .env, and cannot reach a key this module does not know about — notably
  AUTH_SECRET_KEY, the AES-GCM key encrypting TOTP 2FA secrets at rest (whose
  rotation makes every stored 2FA secret permanently undecryptable, locking
  every 2FA user out), and LANGGRAPH_PG_DSN, where a wrong value takes the whole
  agent down rather than degrading it.
* **Values are never returned.** Reads give a masked hint (last four characters)
  and whether the key is set. There is no endpoint that hands back a secret, so
  a compromised session cannot exfiltrate one it did not already have.
* **Never logged.** Errors quote the key NAME only.
* **Atomic writes.** Write a temp file in the same directory, fsync, rename.
  A crash mid-write leaves the old file intact rather than a truncated .env that
  would fail to parse and take the service down on next start.
* **Permissions preserved.** The .env keeps its existing mode (0600), and a
  newly created one is created 0600 rather than inheriting a default umask.
"""
from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

AGENT_ENV = Path(__file__).resolve().parent.parent / ".env"
ROUTER_ENV = Path(__file__).resolve().parent.parent / "services" / "llm-router" / ".env"


@dataclass(frozen=True)
class ManagedKey:
    key: str
    path: Path
    label: str
    help: str
    group: str
    # Which services must restart for a change to take effect. Surfaced to the
    # user rather than silently applied: restarting the router interrupts every
    # in-flight model call, so it is their call when that happens.
    restarts: tuple[str, ...]
    secret: bool = True


MANAGED_KEYS: tuple[ManagedKey, ...] = (
    ManagedKey(
        "OPENROUTER_API_KEY", ROUTER_ENV, "OpenRouter API key",
        "Pays for every model call. The router resolves each role's alias to a model and bills this key.",
        "Models", ("llm-router",),
    ),
    ManagedKey(
        "MODEL_ROUTER_KEY", ROUTER_ENV, "Router master key",
        "The router's own auth. Every service that calls it presents this. Changing it requires updating "
        "MODEL_ROUTER_KEY below to match, or the agent and reviewer lose access.",
        "Models", ("llm-router", "tektonix", "commit-reviewer", "agent-review"),
    ),
    ManagedKey(
        "MODEL_ROUTER_KEY", AGENT_ENV, "Router key (agent side)",
        "What the agent presents to the router. Must equal the master key above.",
        "Models", ("tektonix",),
    ),
    ManagedKey(
        "LANGSMITH_API_KEY", AGENT_ENV, "LangSmith API key",
        "Tracing. Optional — leave empty to disable. Traces are redacted before they leave the box.",
        "Tracing", ("tektonix",),
    ),
    ManagedKey(
        "LANGSMITH_PROJECT", AGENT_ENV, "LangSmith project",
        "Which LangSmith project traces are filed under.",
        "Tracing", ("tektonix",), secret=False,
    ),
    ManagedKey(
        "LANGSMITH_TRACING", AGENT_ENV, "Tracing enabled",
        "true or false. Off means no trace data leaves this machine at all.",
        "Tracing", ("tektonix",), secret=False,
    ),
    ManagedKey("SMTP_HOST", AGENT_ENV, "SMTP host", "Outbound mail for password resets.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_PORT", AGENT_ENV, "SMTP port", "Usually 587 for STARTTLS.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_USER", AGENT_ENV, "SMTP user", "Usually the sending address.", "Email", ("tektonix",), secret=False),
    ManagedKey("SMTP_PASS", AGENT_ENV, "SMTP password", "App password, not the account password.", "Email", ("tektonix",)),
    ManagedKey("SMTP_FROM", AGENT_ENV, "From address", "What recipients see.", "Email", ("tektonix",), secret=False),
)

_BY_KEY = {k.key: k for k in MANAGED_KEYS}


def _read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def _mask(value: str) -> str:
    """A hint, not the secret. Short values reveal nothing at all rather than
    most of themselves — masking a 6-character value as ``ab…ef`` would give
    away two thirds of it."""
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return f"{'•' * 8}{value[-4:]}"


def list_keys() -> list[dict]:
    """Masked view for the Settings page. Never returns a value."""
    out = []
    for mk in MANAGED_KEYS:
        env = _read_env(mk.path)
        raw = env.get(mk.key, "")
        out.append({
            "key": mk.key,
            "label": mk.label,
            "help": mk.help,
            "group": mk.group,
            "secret": mk.secret,
            "is_set": bool(raw),
            # Non-secret values (host, port, project name) are shown in full —
            # masking a hostname helps nobody and makes the page unusable.
            "display": _mask(raw) if mk.secret else raw,
            "restarts": list(mk.restarts),
            "file": str(mk.path),
        })
    return out


class InvalidValueError(Exception):
    pass


def _format_value(value: str) -> str:
    """Render a value for a .env line. audit M-3: reject embedded newlines /
    carriage returns / NULs (which previously let a managed, non-secret key's
    value smuggle a second `KEY=...` line -- e.g. AUTH_SECRET_KEY or
    LANGGRAPH_PG_DSN -- past the key allow-list, since dotenv resolves the
    last occurrence). Quote and escape anything with shell/dotenv-significant
    characters so the written line round-trips to exactly this value.
    """
    if any(c in value for c in ("\n", "\r", "\x00")):
        raise InvalidValueError("value may not contain newlines or NUL")
    needs_quote = value == "" or any(c in value for c in (' ', '\t', '#', '"', "'", '=', '$', '`', '\\'))
    if not needs_quote:
        return value
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _write_env(path: Path, updates: dict[str, str]) -> None:
    """Rewrite `path` with `updates` applied, preserving order, comments and mode."""
    try:
        original = path.read_text()
        existed = True
    except FileNotFoundError:
        original = ""
        existed = False

    lines = original.splitlines()
    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k = stripped.partition("=")[0].strip()
        if k in updates:
            lines[i] = f"{k}={_format_value(updates[k])}"
            seen.add(k)
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={_format_value(v)}")

    body = "\n".join(lines).rstrip("\n") + "\n"

    # Same directory so the rename is atomic (a cross-filesystem rename is a
    # copy, which reintroduces the truncated-file window this avoids).
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, os.stat(path).st_mode & 0o777 if existed else 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class UnknownKeyError(Exception):
    pass


def set_keys(updates: dict[str, str]) -> dict:
    """Apply updates. Returns which services need restarting.

    Raises UnknownKeyError naming the key if anything outside the allow-list is
    passed — the name is safe to echo, the value never is.
    """
    unknown = [k for k in updates if k not in _BY_KEY]
    if unknown:
        raise UnknownKeyError(f"not editable here: {', '.join(sorted(unknown))}")

    # audit M-3: validate every value BEFORE writing any file, so one bad value
    # can't leave a partial multi-file update behind.
    for k, v in updates.items():
        if any(c in v for c in ("\n", "\r", "\x00")):
            raise InvalidValueError(f"value for {k} may not contain newlines or NUL")

    by_file: dict[Path, dict[str, str]] = {}
    restarts: set[str] = set()
    for k, v in updates.items():
        mk = _BY_KEY[k]
        by_file.setdefault(mk.path, {})[k] = v
        restarts.update(mk.restarts)

    for path, vals in by_file.items():
        _write_env(path, vals)

    return {"updated": sorted(updates), "restart_required": sorted(restarts)}
