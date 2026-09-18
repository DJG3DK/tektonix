import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


@dataclass(frozen=True)
class Config:
    pg_dsn: str
    router_base_url: str
    router_api_key: str
    default_budget_usd: float
    api_port: int
    langsmith_tracing: bool
    auth_secret_key: str
    cors_allow_origins: list[str]
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_pass: str
    smtp_from: str
    admin_email: str
    # Optional: fine-grained, read-only, selected repos. Enables the GitHub
    # pull-request tools (agent/tools/github_tools.py); absent = tools absent.
    github_token: str | None = None


def load_config() -> Config:
    return Config(
        pg_dsn=os.environ["LANGGRAPH_PG_DSN"],
        router_base_url=os.environ["MODEL_ROUTER_URL"],
        router_api_key=os.environ["MODEL_ROUTER_KEY"],
        default_budget_usd=float(os.environ.get("DEFAULT_BUDGET_USD", "2.00")),
        api_port=int(os.environ.get("API_PORT", "8100")),
        langsmith_tracing=os.environ.get("LANGSMITH_TRACING", "").lower() == "true",
        auth_secret_key=os.environ["AUTH_SECRET_KEY"],
        # Same-origin by default -- server.py serves the frontend itself, so
        # production needs no CORS at all. Set CORS_ALLOW_ORIGINS (comma-
        # separated) only for a split dev setup with Vite on its own port.
        cors_allow_origins=[o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "").split(",") if o.strip()],
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.environ["SMTP_PORT"]),
        smtp_user=os.environ["SMTP_USER"],
        smtp_pass=os.environ["SMTP_PASS"],
        smtp_from=os.environ["SMTP_FROM"],
        admin_email=os.environ.get("ADMIN_EMAIL", "admin@example.com"),
        github_token=os.environ.get("GITHUB_TOKEN") or None,
    )


# Deployment-specific: which repos this agent can target, and each one's
# workspace. Since the 2026-08-25 migration the "sandbox" key names a git
# WORKTREE of the live repo (/home/agent-workspaces/<name>), not a separate
# clone — the agent commits to a per-task branch there, which is a plain local
# ref in the live repo, and the review service reads that branch directly.
# There is no git remote between them any more; the key keeps its old name so
# existing projects.json files stay valid. One task runs at a time per project
# (the per-project lock in graph.py), so the workspace is never contended.
#
# Loaded from projects.json (gitignored, deployment-specific) with
# projects.example.json as the committed template.

# AGENT_PROJECTS_JSON is what lets the container bundle put this file on a
# shared volume: the agent writes it from onboarding and the two review
# services read it, and in the bundle those are three separate containers. The
# Node side has read the same variable since it was written; this is the half
# that was missing. Unset on a host install, where the repo-root file is right.
_PROJECTS_CONFIG_PATH = Path(
    os.environ.get("AGENT_PROJECTS_JSON")
    or Path(__file__).resolve().parent.parent / "projects.json"
)
_PROJECTS_EXAMPLE_PATH = Path(__file__).resolve().parent.parent / "projects.example.json"


def _load_projects_config() -> dict:
    path = _PROJECTS_CONFIG_PATH if _PROJECTS_CONFIG_PATH.exists() else _PROJECTS_EXAMPLE_PATH
    with open(path) as f:
        return json.load(f)


_projects_config = _load_projects_config()
PROJECTS = _projects_config["projects"]

# Per-project test-only environment overrides — for a project whose test
# suite needs a path to something outside its own sandbox checkout (for
# example, a live config file it deliberately targets against a single real
# instance). Scoped per project; not a standing grant to every task.
PROJECT_TEST_ENV = _projects_config.get("test_env", {})


def reload_projects() -> dict:
    """Re-read projects.json IN PLACE, so a project added at runtime (the
    onboarding wizard) is visible without restarting the process.

    In place is the whole point: every consumer does
    `from agent.config import PROJECTS`, which binds this exact dict object.
    Rebinding the module global would leave all of them pointing at the old
    copy -- the newly onboarded project would exist in config.py and nowhere
    else. clear()+update() mutates the object they already hold.
    """
    global _projects_config
    _projects_config = _load_projects_config()
    PROJECTS.clear()
    PROJECTS.update(_projects_config["projects"])
    PROJECT_TEST_ENV.clear()
    PROJECT_TEST_ENV.update(_projects_config.get("test_env", {}))
    return PROJECTS
