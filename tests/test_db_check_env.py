"""The host database checks run agent-authored scripts with a SEALED env.

db:drift / db:seed / test:e2e still run on the host (SECURITY.md, "The
database checks, which stay on the host"). Until 2026-09-23 they went through
run(), which spreads process.env underneath, while the comment above them and
SECURITY.md both said the env was built rather than inherited -- so a script
in the repository under review could read the reviewer's router key and
control secret. This runs the real runDatabaseCheck with stand-in psql and
redis-cli, plants both secrets in the reviewer's environment, and requires
that neither reaches any child.
"""
import json
import os
import stat
import subprocess
import textwrap

from agent import paths

REVIEWER = paths.REPO_ROOT / "services" / "commit-reviewer" / "reviewer.js"


def _exe(path, body):
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_db_check_children_do_not_inherit_the_reviewers_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    # psql and redis-cli: succeed, and record what they were given too.
    for tool in ("psql", "redis-cli"):
        _exe(bin_dir / tool, f"#!/bin/sh\nenv > {dumps}/{tool}.$$.env\nexit 0\n")
    # The project's own scripts: each dumps its environment as JSON.
    _exe(bin_dir / "dump-env", textwrap.dedent(f"""\
        #!/usr/bin/env node
        require('fs').writeFileSync('{dumps}/' + process.argv[2] + '.json', JSON.stringify(process.env));
    """))

    worktree = tmp_path / "wt"
    api = worktree / "apps" / "api"
    api.mkdir(parents=True)
    (api / ".env").write_text('DATABASE_URL="postgresql://u:pw@localhost:5432/app?schema=public"\n')

    cfg = {"databaseCheck": {
        "apiDir": "apps/api",
        "driftCmd": {"cmd": "dump-env", "args": ["drift"]},
        "seedCmd": {"cmd": "dump-env", "args": ["seed"]},
        "e2eCmd": {"cmd": "dump-env", "args": ["e2e"]},
    }}
    script = (f"const r=require({json.dumps(str(REVIEWER))});"
              f"r.runDatabaseCheck({json.dumps(cfg)}, {json.dumps(str(worktree))})"
              f".then(x=>console.log(JSON.stringify(x)))")
    env = {**os.environ,
           "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "OPENROUTER_API_KEY": "sk-planted-router-key",
           "REVIEW_CONTROL_SECRET": "planted-control-secret",
           "SMTP_PASSWORD": "planted-smtp"}
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60,
                         cwd=str(REVIEWER.parent), env=env)
    assert out.returncode == 0, out.stderr
    results = json.loads(out.stdout.strip().splitlines()[-1])
    assert [r["name"] for r in results] == ["db-drift", "db-seed", "e2e"]
    assert all(r["ok"] for r in results), results

    for step in ("drift", "seed", "e2e"):
        child = json.loads((dumps / f"{step}.json").read_text())
        for planted in ("OPENROUTER_API_KEY", "REVIEW_CONTROL_SECRET", "SMTP_PASSWORD"):
            assert planted not in child, f"{step} inherited {planted}"
        # ...while what the check genuinely needs is there.
        assert child["DATABASE_URL"].startswith("postgresql://u:pw@localhost:5432/steals_ci_review_")
        assert child["REDIS_URL"].endswith("/15")
        assert child["CI"] == "true"

    psql_env = next(dumps.glob("psql.*.env")).read_text()
    assert "planted-router-key" not in psql_env and "PGPASSWORD=pw" in psql_env
