"""The signup endpoint.

Everything here follows from one fact: the landing page ships no JavaScript,
so this is a plain HTML form POST. A browser, not a client library, is what
sends it and what reads the answer -- which is why every outcome has to be a
redirect to a page a person can read, and why a bad address must not produce a
422 nobody sees.

Run: site/server/.venv/bin/python -m pytest site/server/test_newsletter.py
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
newsletter = importlib.import_module("newsletter")


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, store, fail=False):
        self.store, self.fail = store, fail

    async def execute(self, sql, params=None):
        if self.fail and "INSERT" in sql:
            import psycopg
            raise psycopg.OperationalError("no")
        if "INSERT" in sql:
            email, name, token, source = params
            self.store[email] = {"name": name, "token": token, "source": source}
        if "count(*)" in sql:
            return FakeCursor([(len(self.store),)])
        return FakeCursor([])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakePool:
    def __init__(self, fail=False):
        self.store: dict[str, dict] = {}
        self.fail = fail

    def connection(self):
        return FakeConn(self.store, self.fail)


@pytest.fixture(autouse=True)
def _fresh_limiter():
    # The limiter is process-global. Without a reset, a test that posts
    # five times poisons every later test that shares the TestClient IP.
    newsletter.reset_subscribe_limiter()
    yield
    newsletter.reset_subscribe_limiter()


def _post(client, **form):
    # follow_redirects off: the redirect IS the behaviour under test.
    return client.post("/subscribe", data=form, follow_redirects=False)


def test_a_good_signup_is_stored_and_lands_on_a_real_page(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada Lovelace", email="Ada@Example.COM")

    assert r.status_code == 303, "a browser sent this; it needs somewhere to go"
    assert r.headers["location"] == newsletter.OK_URL
    # Lower-cased on the way in, or the same person signs up twice.
    assert "ada@example.com" in pool.store
    assert pool.store["ada@example.com"]["name"] == "Ada Lovelace"
    assert len(pool.store["ada@example.com"]["token"]) > 20, "no unsubscribe token minted"


@pytest.mark.parametrize("email", ["", "nope", "no@domain", "a b@c.com", "@example.com"])
def test_an_address_that_is_not_one_is_refused_to_a_page(monkeypatch, email):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="Ada", email=email)

    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL
    assert pool.store == {}, "a bad address was stored anyway"


def test_a_missing_name_is_refused(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    r = _post(TestClient(newsletter.app), name="   ", email="ada@example.com")
    assert r.headers["location"] == newsletter.FAIL_URL
    assert pool.store == {}


def test_nothing_is_ever_answered_with_a_bare_status(monkeypatch):
    """The form has no JavaScript behind it. Any answer that is not a redirect
    leaves somebody looking at a JSON blob or a blank page."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    for form in ({"name": "A", "email": "a@b.co"}, {"name": "", "email": ""},
                 {"name": "A", "email": "bad"}):
        r = _post(TestClient(newsletter.app), **form)
        assert r.status_code == 303, f"{form} answered {r.status_code}"
        assert r.headers["location"].startswith("https://"), "redirect must be absolute"


def test_a_database_failure_is_a_page_not_a_500(monkeypatch):
    monkeypatch.setattr(newsletter, "pool", FakePool(fail=True))
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL


def test_no_database_at_all_is_a_page_not_a_crash(monkeypatch):
    monkeypatch.setattr(newsletter, "pool", None)
    r = _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL


def test_the_insert_treats_a_repeat_as_an_update_not_a_failure(monkeypatch):
    """Already on the list is what the person wanted. An error would send them
    away believing it had not worked."""
    captured = {}

    class Recording(FakePool):
        def connection(self):
            outer = self

            class C(FakeConn):
                async def execute(self, sql, params=None):
                    if "INSERT" in sql:
                        captured["sql"] = sql
                    return await super().execute(sql, params)

            return C(outer.store)

    monkeypatch.setattr(newsletter, "pool", Recording())
    _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
    assert "ON CONFLICT (email) DO UPDATE" in captured["sql"]
    assert "unsubscribed_at = NULL" in captured["sql"], (
        "signing up again should undo an earlier unsubscribe")


def test_an_address_never_reaches_the_log(monkeypatch, caplog):
    """A log line is the easiest way for a mailing list to leak, into a file
    people tail over each other's shoulders."""
    monkeypatch.setattr(newsletter, "pool", FakePool())
    with caplog.at_level("INFO"):
        _post(TestClient(newsletter.app), name="Ada", email="ada@example.com")
        _post(TestClient(newsletter.app), name="Ada", email="rubbish")
    assert "ada@example.com" not in caplog.text
    assert "rubbish" not in caplog.text


def test_oversized_input_is_truncated_rather_than_rejected(monkeypatch):
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    _post(TestClient(newsletter.app), name="A" * 500, email="ada@example.com")
    assert len(pool.store["ada@example.com"]["name"]) == 120


def test_a_sixth_signup_from_one_ip_is_a_page_not_a_store(monkeypatch):
    """The landing page is a public form with no CAPTCHA. The sixth POST
    in a minute is refused the same way a bad address is -- a 303 to the
    failure page -- so a browser still has somewhere to go."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    client = TestClient(newsletter.app)
    for i in range(newsletter._SUBSCRIBE_MAX):
        r = _post(client, name="Ada", email=f"ada{i}@example.com")
        assert r.headers["location"] == newsletter.OK_URL
    assert len(pool.store) == newsletter._SUBSCRIBE_MAX
    r = _post(client, name="Ada", email="one-more@example.com")
    assert r.status_code == 303
    assert r.headers["location"] == newsletter.FAIL_URL
    assert "one-more@example.com" not in pool.store


def test_x_real_ip_is_the_rate_limit_key(monkeypatch):
    """nginx sets X-Real-IP from $remote_addr and overwrites anything the
    client sent. Two people behind the same TestClient socket are not one
    person, and a client that forges X-Forwarded-For is not a new bucket."""
    pool = FakePool()
    monkeypatch.setattr(newsletter, "pool", pool)
    client = TestClient(newsletter.app)
    for i in range(newsletter._SUBSCRIBE_MAX):
        _post(client, name="Ada", email=f"a{i}@example.com")
    # Same socket, different real IP: a new window.
    r = client.post(
        "/subscribe",
        data={"name": "Ada", "email": "other@example.com"},
        headers={"X-Real-IP": "203.0.113.9"},
        follow_redirects=False,
    )
    assert r.headers["location"] == newsletter.OK_URL
    assert "other@example.com" in pool.store
    # Forged XFF must not open a new window on the TestClient IP.
    r = client.post(
        "/subscribe",
        data={"name": "Ada", "email": "forged@example.com"},
        headers={"X-Forwarded-For": "198.51.100.1"},
        follow_redirects=False,
    )
    assert r.headers["location"] == newsletter.FAIL_URL
    assert "forged@example.com" not in pool.store
