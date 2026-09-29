#!/usr/bin/env python
"""One value from a .env file, parsed the way the agent parses it.

    .venv/bin/python scripts/env_value.py LANGGRAPH_PG_DSN [path/to/.env]

For shell scripts that need a setting and used to `. ./.env` for it. A
value saved from the Settings page may contain `$(...)` or a backtick,
and sourcing the file ran it; an SMTP password with a `$` in it came out
mangled. This prints the value and nothing else, or nothing when the key
or the file is absent, so `"$(env_value.py KEY)"` is the whole idiom.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import paths  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__.strip(), file=sys.stderr)
        return 2
    key = argv[1]
    path = Path(argv[2]) if len(argv) == 3 else paths.REPO_ROOT / ".env"
    if not path.exists():
        return 0
    try:
        from dotenv import dotenv_values
        value = dotenv_values(path).get(key) or ""
    except ImportError:  # a system python without the agent's venv: a plain KEY=value read
        value = ""
        for line in path.read_text().splitlines():
            if line.startswith(f"{key}="):
                value = line.split("=", 1)[1].strip().strip("\"'")
    sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
