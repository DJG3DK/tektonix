#!/usr/bin/env python
"""Check this installation's configuration, without ever printing a secret.

Six files and two directories have to agree with each other before anything
works, and when they disagree the symptom appears somewhere else entirely: a
mismatched router key looks like every model call failing, a mismatched review
secret looks like a task that builds, reviews, and is then refused at the
merge -- after you have paid for it.

    .venv/bin/python scripts/doctor.py           # check everything
    .venv/bin/python scripts/doctor.py --quiet   # only problems

Exit code is 0 when nothing FAILED (warnings do not fail), 1 otherwise, so it
is usable from cron or a deploy script.

WHAT IT NEVER DOES: print, log or compare-by-echoing a secret's value. Two
secrets are compared by hashing both and comparing the hashes; a key's
validity is reported as its decoded length. If you can see a secret in this
output, that is a bug.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(os.environ.get("AGENT_HOME") or Path(__file__).resolve().parent.parent)

OK, WARN, FAIL = "ok", "warn", "fail"

# Every file that holds a secret, and who reads it. The docstring of each
# check says what breaks when it is wrong -- that is the part worth writing
# down, because the failure never points here.
AGENT_ENV = ROOT / ".env"
ROUTER_ENV = ROOT / "services/model-router/.env"
SHARED_ENV = ROOT / "services/shared/.env"
PROJECTS_JSON = ROOT / "projects.json"
KEYS_DIR = Path(os.environ.get("AGENT_KEYS_DIR") or (ROOT / "keys"))
REVIEW_SECRETS = ROOT / "services/commit-reviewer/review-secrets"

EXPECTED_PM2_APPS = ("tektonix", "model-router", "agent-review", "commit-reviewer")


@dataclass
class Report:
    findings: list[tuple[str, str, str]] = field(default_factory=list)

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.findings.append((level, name, detail))

    ok = lambda self, n, d="": self.add(OK, n, d)          # noqa: E731
    warn = lambda self, n, d="": self.add(WARN, n, d)      # noqa: E731
    fail = lambda self, n, d="": self.add(FAIL, n, d)      # noqa: E731

    @property
    def failed(self) -> bool:
        return any(level == FAIL for level, _, _ in self.findings)


def read_env(path: Path) -> dict[str, str]:
    """KEY=value lines. Values are returned so checks can hash or measure
    them; no caller may print one."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def fingerprint(value: str) -> str:
    """Eight hex characters of a salted digest: enough to say "these two are
    the same" or "these two differ", useless for recovering the value."""
    return hashlib.blake2b(("tektonix-doctor:" + value).encode(), digest_size=4).hexdigest()


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------

def check_file_modes(report: Report) -> None:
    """A secret file readable by everyone on the box is a secret everyone on
    the box has. 600 for files, 700 for directories."""
    for path in (AGENT_ENV, ROUTER_ENV, SHARED_ENV):
        rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
        if not path.exists():
            (report.fail if path is AGENT_ENV else report.warn)(f"{rel} missing")
            continue
        mode = mode_of(path)
        if mode & 0o077:
            report.fail(f"{rel} is mode {mode:o}", f"group/other can read it -- chmod 600 {rel}")
        else:
            report.ok(f"{rel} mode {mode:o}")

    for path in (KEYS_DIR, REVIEW_SECRETS):
        if not path.exists():
            report.ok(f"{path.name}/ not used on this box")
            continue
        mode = mode_of(path)
        if mode & 0o077:
            report.fail(f"{path.name}/ is mode {mode:o}", f"chmod 700 {path}")
        else:
            report.ok(f"{path.name}/ mode {mode:o}")
        bad = [p.name for p in path.rglob("*") if p.is_file() and mode_of(p) & 0o077]
        if bad:
            report.fail(f"{path.name}/ has world/group-readable files", f"{len(bad)}: {', '.join(bad[:4])}")


def check_agent_env(report: Report) -> None:
    """The agent will not start without these, and the error it gives is a
    bare KeyError at import time."""
    env = read_env(AGENT_ENV)
    if not env:
        report.fail(".env unreadable or empty")
        return
    required = ["LANGGRAPH_PG_DSN", "MODEL_ROUTER_URL", "MODEL_ROUTER_KEY",
                "AUTH_SECRET_KEY", "ADMIN_EMAIL"]
    missing = [k for k in required if not env.get(k)]
    if missing:
        report.fail(".env is missing required values", ", ".join(missing))
    else:
        report.ok(".env has every required value")

    # AUTH_SECRET_KEY: the one whose failure mode is delayed and brutal --
    # wrong length works until the first 2FA setup, then bricks admin
    # onboarding, and rotating it makes every stored token undecryptable.
    key = env.get("AUTH_SECRET_KEY", "")
    if key:
        try:
            raw = base64.urlsafe_b64decode(key)
        except Exception:  # noqa: BLE001
            report.fail("AUTH_SECRET_KEY is not urlsafe-base64",
                        'regenerate: python -c "import base64,secrets;'
                        'print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"')
        else:
            if len(raw) in (16, 24, 32):
                report.ok(f"AUTH_SECRET_KEY decodes to {len(raw)} bytes")
            else:
                report.fail(f"AUTH_SECRET_KEY decodes to {len(raw)} bytes",
                            "AES-GCM needs 16, 24 or 32 -- `openssl rand -hex 32` gives 48 and will not work")


