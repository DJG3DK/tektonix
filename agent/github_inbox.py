"""The GitHub inbox: what the poller finds, what policy decides, and the
approve links the operator clicks.

Flow, per project with at least one source switched on:

  discover  -- read open PRs, Dependabot alerts, CodeQL/code-scanning alerts
               (grouped per rule), CHANGES_REQUESTED reviews, and failing
               default-branch checks (check runs, or Actions workflow runs
               when the token has only Actions: read) from the GitHub API,
               each as an inbox ITEM with a stable key and a fingerprint.
  decide    -- for an item not seen before (or whose fingerprint changed):
                 off      -> "seen"      (listed, nothing else)
                 propose  -> "proposed"  (alert with an approve link)
                 auto     -> "task_created" now, within the project's caps;
                             past the cap it is proposed instead.
  approve   -- the operator clicks Approve in the dashboard, in Telegram or
               in an email; the item becomes a task. The task runs the same
               review gate and merge approval as any other.

Items live in the store under ("github_inbox", repo), keyed by their item
key, so a PR is never proposed twice and a dismissed one stays dismissed
until it changes (a new head commit is a new fingerprint).

Approve links carry a signed, expiring token instead of a session: the
operator is on their phone, reading Telegram, and must not have to log in
to say yes. The link opens a confirmation page with a button -- a GET must
never act, because Telegram and mail clients fetch links for previews --
and the POST behind that button is single-use: once an item leaves
"proposed", every token for it is spent.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any
from collections.abc import Awaitable, Callable

import httpx

from agent import github_settings
from agent import audit
from agent.config import Config
from agent.tools.github_tools import fetch_pull_request, format_pull_request, resolve_slug

logger = logging.getLogger("tektonix")

API = "https://api.github.com"
_TIMEOUT = 20
NAMESPACE = "github_inbox"

BOT_SUFFIX = "[bot]"
DEPENDABOT = "dependabot[bot]"

# How long an approve link stays valid. Long enough to read it in the
# morning; short enough that a leaked old message is not a standing key.
APPROVAL_TTL_S = 48 * 3600

STATES = ("seen", "proposed", "task_created", "dismissed", "snoozed", "resolved")


# ---------------------------------------------------------------------------
# GitHub client -- async, thin, and the one thing tests replace
# ---------------------------------------------------------------------------

class GitHubClient:
    def __init__(self, token: str):
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{API}{path}", headers=self._headers, params=params)
        if r.status_code == 404:
            raise LookupError(f"GitHub 404 for {path} (no access, or it does not exist)")
        if r.status_code in (401, 403):
            raise PermissionError(f"GitHub refused {path} ({r.status_code}): {r.text[:160]}")
        r.raise_for_status()
        return r.json()

    async def repo(self, slug: str) -> dict:
        return await self.get(f"/repos/{slug}")

    async def open_prs(self, slug: str) -> list[dict]:
        return await self.get(f"/repos/{slug}/pulls", {"state": "open", "per_page": 50, "sort": "updated", "direction": "desc"})

    async def reviews(self, slug: str, number: int) -> list[dict]:
        return await self.get(f"/repos/{slug}/pulls/{number}/reviews", {"per_page": 50})

    async def dependabot_alerts(self, slug: str) -> list[dict]:
        return await self.get(f"/repos/{slug}/dependabot/alerts", {"state": "open", "per_page": 50})

    async def code_scanning_alerts(self, slug: str) -> list[dict]:
        """Open code scanning (CodeQL etc.) alerts. A first full analysis can
        produce a few hundred; three pages is the ceiling before the inbox
        stops being an inbox."""
        out: list[dict] = []
        for page in (1, 2, 3):
            batch = await self.get(f"/repos/{slug}/code-scanning/alerts", {"state": "open", "per_page": 100, "page": page})
            out.extend(batch)
            if len(batch) < 100:
                break
        return out

    async def check_runs(self, slug: str, ref: str) -> list[dict]:
        data = await self.get(f"/repos/{slug}/commits/{ref}/check-runs", {"per_page": 50})
        return data.get("check_runs") or []

    async def workflow_runs(self, slug: str, branch: str) -> list[dict]:
        """GitHub Actions runs on a branch, newest first. Needs "Actions: read"
        where check_runs needs "Checks: read"; a fine-grained token may have
        either, so ci_failures tries both."""
        data = await self.get(f"/repos/{slug}/actions/runs", {"branch": branch, "per_page": 30})
        return data.get("workflow_runs") or []


# ---------------------------------------------------------------------------
# items
# ---------------------------------------------------------------------------

@dataclass
class Item:
    key: str                 # "pr:12" / "alert:7" / "review:12" / "ci:<sha>:<name>" / "code:<rule>"
    kind: str                # a SOURCES name
    repo: str
    title: str
    url: str
    fingerprint: str         # changes when the thing itself changed (new head sha, new alert state)
    number: int | None = None
    author: str | None = None
    summary: str = ""
    state: str = "seen"
    mode: str = "off"        # policy mode that applied when the item was decided
    reason: str = ""
    task_id: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    snoozed_until: float | None = None
    approval_nonce: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _author_allowed(login: str | None, policy: str) -> bool:
    login = login or ""
    if policy == "anyone":
        return True
    if policy == "bots":
        return login.endswith(BOT_SUFFIX)
    return login == DEPENDABOT


async def discover(client: GitHubClient, repo: str, slug: str, proj: dict) -> list[Item]:
    """Every current item for the sources this project has switched on.
    One source failing (a token without alert permission, say) is logged
    and does not hide the others."""
    policies = proj["policies"]
    items: list[Item] = []
    prs: list[dict] | None = None

    async def _prs() -> list[dict]:
        nonlocal prs
        if prs is None:
            prs = await client.open_prs(slug)
        return prs

    if policies["dependabot_prs"] != "off":
        try:
            for pr in await _prs():
                login = (pr.get("user") or {}).get("login")
                if not _author_allowed(login, proj.get("authors", "dependabot")):
                    continue
                head = (pr.get("head") or {}).get("sha") or ""
                items.append(Item(
                    key=f"pr:{pr['number']}", kind="dependabot_prs", repo=repo, number=pr["number"],
                    title=pr.get("title") or f"PR #{pr['number']}", url=pr.get("html_url") or "",
                    fingerprint=head[:12], author=login,
                    summary=f"{login}: {(pr.get('head') or {}).get('ref')} -> {(pr.get('base') or {}).get('ref')}",
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s dependabot_prs failed: %s", repo, e)

    if policies["review_requests"] != "off":
        try:
            for pr in await _prs():
                reviews = await client.reviews(slug, pr["number"])
                # The latest review per reviewer decides; an approval after a
                # changes-requested review cancels it.
                latest: dict[str, dict] = {}
                for r in reviews:
                    who = (r.get("user") or {}).get("login") or "?"
                    latest[who] = r
                requesting = [r for r in latest.values() if r.get("state") == "CHANGES_REQUESTED"]
                if not requesting:
                    continue
                head = (pr.get("head") or {}).get("sha") or ""
                reviewers = ", ".join(sorted((r.get("user") or {}).get("login") or "?" for r in requesting))
                items.append(Item(
                    key=f"review:{pr['number']}", kind="review_requests", repo=repo, number=pr["number"],
                    title=pr.get("title") or f"PR #{pr['number']}", url=pr.get("html_url") or "",
                    fingerprint=f"{head[:12]}:{max(r.get('id', 0) for r in requesting)}",
                    author=(pr.get("user") or {}).get("login"),
                    summary=f"changes requested by {reviewers}",
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s review_requests failed: %s", repo, e)

    if policies["security_alerts"] != "off":
        try:
            for a in await client.dependabot_alerts(slug):
                adv = a.get("security_advisory") or {}
                dep = (a.get("dependency") or {}).get("package") or {}
                vuln = a.get("security_vulnerability") or {}
                sev = (adv.get("severity") or "?").upper()
                pkg = dep.get("name") or "?"
                items.append(Item(
                    key=f"alert:{a['number']}", kind="security_alerts", repo=repo, number=a["number"],
                    title=f"[{sev}] {pkg}: {adv.get('summary') or 'security alert'}",
                    url=a.get("html_url") or "", fingerprint=f"{a.get('state')}:{adv.get('ghsa_id')}",
                    summary=f"{pkg} {vuln.get('vulnerable_version_range') or ''} -> patched in "
                            f"{(vuln.get('first_patched_version') or {}).get('identifier') or '?'} "
                            f"({(a.get('dependency') or {}).get('manifest_path') or '?'})",
                ))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s security_alerts failed: %s", repo, e)

    if policies["ci_failures"] != "off":
        try:
            info = await client.repo(slug)
            branch = info.get("default_branch") or "main"
            items.extend(await _ci_failures(client, repo, slug, branch))
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s ci_failures failed: %s", repo, e)

    if policies.get("code_scanning", "off") != "off":
        try:
            items.extend(code_scanning_items(await client.code_scanning_alerts(slug), repo, slug))
        except LookupError as e:
            # 404 here means code scanning has never run on this repository
            # (no workflow, or default setup still pending) -- not a fault.
            logger.info("github inbox: %s has no code scanning results yet: %s", repo, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("github inbox: %s code_scanning failed: %s", repo, e)

    return items


_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3, "error": 1, "warning": 2, "note": 3}
_MAX_LOCATIONS_IN_SUMMARY = 12


def _alert_severity(alert: dict) -> str:
    rule = alert.get("rule") or {}
    return str(rule.get("security_severity_level") or rule.get("severity") or "unknown").lower()


def code_scanning_items(alerts: list[dict], repo: str, slug: str) -> list[Item]:
    """One item per rule, not per alert. Five `js/double-escaping` findings
    are one piece of work (the same helper, fixed once), and a task that sees
    all the locations fixes the cause rather than the first symptom. The key
    is the rule id with '/' folded to '.', because the key travels in a URL
    path segment. The fingerprint covers every open alert's number, commit
    and location, so a partial fix re-decides the item and a complete fix
    resolves it."""
    by_rule: dict[str, list[dict]] = {}
    for a in alerts:
        if a.get("state") not in (None, "open"):
            continue
        rule_id = str((a.get("rule") or {}).get("id") or "unknown-rule")
        by_rule.setdefault(rule_id, []).append(a)
    items: list[Item] = []
    for rule_id, group in by_rule.items():
        group.sort(key=lambda a: int(a.get("number") or 0))
        first = group[0]
        rule = first.get("rule") or {}
        tool = ((first.get("tool") or {}).get("name")) or "code scanning"
        sev = min((_alert_severity(a) for a in group), key=lambda s: _SEVERITY_RANK.get(s, 9))
        locs = []
        for a in group:
            inst = a.get("most_recent_instance") or {}
            loc = inst.get("location") or {}
            msg = str((inst.get("message") or {}).get("text") or "").strip().replace("\n", " ")
            path = loc.get("path") or "?"
            line = loc.get("start_line")
            locs.append((a.get("number"), f"{path}:{line}" if line else path, msg, (inst.get("commit_sha") or "")[:12]))
        fp = hashlib.sha1("|".join(f"{n}:{where}:{sha}" for n, where, _, sha in locs).encode()).hexdigest()[:12]
        lines = [f"{tool} · {sev} · {len(group)} open alert{'s' if len(group) != 1 else ''} for rule {rule_id}"]
        for n, where, msg, _ in locs[:_MAX_LOCATIONS_IN_SUMMARY]:
            lines.append(f"#{n} {where}" + (f" — {msg[:160]}" if msg else ""))
        if len(locs) > _MAX_LOCATIONS_IN_SUMMARY:
            lines.append(f"… and {len(locs) - _MAX_LOCATIONS_IN_SUMMARY} more (see the alerts page)")
        desc = rule.get("description") or rule.get("name") or rule_id
        items.append(Item(
            key=f"code:{rule_id.replace('/', '.')}", kind="code_scanning", repo=repo, number=None,
            title=f"[{sev.upper()}] {rule_id}: {desc}" + (f" ({len(group)} locations)" if len(group) > 1 else ""),
            url=f"https://github.com/{slug}/security/code-scanning?query=is%3Aopen+rule%3A{rule_id}",
            fingerprint=fp, summary="\n".join(lines),
        ))
    items.sort(key=lambda i: (_SEVERITY_RANK.get(i.title[1:i.title.index(']')].lower(), 9), i.key))
    return items


async def _ci_failures(client: GitHubClient, repo: str, slug: str, branch: str) -> list[Item]:
    """Failed runs on the tip of `branch`: check runs when the token may read
    them, else the newest Actions workflow runs. Only the latest commit that
    has runs counts -- a failure three commits back is history, not work."""
    out: list[Item] = []
    try:
        runs = await client.check_runs(slug, branch)
        for run in runs:
            if run.get("conclusion") not in ("failure", "timed_out"):
                continue
            sha = (run.get("head_sha") or "")[:12]
            name = run.get("name") or "check"
            out.append(Item(
                key=f"ci:{sha}:{name}", kind="ci_failures", repo=repo,
                title=f"{name} failed on {branch} @ {sha[:7]}", url=run.get("html_url") or "",
                fingerprint=f"{sha}:{run.get('id')}",
                summary=(run.get("output") or {}).get("title") or run.get("conclusion") or "",
            ))
        return out
    except PermissionError as e:
        logger.info("github inbox: %s check-runs refused (%s); trying Actions runs", repo, str(e)[:80])
    runs = await client.workflow_runs(slug, branch)
    if not runs:
        return out
    # Dependabot's own version-update jobs show up as workflow runs with
    # event "dynamic" ("npm_and_yarn in /. for sharp - Update #..."). A failed
    # one means Dependabot could not produce a PR, which the alerts source
    # already covers; it is not a check on the operator's code (2026-09-10:
    # five of six "failing checks" on storefront were these).
    runs = [r for r in runs if r.get("event") != "dynamic"]
    if not runs:
        return out
    tip = runs[0].get("head_sha") or ""
    for run in runs:
        if run.get("head_sha") != tip or run.get("conclusion") not in ("failure", "timed_out"):
            continue
        sha = tip[:12]
        name = run.get("name") or "workflow"
        out.append(Item(
            key=f"ci:{sha}:{name}", kind="ci_failures", repo=repo,
            title=f"{name} failed on {branch} @ {sha[:7]}", url=run.get("html_url") or "",
            fingerprint=f"{sha}:{run.get('id')}",
            summary=f"{run.get('display_title') or ''} ({run.get('event') or 'run'} #{run.get('run_number')})".strip(),
        ))
    return out


# ---------------------------------------------------------------------------
# policy -- pure
# ---------------------------------------------------------------------------

# Said in full on the item, because "why did this only get proposed?" is asked
# at the moment the operator is deciding, not when they set the policy.
_NO_CHECKS_REASON = ("auto needs checks: this project's review gate runs nothing mechanical, "
                     "so nothing would verify the work -- proposed instead")
_UNCONFIRMED_CHECKS_REASON = ("auto held back: could not confirm this project's checks with the "
                              "review service -- proposed instead")


@dataclass
class Decision:
    item: Item
    action: str      # "none" | "propose" | "create"
    reason: str = ""


def decide(existing: dict[str, dict], found: list[Item], proj: dict, open_auto: int,
           now: float | None = None, has_checks: bool | None = True,
           live_tasks: set | None = None) -> tuple[list[Decision], list[str]]:
    """Compare what was found with what the store holds.

    Returns the decisions for items that are new or changed, and the keys of
    stored items that are no longer present on GitHub (merged, closed, fixed)
    so the caller can mark them resolved. `open_auto` is how many auto-created
    tasks are still open; the cap is enforced here.

    `has_checks` is whether this project's review gate runs anything
    mechanical (tests, lint, a build), from the review service -- None when
    that could not be confirmed. Auto needs it: starting work by itself whose
    gate runs no checks means a model's opinion is the only thing between a
    GitHub alert and a diff waiting for the operator's merge click. Without
    confirmed checks, Auto degrades to Propose, with the reason on the item.
    The operator loses one click and keeps the review that click is for.
    """
    now = now or time.time()
    # None means "we could not ask", which must behave exactly as before:
    # never re-propose on a guess. An empty SET, by contrast, is a real
    # answer -- nothing is in flight.
    live = live_tasks

    def _still_being_worked(prev: dict) -> bool:
        """Is a task genuinely on this item right now?

        `task_created` used to be a one-way door: whatever became of the task,
        the item kept the state and the UI offered no action on it, so the
        alert sat in the list with no button while it was still open on
        GitHub. Observed 2026-09-22 -- a task was stopped after going down a
        rabbit hole, and its alert became unreachable.

        In flight means running, queued, or parked on a decision about THAT
        task (an approval, a merge, an escalation the operator can resume).
        Stopped, errored, finished-without-fixing-it, or a task id that no
        longer exists are all "nobody is on this", and the honest state for
        those is back in the queue.
        """
        if live is None:
            return True            # could not ask; keep the old behaviour
        return prev.get("task_id") in live

    decisions: list[Decision] = []
    found_keys = {i.key for i in found}
    budget = max(0, int(proj.get("max_open_auto", 2)) - open_auto)
    auto_allowed = has_checks is True
    for item in found:
        prev = existing.get(item.key)
        mode = proj["policies"].get(item.kind, "off")
        item.mode = mode
        if prev and prev.get("fingerprint") == item.fingerprint:
            # Unchanged. A snooze that expired is re-proposed, an item whose
            # task is no longer running comes back to the queue, and anything
            # else keeps its state.
            if prev.get("state") == "snoozed" and (prev.get("snoozed_until") or 0) <= now and mode != "off":
                item.state = "proposed"
                item.created_at = prev.get("created_at", now)
                decisions.append(Decision(item, "propose", "snooze expired"))
                continue
            # This is the branch a stranded item is actually in: the alert did
            # not change, the TASK died. Checking only the changed-fingerprint
            # path below would have left the common case stuck.
            if (prev.get("state") == "task_created" and mode != "off"
                    and not _still_being_worked(prev)):
                item.state = "proposed"
                item.created_at = prev.get("created_at", now)
                decisions.append(Decision(item, "propose", "its task is no longer running"))
                continue
            continue
        if prev:
            item.created_at = prev.get("created_at", now)
            if (prev.get("state") == "task_created" and prev.get("task_id")
                    and _still_being_worked(prev)):
                # The thing changed while a task is on it (the task itself
                # pushed, most likely). Keep the link; do not stack a second.
                item.state = "task_created"
                item.task_id = prev["task_id"]
                decisions.append(Decision(item, "none", "changed while a task is open"))
                continue
            # ...and if that task is gone, fall through to the normal policy
            # decision below, which proposes or creates as the project says.
        if mode == "off":
            item.state = "seen"
            decisions.append(Decision(item, "none", "source is off"))
        elif mode == "propose":
            item.state = "proposed"
            decisions.append(Decision(item, "propose", "policy: propose"))
        else:  # auto
            if not auto_allowed:
                item.state = "proposed"
                decisions.append(Decision(item, "propose", _NO_CHECKS_REASON if has_checks is False else _UNCONFIRMED_CHECKS_REASON))
            elif budget > 0:
                budget -= 1
                item.state = "task_created"
                decisions.append(Decision(item, "create", "policy: auto"))
            else:
                item.state = "proposed"
                decisions.append(Decision(item, "propose", f"auto cap reached ({proj.get('max_open_auto')} open)"))
    gone = [k for k, v in existing.items() if k not in found_keys and v.get("state") not in ("resolved",)]
    return decisions, gone


# ---------------------------------------------------------------------------
# approve links -- HMAC over the auth secret, expiring, single-use by state
# ---------------------------------------------------------------------------

def _mac(config: Config, payload: bytes) -> str:
    key = base64.urlsafe_b64decode(config.auth_secret_key)
    return base64.urlsafe_b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode().rstrip("=")


def sign_approval(config: Config, repo: str, key: str, nonce: str, action: str = "approve", ttl_s: int = APPROVAL_TTL_S, now: float | None = None) -> str:
    payload = json.dumps({"r": repo, "k": key, "n": nonce, "a": action, "e": int((now or time.time()) + ttl_s)}, separators=(",", ":")).encode()
    body = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"{body}.{_mac(config, payload)}"


def verify_approval(config: Config, token: str, now: float | None = None) -> dict:
    """The payload, or ValueError whose message is a reason CODE (malformed,
    invalid, expired, unknown) the page maps to its own wording."""
    try:
        body, mac = token.split(".", 1)
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except Exception as e:  # noqa: BLE001
        raise ValueError("malformed") from e
    if not hmac.compare_digest(mac, _mac(config, payload)):
        raise ValueError("invalid")
    data = json.loads(payload)
    if data.get("e", 0) < (now or time.time()):
        raise ValueError("expired")
    if data.get("a") not in ("approve", "dismiss"):
        raise ValueError("unknown")
    return data


def approval_url(settings: dict, token: str) -> str | None:
    base = settings.get("public_url") or ""
    return f"{base}/api/github/approve?t={token}" if base else None


# ---------------------------------------------------------------------------
# task goal
# ---------------------------------------------------------------------------

_GOAL_TEMPLATES = {
    "dependabot_prs": (
        "Land the dependency update proposed in GitHub pull request #{number} ({title}).\n\n"
        "Read the PR with github_pull_request first. Apply the same version bump on this branch "
        "(edit the manifest and lockfile the way the PR does; do not hand-edit a lockfile if the "
        "package manager can regenerate it), install, and run the project's full test suite and build. "
        "If a breaking change needs code updates, make them and say so in the commit message. "
        "Do not touch unrelated files.\n\nPR: {url}\n{summary}"
    ),
    "security_alerts": (
        "Fix Dependabot security alert #{number}: {title}.\n\n"
        "{summary}\n\nUpgrade the affected package to a patched version in every manifest that pins "
        "it, regenerate the lockfile with the package manager, install, and run the full test suite "
        "and build. If the direct dependency must move to a new major to clear the alert, make the "
        "code changes that requires and say so in the commit message.\n\nAlert: {url}"
    ),
    "review_requests": (
        "Address the review on GitHub pull request #{number} ({title}): {summary}.\n\n"
        "Read the PR with github_pull_request, part=\"comments\" first, then part=\"diff\". Treat every "
        "review comment as a todo: fix each one on this branch, keeping the PR's intent, and note in "
        "the commit message which comments were addressed. Run the tests the PR touches.\n\nPR: {url}"
    ),
    "ci_failures": (
        "Fix the failing check on the default branch: {title}.\n\n{summary}\n\nRead the check run "
        "output at {url}, reproduce the failure locally, fix the cause (not the check), and run the "
        "full suite before finishing."
    ),
    "code_scanning": (
        "Fix the code scanning finding {title} in THIS repository.\n\n{summary}\n\n"
        "Every location above is an open alert on this repository's Security → Code scanning page ({url}); "
        "the alerts belong to this repository only — do not look for or touch other projects. "
        "For each location: read the surrounding code, understand why the query flags it, and fix "
        "the cause (validate or constrain the input, use the safe API, or restructure the flow) with "
        "the smallest change that makes the finding untrue. Do not silence it: no lgtm/codeql "
        "suppression comments, no dismissing alerts on GitHub, no deleting the code path unless it is "
        "genuinely dead. Where several locations share a helper, fix the helper once. Keep behaviour "
        "identical for legitimate input, add or extend a test where the fix is testable, and run the "
        "project's full test suite, typecheck and lint before finishing. In the commit message list "
        "the alert numbers addressed."
    ),
}


def build_goal(item: Item | dict, pr_text: str | None = None) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    goal = _GOAL_TEMPLATES[d["kind"]].format(
        number=d.get("number"), title=d.get("title"), url=d.get("url"), summary=d.get("summary") or "")
    if pr_text:
        goal += "\n\n--- pull request as read from GitHub ---\n" + pr_text[:20_000]
    goal += "\n\n(Created from the GitHub inbox. This task goes through the normal review gate and merge approval.)"
    return goal


async def pr_text_for(token: str, slug: str, number: int) -> str | None:
    """The PR summary (no diff) for the goal, or None if it cannot be read."""
    try:
        import asyncio
        data = await asyncio.to_thread(fetch_pull_request, token, slug, number)
        return format_pull_request(data, "summary")
    except Exception as e:  # noqa: BLE001
        logger.info("github inbox: could not read PR #%s on %s for the goal: %s", number, slug, e)
        return None


# ---------------------------------------------------------------------------
# notifications -- text only; delivery is the caller's
# ---------------------------------------------------------------------------

_KIND_LABEL = {
    "dependabot_prs": "Dependabot PR", "security_alerts": "Security alert",
    "review_requests": "Review requests changes", "ci_failures": "Failing check",
    "code_scanning": "Code scanning alert",
}


def proposal_text(item: Item | dict, approve_link: str | None, dismiss_link: str | None, budget_usd: float) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    lines = [
        f"🐙 GitHub: {_KIND_LABEL.get(d['kind'], d['kind'])} on {d['repo']}",
        d["title"],
    ]
    if d.get("summary"):
        lines.append(d["summary"])
    if d.get("reason") and "cap" in d["reason"]:
        lines.append(f"(not started automatically: {d['reason']})")
    lines.append(f"Budget if approved: ${budget_usd:.2f}")
    lines.append(d.get("url") or "")
    if approve_link:
        lines += ["", f"Approve → {approve_link}"]
        if dismiss_link:
            lines.append(f"Dismiss → {dismiss_link}")
    else:
        lines += ["", "Approve or dismiss it in the dashboard's GitHub inbox (set the public URL in Settings → GitHub to get links here)."]
    return "\n".join(lines)


def created_text(item: Item | dict, task_id: str, budget_usd: float) -> str:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    return (f"🐙 GitHub: task started automatically on {d['repo']}\n{d['title']}\n"
            f"{_KIND_LABEL.get(d['kind'], d['kind'])} · budget ${budget_usd:.2f} · task {task_id[:8]}\n{d.get('url') or ''}")


# ---------------------------------------------------------------------------
# the poll -- store + GitHub + callbacks, no FastAPI
# ---------------------------------------------------------------------------

CreateTask = Callable[[str, str, float, str], Awaitable[str]]        # (repo, goal, budget, route) -> task_id
Notify = Callable[[str, str], Awaitable[None]]                        # (text, repo)
OpenAutoCount = Callable[[str], Awaitable[int]]                       # repo -> open auto tasks
LiveTasks = Callable[[str], Awaitable[set]]                           # repo -> task ids still in flight


async def list_items(store, repo: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for it in await store.asearch((NAMESPACE, repo), limit=200):
        out[it.key] = it.value
    return out


async def put_item(store, item: Item | dict) -> None:
    d = item.to_dict() if isinstance(item, Item) else dict(item)
    d["updated_at"] = time.time()
    await store.aput((NAMESPACE, d["repo"]), d["key"], d)


async def create_task_for_item(item: dict, settings: dict, config: Config, create_task: CreateTask) -> str:
    proj = github_settings.project_settings(settings, item["repo"])
    pr_text = None
    if item.get("number") and item["kind"] in ("dependabot_prs", "review_requests"):
        token = github_settings.token_for(settings, config, item["repo"])
        slug = resolve_slug(item["repo"])
        if token and slug:
            pr_text = await pr_text_for(token, slug, int(item["number"]))
    goal = build_goal(item, pr_text)
    return await create_task(item["repo"], goal, float(proj["budget_usd"]), proj.get("route", "auto"))


async def poll_project(
    store, config: Config, settings: dict, repo: str, *,
    create_task: CreateTask, notify: Notify, open_auto_count: OpenAutoCount,
    client: GitHubClient | None = None, live_tasks: LiveTasks | None = None,
) -> dict:
    """One project, one pass. Returns a small summary for the dashboard/log."""
    proj = github_settings.project_settings(settings, repo)
    token = github_settings.token_for(settings, config, repo)
    slug = resolve_slug(repo)
    if not token or not slug:
        return {"repo": repo, "skipped": "no token" if not token else "no GitHub origin"}
    client = client or GitHubClient(token)
    found = await discover(client, repo, slug, proj)
    existing = await list_items(store, repo)
    open_auto = await open_auto_count(repo)
    # Asked at poll time, not at save time only: a project's checks can be
    # emptied long after its policy was set, and the poll is what actually
    # starts work.
    from agent.tools.review_gate import project_has_checks
    has_checks = await project_has_checks(repo)
    # None when the caller did not supply a lookup: decide() then keeps every
    # task_created item exactly as it was, which is what it did before this.
    in_flight = await live_tasks(repo) if live_tasks else None
    decisions, gone = decide(existing, found, proj, open_auto, has_checks=has_checks,
                             live_tasks=in_flight)
    summary = {"repo": repo, "found": len(found), "proposed": 0, "created": 0, "resolved": 0}
    for d in decisions:
        item = d.item
        item.reason = d.reason
        if d.action == "create":
            try:
                item.task_id = await create_task_for_item(item.to_dict(), settings, config, create_task)
                summary["created"] += 1
                await put_item(store, item)
                # The one entry in this log with no person behind it, and the
                # one most worth having: a task appeared, nobody clicked
                # anything, and the only trace otherwise is a Telegram
                # message. The actor names the policy, not an account -- the
                # operator who set the source to Auto is already recorded
                # under settings, at the time they set it.
                await audit.record(
                    store, actor="github-inbox", action="inbox.auto_start",
                    target=f"{repo}/{item.key}", detail=(item.title or "")[:160],
                    extra={"task_id": item.task_id},
                )
                await notify(created_text(item, item.task_id, float(proj["budget_usd"])), repo)
            except Exception as e:  # noqa: BLE001 -- fall back to a proposal rather than lose the item
                logger.exception("github inbox: auto task for %s %s failed", repo, item.key)
                item.state = "proposed"
                item.reason = f"auto start failed: {e}"
                d.action = "propose"
        if d.action == "propose":
            item.approval_nonce = secrets.token_urlsafe(12)
            await put_item(store, item)
            summary["proposed"] += 1
            approve = approval_url(settings, sign_approval(config, repo, item.key, item.approval_nonce, "approve"))
            dismiss = approval_url(settings, sign_approval(config, repo, item.key, item.approval_nonce, "dismiss"))
            await notify(proposal_text(item, approve, dismiss, float(proj["budget_usd"])), repo)
        elif d.action == "none":
            await put_item(store, item)
    for key in gone:
        prev = existing[key]
        prev["state"] = "resolved"
        prev["reason"] = "no longer open on GitHub"
        await put_item(store, prev)
        summary["resolved"] += 1
    return summary


async def poll_all(store, config: Config, *, create_task: CreateTask, notify: Notify,
                   open_auto_count: OpenAutoCount, live_tasks: LiveTasks | None = None) -> list[dict]:
    settings = github_settings.current()
    out = []
    for repo in github_settings.enabled_projects(settings):
        try:
            out.append(await poll_project(store, config, settings, repo, create_task=create_task,
                                          notify=notify, open_auto_count=open_auto_count,
                                          live_tasks=live_tasks))
        except Exception as e:  # noqa: BLE001 -- one project's failure must not strand the others
            logger.exception("github inbox: poll failed for %s", repo)
            out.append({"repo": repo, "error": str(e)[:200]})
    return out


# ---------------------------------------------------------------------------
# token probe -- for the settings card's Test button
# ---------------------------------------------------------------------------

async def _can(client: GitHubClient, path: str, params: dict) -> bool | None:
    """True/False for a permission the token clearly has or lacks; None when
    the endpoint answered something else (an empty repo has no HEAD, say)."""
    try:
        await client.get(path, params)
        return True
    except PermissionError:
        return False
    except Exception:  # noqa: BLE001
        return None


async def _can_ci(client: GitHubClient, slug: str) -> bool | None:
    """Either "Checks: read" (check runs) or "Actions: read" (workflow runs)
    is enough for the failing-checks source."""
    checks = await _can(client, f"/repos/{slug}/commits/HEAD/check-runs", {"per_page": 1})
    if checks:
        return True
    actions = await _can(client, f"/repos/{slug}/actions/runs", {"per_page": 1})
    if actions:
        return True
    return False if (checks is False or actions is False) else None


async def probe_token(token: str, projects: dict[str, dict]) -> dict:
    """Who the token is, which configured projects it can see, and what it
    may do there. Fine-grained tokens list only their selected repos."""
    client = GitHubClient(token)
    out: dict[str, Any] = {"ok": True, "login": None, "repos": [], "matched": []}
    try:
        me = await client.get("/user")
        out["login"] = me.get("login")
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"token rejected: {e}"}
    slugs = {resolve_slug(name): name for name in projects}
    slugs.pop(None, None)
    try:
        repos = await client.get("/user/repos", {"per_page": 100, "sort": "updated"})
    except Exception as e:  # noqa: BLE001
        repos = []
        out["warning"] = f"could not list repositories: {e}"
    for r in repos:
        slug = r.get("full_name")
        perms = r.get("permissions") or {}
        entry = {"slug": slug, "project": slugs.get(slug), "push": bool(perms.get("push")), "pull": bool(perms.get("pull"))}
        out["repos"].append(entry)
        if entry["project"]:
            entry["dependabot_alerts"] = await _can(client, f"/repos/{slug}/dependabot/alerts", {"per_page": 1})
            entry["code_scanning"] = await _can(client, f"/repos/{slug}/code-scanning/alerts", {"per_page": 1})
            entry["checks"] = await _can_ci(client, slug)
            out["matched"].append(entry)
    # A project whose slug the token did not list may still be reachable
    # (classic tokens list everything; fine-grained ones only selected).
    listed = {e["slug"] for e in out["repos"]}
    for slug, name in slugs.items():
        if slug in listed:
            continue
        try:
            info = await client.repo(slug)
            perms = info.get("permissions") or {}
            out["matched"].append({
                "slug": slug, "project": name, "push": bool(perms.get("push")), "pull": True,
                "dependabot_alerts": await _can(client, f"/repos/{slug}/dependabot/alerts", {"per_page": 1}),
                "code_scanning": await _can(client, f"/repos/{slug}/code-scanning/alerts", {"per_page": 1}),
                "checks": await _can_ci(client, slug),
            })
        except Exception:  # noqa: BLE001
            pass
    return out
