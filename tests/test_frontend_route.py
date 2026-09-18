"""Frontend routing (agent/frontend_route.py): which coder and planning seat
a request gets. Category beats paths beats keywords; the operator's choice
beats everything; every decision carries a reason."""

import pytest

from agent.frontend_route import (
    CODER_ROLE,
    FRONTEND_KEYWORDS,
    backend_signals,
    classify_frontend,
    keyword_hits,
    named_paths,
    normalize_override,
)


def test_operator_override_beats_everything():
    d = classify_frontend("rewrite src/core/bot.js for speed", category="performance", override="frontend")
    assert d.route == "frontend" and d.reason == "operator's choice"
    d = classify_frontend("polish the dashboard layout and theme", category="ui-styling", override="general")
    assert d.route == "general"


def test_ui_styling_category_routes_frontend():
    d = classify_frontend("tighten the positions table", category="ui-styling")
    assert d.is_frontend and d.reason == "category ui-styling"


def test_a_clear_frontend_majority_routes_frontend_even_for_a_feature():
    text = "Add a Notional column: frontend/src/pages/OpenPositionsPage.tsx, frontend/src/utils/format.ts, frontend/src/utils/positions.ts and a note in docs/README.md"
    d = classify_frontend(text, category="feature")
    assert d.is_frontend
    # Three frontend files and no backend file trips the quorum branch, which
    # is the more specific reason for the same decision.
    assert d.reason == "3 frontend files named, no backend files"


def test_any_named_backend_path_routes_general_whatever_the_count():
    """2026-09-09: a Prisma schema change with API, mapper and import edits
    named 19 frontend files against 17 backend ones and went to the frontend
    seat on the majority vote. The migration was the whole risk."""
    text = ("Products in several categories: change apps/api/prisma/schema.prisma, apps/api/src/catalog/products/products.service.ts, "
            "apps/api/src/catalog/mappers/product.mapper.ts, then apps/admin/src/pages/ProductEditPage.tsx, "
            "apps/admin/src/components/CategoryMultiSelect.tsx, apps/storefront/src/pages/ShopPage.tsx, HomePage.tsx, ProductDetailPage.tsx")
    d = classify_frontend(text, category="feature")
    assert d.route == "general"
    assert d.reason.startswith("backend work named:") and "schema.prisma" in d.reason


def test_backend_keywords_route_general_even_with_frontend_paths():
    d = classify_frontend("Add a migration so the ShopPage.tsx filter can read the new column", category="feature")
    assert d.route == "general" and "migration" in d.reason


def test_a_slim_frontend_majority_is_not_enough():
    text = "Touch frontend/src/a.tsx, frontend/src/b.tsx and lib/util.js, lib/other.js"  # 2 of 4
    d = classify_frontend(text, category="feature")
    assert d.route == "general" and d.reason.startswith("mixed:")


def test_mostly_backend_paths_stay_general():
    text = "Fix src/core/bot.js and src/strategies/gridder.js; adjust the badge in frontend/src/components/Sidebar.tsx"
    d = classify_frontend(text, category="bug-fix")
    assert d.route == "general"


def test_ui_styling_category_still_wins_over_a_backend_mention():
    # The classifier read the whole goal; a passing mention of an endpoint in a styling task does not demote it.
    assert classify_frontend("restyle the settings page; the endpoint stays as is", category="ui-styling").is_frontend


def test_two_keywords_route_frontend_one_does_not():
    assert classify_frontend("make the sidebar responsive on mobile").is_frontend
    d = classify_frontend("fix the chart's wrong numbers after a restart")
    assert d.route == "general" and d.reason == "no frontend signal"


def test_keyword_match_is_whole_word():
    assert "ui" not in keyword_hits("rebuild the guidance module")
    assert "page" not in keyword_hits("pagination in the API")


def test_named_paths_split_by_location_and_extension():
    fe, other = named_paths("touch frontend/src/App.tsx, src/core/bot.js, styles/app.css and docs/README.md")
    assert fe == ["frontend/src/App.tsx", "styles/app.css"]
    assert other == ["src/core/bot.js"]


@pytest.mark.parametrize("value,expected", [("frontend", "frontend"), ("general", "general"), ("auto", None), (None, None), ("bogus", None)])
def test_normalize_override(value, expected):
    assert normalize_override(value) == expected


def test_roles_are_the_router_aliases():
    assert CODER_ROLE == {"frontend": "agent-coder-frontend", "general": "agent-coder"}


# ---------------------------------------------------------------------------
# plurals, and the lighting vocabulary -- storefront, 2026-09-15
# ---------------------------------------------------------------------------

HDR_REQUEST = ("on the storefront, I want the buttons to add products to the cart "
               "to have an HDR lighting effect when you hover over them")


def test_the_hdr_lighting_request_routes_frontend():
    """The request that exposed both defects. It planned on the general seat
    with reason "no frontend signal": "buttons" could not match the keyword
    "button" (a plural "s" is [a-z], so the whole-word guard rejected it), and
    nothing in the list described light, so "HDR"/"lighting" scored zero. One
    hit, and it takes two."""
    d = classify_frontend(HDR_REQUEST)
    assert d.is_frontend, d.reason
    hits = keyword_hits(HDR_REQUEST)
    assert "button" in hits, "the plural has to match the singular keyword"
    assert "hdr" in hits and "lighting" in hits


@pytest.mark.parametrize("plural,singular", [
    ("make the buttons glow", "button"),
    ("tidy the pages", "page"),
    ("restyle the components", "component"),
    ("the colors are off", "color"),
    ("fix the modals and tooltips", "modal"),
])
def test_a_plural_matches_its_keyword(plural, singular):
    assert singular in keyword_hits(plural)


