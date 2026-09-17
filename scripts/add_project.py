"""Add a project from the command line -- the wizard's headless twin.

Same three phases as the dashboard wizard (inspect -> confirm -> provision)
and the same module underneath (agent/provisioning.py), so a headless install
cannot drift from what the UI does.

    .venv/bin/python scripts/add_project.py /path/to/repo
    .venv/bin/python scripts/add_project.py /path/to/repo --yes

--yes accepts the RECOMMENDED answers, which deliberately means: proposed
secret files on, fixture mounts off, and any test script that makes network
calls left OFF. It never auto-enables something this tool could not verify --
see agent/provisioning.py for why that distinction is the whole point.

The project must sit inside AGENT_PROJECT_ROOTS (default /home) and its name
is the directory's own basename; the worktree location comes from
AGENT_SANDBOX_ROOT. Same containment rules as the dashboard wizard, because
both call the same module.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import _PROJECTS_CONFIG_PATH, PROJECTS  # noqa: E402
from agent import provisioning as prov  # noqa: E402


def _ask(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"  {question} {suffix} ").strip().lower()
    except EOFError:
        return default
    if not ans:
        return default
    return ans in ("y", "yes")


def _choose(items, assume_yes: bool, label: str) -> list[str]:
    chosen = []
    if not items:
        return chosen
    print(f"\n{label}")
    for c in items:
        print(f"  - {c.value}\n      {c.reason}")
        if c.warning:
            print(f"      ! {c.warning}")
        chosen.append(c.value) if (c.enabled if assume_yes else _ask(f"include {c.value}?", c.enabled)) else None
    return chosen


def main() -> int:
    ap = argparse.ArgumentParser(description="Onboard a project for the agent")
    ap.add_argument("path", help="absolute path to the live repo")
    ap.add_argument("--sandbox-root", default=None,
                    help="override AGENT_SANDBOX_ROOT for this run")
    ap.add_argument("--yes", action="store_true", help="accept recommended answers")
    args = ap.parse_args()

    try:
        report = prov.detect_project(args.path, sandbox_root=args.sandbox_root,
                                     existing_names=list(PROJECTS))
    except prov.ProvisioningError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    name = report.name
    print(f"\n{name}")
    print(f"  live      {report.live}")
    print(f"  worktree  {report.sandbox}")
    print(f"  stack     {', '.join(report.languages) or 'unknown'}"
          + (f" ({report.package_manager})" if report.package_manager else ""))

    for b in report.blockers:
        print(f"  BLOCKER   {b}")
    if report.blockers:
        return 1
    for w in report.warnings:
        print(f"  warning   {w}")

    print("\nChecks the review gate will run:")
    for c in report.checks:
        print(f"  {c['name']:<12} {c['cmd']} {' '.join(c['args'])}")
    if not report.checks:
        print("  (none detected)")

    if args.yes:
        # The recommended answers live in provisioning so the "new project"
        # endpoint and this script cannot disagree about what --yes means.
        choices = prov.recommended_choices(report)
        for label, items in (
            ("Secret files to copy into review checkouts:", report.secret_files),
            ("Read-only fixture mounts:", report.read_only_mounts),
            ("pm2 apps to restart on deploy:", report.pm2_apps),
            ("Test scripts that make network calls (off unless you confirm):", report.risky_scripts),
        ):
            _choose(items, True, label)     # prints what --yes accepted
    else:
        secrets = _choose(report.secret_files, False,
                          "Secret files to copy into review checkouts:")
        mounts = _choose(report.read_only_mounts, False,
                         "Read-only fixture mounts:")
        apps = _choose(report.pm2_apps, False, "pm2 apps to restart on deploy:")
        risky = _choose(report.risky_scripts, False,
                        "Test scripts that make network calls (off unless you confirm):")
        checks = list(report.checks) + [
            {"name": r, "dir": ".", "cmd": report.package_manager or "npm",
             "args": ["run", r], "timeoutMs": prov.TEST_TIMEOUT_MS_DEFAULT}
            for r in risky
        ]
        choices = {
            "secret_files": secrets, "read_only_mounts": mounts, "pm2_apps": apps,
            "node_modules_dirs": report.node_modules_dirs,
            "dependency_dirs": report.dependency_dirs, "checks": checks,
            "build_steps": report.build_steps, "db_env_file": report.db_env_file,
        }
    secrets, apps, checks = choices["secret_files"], choices["pm2_apps"], choices["checks"]

    if not args.yes and not _ask(f"\ncreate project {name!r}?", True):
        print("aborted")
        return 1

    ok, detail = prov.create_worktree(report.live, report.sandbox)
    print(f"  worktree  {'ok' if ok else 'FAILED'}: {detail}")
    if not ok:
        return 1

    entry = prov.config_from_choices(name, report.live, report.sandbox, choices)
    try:
        prov.write_project_entry(_PROJECTS_CONFIG_PATH, name, entry)
    except prov.ProvisioningError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"  config    wrote {name} to {_PROJECTS_CONFIG_PATH}")
    print(f"  live      {report.live}")
    print(f"  sandbox   {report.sandbox}")
    print(f"  pm2 apps  {', '.join(apps) or '-'}")
    print(f"  checks    {', '.join(c['name'] for c in checks) or '-'}")
    print(f"  secrets   {len(secrets)} file(s) (names in {_PROJECTS_CONFIG_PATH})")
    print("\nNext:")
    print(f"  .venv/bin/python scripts/run_cartographer.py {name}   # build its codebase map")
    print("  .venv/bin/python scripts/seed_memory.py               # seed project memory")
    print("  pm2 restart tektonix                                   # the running agent re-reads projects.json")
    print("  (the two review services need no restart: they re-read projects.json on")
    print("   every poll. The agent process only reloads it when the DASHBOARD adds a")
    print("   project, so a write from this script needs the restart above.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
