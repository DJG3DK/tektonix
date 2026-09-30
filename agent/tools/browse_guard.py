"""A per-turn limit on browsing, and on guessing addresses in particular.

2026-09-30: a planning turn whose web search had failed loaded one real docs
page and then invented twenty more paths on the same site
(`…/registration-and-authentication-walkthrough`, `…-tutorial`, `…-in-depth`),
nearly every one "Page Not Found". Nothing counted them: the repeat guard sees
different URLs, and the overall tool-call limit is far higher.

Two rules, each ending in a message that says what to do instead:
  * after MISSES_PER_HOST pages that do not exist on one host, that host is
    closed for the turn;
  * after BUDGET pages in all, browsing is closed for the turn.

One guard per agent build: the planning agent and the chat build their tools
every turn, so the counts reset with the turn.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

MISSES_PER_HOST = 3
BUDGET = 15

_NOT_FOUND = re.compile(
    r"\b(404|page not found|not found|page (?:does not|doesn't) exist|no longer exists|could not find what you were looking for)\b",
    re.I)


def looks_missing(result: str) -> bool:
    """True when a browse result is an error page: an HTTP error the tool
    reported, or a page whose title or opening says it does not exist (a
    single-page docs site answers 200 with a "Page Not Found" view)."""
    if not result:
        return False
    if result.startswith("ERROR: HTTP 4") or result.startswith("ERROR: HTTP 5"):
        return True
    head = result[:400]
    return bool(_NOT_FOUND.search(head))


class BrowseGuard:
    def __init__(self, misses_per_host: int = MISSES_PER_HOST, budget: int = BUDGET):
        self.misses_per_host = misses_per_host
        self.budget = budget
        self.loaded = 0
        self.misses: dict[str, int] = {}

    @staticmethod
    def host(url: str) -> str:
        return (urlparse(url).hostname or "").lower()

    def check(self, url: str) -> str | None:
        """A refusal, or None to go ahead."""
        host = self.host(url)
        if self.misses.get(host, 0) >= self.misses_per_host:
            return (
                f"ERROR: {self.misses[host]} pages you tried on {host} do not exist. Stop guessing "
                f"addresses on that site. Open a link that appeared in a page you loaded or in a search "
                f"result, try a search, or say that the documentation could not be reached and carry on "
                f"with what you have."
            )
        if self.loaded >= self.budget:
            return (
                f"ERROR: this turn has loaded {self.loaded} pages, the limit. Work with what you have "
                f"read; if something is still unknown, say so in your answer rather than reading more."
            )
        return None

    def record(self, url: str, result: str) -> None:
        self.loaded += 1
        if looks_missing(result):
            host = self.host(url)
            self.misses[host] = self.misses.get(host, 0) + 1