@pytest.mark.parametrize("text,kw", [
    ("add two migrations for the new column", "migration"),
    ("wire up the new endpoints", "endpoint"),
    ("the schemas disagree", "schema"),
])
def test_backend_keywords_match_in_the_plural_too(text, kw):
    """The same bug, and the more expensive direction to get wrong: a missed
    backend signal sends database work to the Kimi seat."""
    assert kw in backend_signals(text)


@pytest.mark.parametrize("text,kw", [
    ("rebuild the guidance module", "ui"),
    ("pagination in the API", "page"),
    ("the pager widget is slow", "page"),
    ("designer handoff notes", "design"),
])
def test_only_a_suffix_is_tolerated_never_a_prefix(text, kw):
    """Tolerating a plural must not turn the whole-word match into substring
    matching -- "pager" is still not "page"."""
    assert kw not in keyword_hits(text)


@pytest.mark.parametrize("text,kw", [
    ("fix format.ts and the forms", "orm"),
    ("mysql and sqlite tuning", "sql"),
])
def test_backend_keywords_keep_their_prefix_guard(text, kw):
    assert kw not in backend_signals(text)


def test_lighting_words_alone_are_not_enough_without_a_second_hit():
    """The new vocabulary widens the list; it must not lower the bar. One hit
    is still one hit."""
    d = classify_frontend("the gradient descent step is diverging")
    assert d.route == "general" and d.reason == "no frontend signal"


def test_the_ambiguous_words_stayed_out():
    """Each of these reads as frontend in a storefront and as something else
    one repo over -- margin trading in webapp, HTTP headers, payment cards."""
    for kw in ("header", "card", "margin", "cart"):
        assert kw not in FRONTEND_KEYWORDS


def test_backend_still_beats_the_wider_keyword_list():
    d = classify_frontend("Add a migration so the ShopPage.tsx buttons can read the new column", category="feature")
    assert d.route == "general" and "migration" in d.reason


def test_a_settled_planning_category_reaches_the_route_decision():
    """agent/server.py's planning turn used to pass None as the category, so a
    session that had already settled into `ui-styling` went on planning with
    the general seat every later turn. Pin the call shape: the session's own
    category has to be what is handed over."""
    import inspect

    import agent.server as srv

    source = inspect.getsource(srv._run_planning_turn_bg)
    assert 'classify_frontend(text, _meta_val.get("category")' in source
    assert "classify_frontend(text, None" not in source


# ---------------------------------------------------------------------------
# a backend WORD must not outrank an unambiguous list of frontend FILES
# (storefront, 2026-09-16)
# ---------------------------------------------------------------------------

def test_a_prose_backend_word_does_not_beat_several_frontend_files():
    """The live miss: a storefront error-state task naming four .tsx files and
    zero backend files routed to the general coder because the word "endpoint"
    appeared in a sentence describing what already existed. It then stalled in
    a tool loop on the weaker seat."""
    text = ("Make a dead API visible on the storefront. The API's health endpoint already works; "
            "this is about apps/storefront/src/components/LoadError.tsx, "
            "apps/storefront/src/pages/HomePage.tsx, apps/storefront/src/pages/ShopPage.tsx and "
            "apps/storefront/src/pages/ProductDetailPage.tsx.")
    d = classify_frontend(text)
    assert d.is_frontend, d.reason
    assert "no backend files" in d.reason


def test_one_frontend_file_beside_a_backend_word_still_routes_general():
    """The guard on the above. One component mentioned next to a migration is
    the 2026-09-09 case and must not flip: the migration is the whole risk."""
    d = classify_frontend("Add a migration so the ShopPage.tsx filter can read the new column")
    assert d.route == "general" and "migration" in d.reason


def test_a_named_backend_file_still_wins_outright():
    """A backend PATH is the strong signal and the quorum must not weaken it,
    however many components sit beside it."""
    text = ("apps/api/prisma/schema.prisma plus apps/storefront/src/a.tsx, "
            "apps/storefront/src/b.tsx, apps/storefront/src/c.tsx, apps/storefront/src/d.tsx")
    d = classify_frontend(text)
    assert d.route == "general"
    assert "schema.prisma" in d.reason


def test_a_frontend_apps_own_api_client_is_not_a_backend_file():
    """`api` is in BACKEND_DIRS, but a frontend app legitimately contains an
    api/ directory holding its HTTP client. One such path used to mark a whole
    storefront task as backend work and send it to the general coder, where it
    stalled in a tool loop (storefront, 2026-09-16). The markers are not
    symmetric -- a backend never contains components/ or pages/ -- so the
    frontend one wins."""
    from agent.frontend_route import _is_backend_path, _is_frontend_path
    assert _is_frontend_path("apps/storefront/src/api/client.ts")
    assert not _is_backend_path("apps/storefront/src/api/client.ts")
    # and the real backend is untouched
    assert _is_backend_path("apps/api/src/catalog/products.service.ts")
    assert _is_backend_path("apps/api/prisma/schema.prisma")


def test_the_storefront_task_that_stalled_now_routes_frontend():
    text = ("Make a dead API visible on the storefront instead of silently empty. "
            "apps/storefront/src/api/client.ts, apps/storefront/src/pages/HomePage.tsx, "
            "apps/storefront/src/pages/ShopPage.tsx. The health endpoint and its controller "
            "already work; this is about what the user sees.")
    d = classify_frontend(text)
    assert d.is_frontend, d.reason
