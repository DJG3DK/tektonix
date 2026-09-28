"""The Environment page in the compose bundle (2026-09-28, the first Windows
install): the OpenRouter key showed "not set" although compose had passed
it, and saving answered "could not write the env file" because none of the
.env files the editor writes exist in the container. In the bundle the page
shows what the process was given and points at the host's .env; a write
is a 409 with the same sentence."""
import dataclasses

from fastapi.testclient import TestClient

import agent.env_config as ec
import agent.server as srv
from agent.auth import User


def _admin():
    return User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


def test_in_the_bundle_the_page_shows_what_compose_provided_and_names_the_host_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-0123456789abcdef")
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setattr(ec, "ROUTER_ENV", tmp_path / "missing" / ".env")
    monkeypatch.setattr(ec, "AGENT_ENV", tmp_path / "missing" / ".env")
    rows = {r["key"]: r for r in ec.list_keys()}
    assert rows["OPENROUTER_API_KEY"]["is_set"] is True
    assert rows["OPENROUTER_API_KEY"]["display"].endswith("cdef") and "0123" not in rows["OPENROUTER_API_KEY"]["display"]
    assert rows["OPENROUTER_API_KEY"]["file"] == "the host's .env (docker compose)"
    assert rows["LANGSMITH_API_KEY"]["is_set"] is False


def test_in_the_bundle_a_write_is_refused_with_where_to_change_it(monkeypatch, tmp_path):
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    monkeypatch.setattr(ec, "ROUTER_ENV", tmp_path / ".env")
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.get_current_user, _admin)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, _admin)
    c = TestClient(srv.app)
    r = c.get("/api/env-config")
    assert r.status_code == 200 and r.json()["managed_by"] == "compose" and "docker compose up -d" in r.json()["note"]
    r = c.post("/api/env-config", json={"updates": {"OPENROUTER_API_KEY": "sk-new"}})
    assert r.status_code == 409 and "docker-compose.yml" in r.json()["detail"]
    assert not (tmp_path / ".env").exists(), "nothing was written"


def test_on_a_host_install_the_files_are_still_the_source(monkeypatch, tmp_path):
    monkeypatch.delenv("TEKTONIX_BUNDLE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-the-process-not-the-file")
    env = tmp_path / ".env"
    env.write_text("OPENROUTER_API_KEY=sk-or-v1-fromfile9999\n")
    # The key table holds its paths; point every entry at the temp files.
    keys = tuple(dataclasses.replace(mk, path=env if mk.path == ec.ROUTER_ENV else tmp_path / "agent.env")
                 for mk in ec.MANAGED_KEYS)
    monkeypatch.setattr(ec, "MANAGED_KEYS", keys)
    monkeypatch.setattr(ec, "_BY_KEY", {k.key: k for k in keys})
    rows = {r["key"]: r for r in ec.list_keys()}
    assert rows["OPENROUTER_API_KEY"]["display"].endswith("9999") and rows["OPENROUTER_API_KEY"]["file"] == str(env)
    assert ec.set_keys({"OPENROUTER_API_KEY": "sk-or-v1-written00"})["updated"] == ["OPENROUTER_API_KEY"]
    assert "sk-or-v1-written00" in env.read_text()