def check_router_pairing(report: Report) -> None:
    """MODEL_ROUTER_KEY (agent) must equal MODEL_ROUTER_KEY (router), or
    every model call comes back 401 and the dashboard looks broken."""
    agent_key = read_env(AGENT_ENV).get("MODEL_ROUTER_KEY", "")
    master = read_env(ROUTER_ENV).get("MODEL_ROUTER_KEY", "")
    if not agent_key or not master:
        report.warn("cannot compare the router key", "one side is missing")
        return
    if agent_key == master:
        report.ok("router key matches", f"both {fingerprint(agent_key)}")
    else:
        report.fail("router key MISMATCH",
                    f"agent {fingerprint(agent_key)} vs router {fingerprint(master)} -- every model call will 401")

    if not read_env(ROUTER_ENV).get("OPENROUTER_API_KEY"):
        report.fail("services/model-router/.env has no OPENROUTER_API_KEY", "the router has nothing to call")


def check_review_secret(report: Report) -> None:
    """The agent sends it, the two Node services check it. A mismatch is the
    expensive one: the task builds and reviews, then the merge is refused."""
    agent_secret = read_env(AGENT_ENV).get("REVIEW_CONTROL_SECRET", "")
    shared = read_env(SHARED_ENV).get("REVIEW_CONTROL_SECRET", "")
    legacy = read_env(ROUTER_ENV).get("REVIEW_CONTROL_SECRET", "")

    if legacy:
        report.warn("REVIEW_CONTROL_SECRET is still in services/model-router/.env",
                    "move it to services/shared/.env -- the model proxy should not carry it")
    if not agent_secret:
        report.fail(".env has no REVIEW_CONTROL_SECRET", "every merge will be refused")
        return
    service_side = shared or legacy
    if not service_side:
        report.fail("no REVIEW_CONTROL_SECRET for the services",
                    "expected in services/shared/.env")
        return
    if agent_secret == service_side:
        report.ok("review secret matches", f"both {fingerprint(agent_secret)}")
    else:
        report.fail("review secret MISMATCH",
                    f"agent {fingerprint(agent_secret)} vs services {fingerprint(service_side)} -- merges will be refused")


def check_projects(report: Report) -> None:
    """A project whose live or sandbox path is wrong fails at the first tool
    call, or -- worse -- merges into the wrong place."""
    if not PROJECTS_JSON.exists():
        report.warn("projects.json missing", "no projects onboarded yet")
        return
    try:
        data = json.loads(PROJECTS_JSON.read_text())
    except Exception as e:  # noqa: BLE001
        report.fail("projects.json does not parse", str(e)[:120])
        return
    projects = data.get("projects") or {}
    if not projects:
        report.warn("projects.json has no projects")
        return
    for name, cfg in projects.items():
        live, sandbox = Path(cfg.get("live", "")), Path(cfg.get("sandbox", ""))
        problems = []
        if not live.is_dir():
            problems.append(f"live {live} is not a directory")
        elif not (live / ".git").exists():
            problems.append(f"live {live} is not a git checkout")
        if not sandbox.is_dir():
            problems.append(f"sandbox {sandbox} is not a directory")
        if problems:
            report.fail(f"project {name}", "; ".join(problems))
        else:
            report.ok(f"project {name}", f"{live} -> {sandbox}")


def check_pm2(report: Report) -> None:
    """Which of the four processes are actually running. pm2 is optional, so
    its absence is not a failure."""
    try:
        raw = subprocess.run(["pm2", "jlist"], capture_output=True, text=True, timeout=20)
    except Exception:  # noqa: BLE001
        report.ok("pm2 not installed", "fine if you run the processes another way")
        return
    if raw.returncode != 0:
        report.warn("pm2 jlist failed", raw.stderr.strip()[:120])
        return
    try:
        apps = {a["name"]: a["pm2_env"]["status"] for a in json.loads(raw.stdout)}
    except Exception:  # noqa: BLE001
        report.warn("pm2 output could not be read")
        return
    for name in EXPECTED_PM2_APPS:
        status = apps.get(name)
        if status is None:
            report.warn(f"{name} is not in pm2", "start it, or run it another way")
        elif status != "online":
            report.fail(f"{name} is {status}")
        else:
            report.ok(f"{name} online")


