"""What this deployment is licensed for.

The public build is the single-operator product. Accounts beyond the first
are a licensed feature: the routes that mint them refuse, and the page that
would offer them is not shown. A licensed deployment names its features in
TEKTONIX_FEATURES, comma-separated. Existing accounts always keep working:
the switch is on making new ones, never on signing in.
"""
from __future__ import annotations

import os

MULTI_USER = "multi-user"
_ENV = "TEKTONIX_FEATURES"
_KNOWN = (MULTI_USER,)


def enabled_set() -> frozenset[str]:
    raw = os.environ.get(_ENV, "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


def enabled(name: str) -> bool:
    return name in enabled_set()


def public() -> dict[str, bool]:
    """For the dashboard: which licensed features this deployment has."""
    on = enabled_set()
    return {name.replace("-", "_"): name in on for name in _KNOWN}
