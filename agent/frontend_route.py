"""Frontend routing: which planning chat and which coder a request gets.

The operator wants Kimi k3 on frontend work -- "a master at frontend polish"
-- and deepseek everywhere else, because Kimi costs roughly ten times as much
per coder call. Polish is decided at the keyboard, not in the plan, so the
seat that matters is the coder (and the investigator that reads for it); the
test-writer stays on the general model by the operator's own call
(2026-09-09). Planning chat gets a frontend tier too, so a frontend plan is
written in the vocabulary the coder will execute it in.

Three signals, strongest first, plus a switch the operator flips:

1. category  -- the task classifier's `ui-styling` is the strongest evidence:
                a model read the whole goal. (Tasks only; a planning session
                has no category when its first turn starts.)
2. backend   -- any named backend path (api/, prisma/, migrations/, a .sql or
                .prisma file...) or backend keyword (migration, schema,
                database, endpoint...) routes GENERAL. Database and API work is
                never "frontend work" because it also has a UI.
3. paths     -- files the request names. A CLEAR majority of frontend paths
                (two thirds) is frontend work whatever the category says: a
                `feature` that lives in frontend/ routes to Kimi. Anything
                short of that with both kinds named stays general.
4. keywords  -- a short list covering both where a thing lives (page, modal,
                sidebar) and what it should look like (glow, gradient, hdr),
                matched whole-word but plural-tolerant, and it takes two
                distinct hits: "fix the chart's numbers" mentions a chart but
                is a data bug.
0. override  -- "frontend" or "general" from the Build Now popup, the New Task
                form, or a new planning session. Beats everything.

Every decision carries a reason, and the task/session shows it, because
silent routing is how a $40 Kimi run on a backend refactor would happen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ROUTES = ("auto", "frontend", "general")
FRONTEND, GENERAL = "frontend", "general"

# Roles the two routes resolve to. The aliases live in the router's
# config.yaml; the operator pins whatever model they like behind them.
CODER_ROLE = {FRONTEND: "agent-coder-frontend", GENERAL: "agent-coder"}
PLANNING_ROLE = {FRONTEND: "agent-planning-chat-frontend"}

FRONTEND_CATEGORIES = frozenset({"ui-styling"})
# Backend-core signals: a request that names any of these is not "frontend
# work" however many component files it also lists. A storefront task on
# 2026-09-09 -- a Prisma schema change so products can live in several
# categories, with API, mapper, import and shared-type edits -- named 19
# frontend files against 17 backend ones and routed to the frontend seat on
# the majority vote. The database migration was the whole risk of that task;
# the file count said nothing about it.
BACKEND_DIRS = frozenset({"api", "server", "backend", "prisma", "migrations", "db", "database", "services", "controllers", "models", "workers", "core", "strategies"})
BACKEND_EXTS = frozenset({".sql", ".prisma", ".py", ".go", ".rs", ".java", ".rb", ".php"})
BACKEND_KEYWORDS = ("migration", "schema", "database", "prisma", "sql", "endpoint", "controller", "foreign key", " fk ", "orm", "backfill")
# A frontend majority has to be clear, not a coin flip.
FRONTEND_MAJORITY = 2 / 3
# How many frontend files it takes for a backend WORD in the prose to stop
# outranking them. A backend PATH always wins regardless (see the 2026-09-09
# note above) -- this is only about vocabulary.
#
# Three, because one is genuinely ambiguous and several is not: "add a
# migration so ShopPage.tsx can read the new column" names one component and
# is backend work, while a task naming three or more frontend files and zero
# backend ones is frontend work whose description happens to say "endpoint".
# The live case (2026-09-16) was a storefront error-state task that named four
# .tsx files, no backend files at all, and routed to the general coder because
# the word "endpoint" appeared in a sentence about what was already built.
FRONTEND_PATH_QUORUM = 3
# "storefront" is here because a frontend APP legitimately contains an api/
# directory -- its own HTTP client -- and "api" is in BACKEND_DIRS above.
# Without it, apps/storefront/src/api/client.ts reads as a backend file and
# one such path routes an entire storefront task to the general coder
# (storefront, 2026-09-16). A backend never contains components/ or pages/,
# so the markers are not symmetric and the frontend one should win.
FRONTEND_DIRS = frozenset({"frontend", "web", "client", "ui", "components", "pages", "views",
                           "layouts", "styles", "css", "storefront"})
FRONTEND_EXTS = frozenset({".tsx", ".jsx", ".css", ".scss", ".less", ".html", ".vue", ".svelte"})
CODE_EXTS = FRONTEND_EXTS | frozenset({".ts", ".js", ".mjs", ".cjs", ".py", ".go", ".rs", ".java", ".rb", ".php", ".sql", ".sh", ".json", ".yaml", ".yml", ".prisma"})
FRONTEND_KEYWORDS = (
    "ui", "ux", "layout", "styling", "style", "css", "design", "responsive", "theme",
    "animation", "polish", "dashboard", "page", "component", "button", "modal",
    "sidebar", "font", "color", "colour", "spacing", "mobile", "dark mode", "hover", "tooltip",
    # Lighting and material vocabulary. The list above described WHERE a thing
    # lives and had almost nothing for what it should LOOK like, so "an HDR
    # lighting effect on the add-to-cart buttons" (storefront, 2026-09-15) scored
    # one hit and planned on the general seat -- the purest frontend request
    # the operator has ever typed.
    "hdr", "lighting", "glow", "sheen", "shine", "gloss", "bloom", "gradient",
    "shadow", "opacity", "blur", "transition", "visual",
    # Presentational nouns. Deliberately NOT here: "header" (HTTP headers),
    # "card" (payment cards), "margin" (margin trading, in webapp), "cart"
    # (cart logic is backend) -- each reads as frontend in a storefront and as
    # something else entirely one repo over.
    "icon", "banner", "hero", "navbar", "footer", "carousel", "dropdown",
    "badge", "scroll", "alignment", "storefront",
)

# Segment-then-separator, not separator-then-segment: "(?:seg/)+seg" let the
# engine split a run of "-" characters between the repeated group and the
# tail in many ways (CodeQL py/polynomial-redos, 2026-09-10); with "/" as the
# only way to enter another segment there is one parse per input.
# Possessive (++): once a run of name characters is consumed it is never
# given back, so a long run of "-" that ends without "/" or an extension
# fails in one step instead of being re-split at every length.
_PATH_TOKEN = re.compile(r"(?<![\w/])([\w.@-]++(?:/[\w.@-]++)++|[\w@-]++\.(?:tsx|jsx|css|scss|less|html|vue|svelte))(?![\w/])")


def _word(kw: str) -> str:
    """Whole-word match for a keyword, tolerating a plural.

    The guard used to be a bare `(?![a-z])`, which is correct about "ui" in
    "guidance" and wrong about every plural: a trailing "s" IS [a-z], so
    "buttons" did not match "button", "pages" did not match "page", and
    "migrations" did not match "migration". Both lists were affected -- 23 of
    the 25 frontend keywords and 10 of the 11 backend ones could only be hit
    in the singular, which is not how anyone writes a request ("make the
    buttons glow", not "make the button glow").

    Only a suffix is allowed, never a prefix: "pager" and "formats" must still
    not hit "page" and "orm".
    """
    return r"(?<![a-z])" + re.escape(kw) + r"(?:es|s)?(?![a-z])"


@dataclass(frozen=True)
class RouteDecision:
    route: str      # "frontend" | "general"
    reason: str

    @property
    def is_frontend(self) -> bool:
        return self.route == FRONTEND


def _is_frontend_path(path: str) -> bool:
    parts = [p.lower() for p in path.strip("`'\"()[],.").split("/")]
    ext = "." + parts[-1].rsplit(".", 1)[-1] if "." in parts[-1] else ""
    if ext in FRONTEND_EXTS:
        return True
    if ext in CODE_EXTS and any(p in FRONTEND_DIRS for p in parts[:-1]):
        return True
    return False


def _is_code_path(path: str) -> bool:
    last = path.strip("`'\"()[],.").split("/")[-1]
    return "." in last and ("." + last.rsplit(".", 1)[-1]) in CODE_EXTS


def _is_backend_path(path: str) -> bool:
    parts = [p.lower() for p in path.strip("`'\"()[],.").split("/")]
    ext = "." + parts[-1].rsplit(".", 1)[-1] if "." in parts[-1] else ""
    if ext in BACKEND_EXTS or parts[-1].lower() == "schema.prisma":
        return True
    return any(p in BACKEND_DIRS for p in parts[:-1]) and not _is_frontend_path(path)


def backend_paths(text: str) -> list[str]:
    """Backend FILES the request names. The strong signal: a task that edits
    schema.prisma is backend work however many components it also touches."""
    _fe, other = named_paths(text)
    return [p for p in other if _is_backend_path(p)]


def backend_keywords(text: str) -> list[str]:
    """Backend VOCABULARY in the prose. Weaker than a path -- a sentence can
    say "endpoint" while every file the task names is a component."""
    lowered = (text or "").lower()
    # Whole words only: "orm" must not fire on "format.ts", "sql" not on "mysql".
    return [kw.strip() for kw in BACKEND_KEYWORDS if re.search(_word(kw.strip()), lowered)]


def backend_signals(text: str) -> list[str]:
    """Both kinds, for the reason string and for callers that want either."""
    return backend_paths(text) + backend_keywords(text)


def named_paths(text: str) -> tuple[list[str], list[str]]:
    """(frontend paths, other code paths) named in the text, de-duplicated."""
    fe: list[str] = []
    other: list[str] = []
    seen: set[str] = set()
    for tok in _PATH_TOKEN.findall(text or ""):
        tok = tok.strip("`'\"()[],.")
        if tok in seen or "://" in tok:
            continue
        seen.add(tok)
        if _is_frontend_path(tok):
            fe.append(tok)
        elif _is_code_path(tok):
            other.append(tok)
    return fe, other


def keyword_hits(text: str) -> list[str]:
    lowered = (text or "").lower()
    hits = []
    for kw in FRONTEND_KEYWORDS:
        if re.search(_word(kw), lowered):
            hits.append(kw)
    return hits


def classify_frontend(text: str, category: str | None = None, override: str | None = None) -> RouteDecision:
    """The routing decision for a task goal, a plan, or a planning message."""
    if override in (FRONTEND, GENERAL):
        return RouteDecision(override, "operator's choice")
    if category in FRONTEND_CATEGORIES:
        return RouteDecision(FRONTEND, f"category {category}")
    # A named backend FILE ends it: that is the 2026-09-09 lesson, and the
    # risk it protects (a migration inside a mostly-frontend diff) is real.
    b_paths = backend_paths(text)
    if b_paths:
        shown = ", ".join(dict.fromkeys(b_paths))[:120]
        return RouteDecision(GENERAL, f"backend work named: {shown}")

    fe, other = named_paths(text)
    total = len(fe) + len(other)
    b_words = backend_keywords(text)

    # No backend file anywhere, and several frontend ones: the files are better
    # evidence than a word in a sentence about them.
    if len(fe) >= FRONTEND_PATH_QUORUM and not other:
        return RouteDecision(FRONTEND, f"{len(fe)} frontend files named, no backend files")

    if b_words:
        shown = ", ".join(dict.fromkeys(b_words))[:120]
        return RouteDecision(GENERAL, f"backend work named: {shown}")

    if fe and len(fe) >= FRONTEND_MAJORITY * total:
        return RouteDecision(FRONTEND, f"{len(fe)} of {total} named files are frontend")
    if fe and other:
        return RouteDecision(GENERAL, f"mixed: {len(other)} backend vs {len(fe)} frontend files")
    hits = keyword_hits(text)
    if len(hits) >= 2:
        return RouteDecision(FRONTEND, "keywords: " + ", ".join(hits[:4]))
    return RouteDecision(GENERAL, "no frontend signal")


def normalize_override(value: str | None) -> str | None:
    """A request's `route` field: "frontend"/"general" are overrides, "auto"
    or missing means decide."""
    return value if value in (FRONTEND, GENERAL) else None