def check_dashboard(report: Report) -> None:
    """The agent serves frontend/dist. No dist, no dashboard -- the API works
    and every page is a 404, which reads as a broken deploy."""
    index = ROOT / "frontend/dist/index.html"
    if index.exists():
        assets = list((ROOT / "frontend/dist/assets").glob("*.js")) if (ROOT / "frontend/dist/assets").is_dir() else []
        report.ok("dashboard built", f"{len(assets)} bundle(s) in frontend/dist")
    else:
        report.fail("frontend/dist/index.html missing",
                    "a release tarball ships it prebuilt; from a git clone run: cd frontend && npm ci && npm run build")


def check_sandbox_image(report: Report) -> None:
    """Every bash call the coder makes runs in this image."""
    try:
        r = subprocess.run(["docker", "image", "inspect", "tektonix-sandbox:latest"],
                           capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        report.fail("docker not available", "the coder cannot run a single shell command without it")
        return
    if r.returncode == 0:
        report.ok("sandbox image present")
        _check_sandbox_tools(report)
    else:
        report.fail("sandbox image tektonix-sandbox:latest missing",
                    "docker build -t tektonix-sandbox:latest docker/agent-sandbox/")


def _check_sandbox_tools(report: Report) -> None:
    """Can every configured check actually RUN in that image?

    The reviewer runs checks inside the sandbox now (SECURITY.md), and the
    image carries Node and Python. agent/provisioning.py cheerfully detects
    and configures checks for Go, Rust, Ruby, Java, .NET, PHP and Elixir --
    none of which are in it. Without this the operator finds out at their
    first merge, when every check on a perfectly good commit comes back as a
    setup error.

    A warning, never a failure: a project whose toolchain is missing is a
    project that needs the image extended, not an installation that is
    broken. The script says which tool and which checks.
    """
    import subprocess  # noqa: PLC0415

    script = ROOT / "scripts" / "check_sandbox_tools.js"
    if not script.is_file():
        return
    try:
        r = subprocess.run(["node", str(script)], capture_output=True, text=True,
                           timeout=180, cwd=str(ROOT), check=False)
    except (OSError, subprocess.SubprocessError):
        return
    if r.returncode == 0:
        report.ok("every configured check can run in the sandbox")
        return
    missing = [ln.strip() for ln in (r.stdout or "").splitlines() if "needed by" in ln]
    report.warn("a configured check needs a tool the sandbox image lacks",
                "; ".join(missing)[:400] or "run scripts/check_sandbox_tools.js")


def check_capabilities(report: Report) -> None:
    """The optional halves of this installation, and whether each one is
    here. Never a failure: every one of these is absent on a perfectly
    healthy box, and a doctor that fails over an unused feature is a doctor
    people stop running. It is here because "the feature did nothing" and
    "the feature is not installed" look identical from the dashboard."""
    import sys  # noqa: PLC0415

    sys.path.insert(0, str(ROOT))
    from agent.capabilities import CAPABILITIES  # noqa: PLC0415

    for cap in CAPABILITIES:
        if cap.available():
            report.ok(f"{cap.name} available", cap.provides)
        else:
            report.warn(f"{cap.name} not available", f"{cap.provides} -- {cap.hint}" if cap.hint else cap.provides)


CHECKS = (
    check_file_modes,
    check_agent_env,
    check_router_pairing,
    check_review_secret,
    check_projects,
    check_dashboard,
    check_sandbox_image,
    check_pm2,
    check_capabilities,
)


def run_all() -> Report:
    report = Report()
    for check in CHECKS:
        try:
            check(report)
        except Exception as e:  # noqa: BLE001 -- one broken check must not hide the rest
            report.fail(f"{check.__name__} raised", f"{type(e).__name__}: {str(e)[:120]}")
    return report


_SECRET_SHAPE = re.compile(r"(sk-[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|[A-Za-z0-9+/=_-]{40,})")


def render(report: Report, quiet: bool = False) -> str:
    marks = {OK: "  ok  ", WARN: " warn ", FAIL: " FAIL "}
    lines = []
    for level, name, detail in report.findings:
        if quiet and level == OK:
            continue
        lines.append(f"[{marks[level]}] {name}" + (f"\n           {detail}" if detail else ""))
    counts = {level: sum(1 for x, _, _ in report.findings if x == level) for level in (OK, WARN, FAIL)}
    lines.append(f"\n{counts[OK]} ok, {counts[WARN]} warning(s), {counts[FAIL]} failure(s)")
    if counts[FAIL]:
        lines.append("Fix the failures above; each one breaks something that fails somewhere else.")
    out = "\n".join(lines)
    # Belt and braces: this tool's entire promise is that it does not print
    # secrets, so it checks its own output before handing it over.
    leak = _SECRET_SHAPE.search(out)
    if leak:
        return ("doctor: refusing to print its own report -- it contains something shaped like a "
                f"secret ({len(leak.group(0))} characters). This is a bug in scripts/doctor.py.")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Check this installation's configuration.")
    parser.add_argument("--quiet", action="store_true", help="only warnings and failures")
    args = parser.parse_args()
    report = run_all()
    print(render(report, quiet=args.quiet))
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
