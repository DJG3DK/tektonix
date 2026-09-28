"""The bundle's Models page (2026-09-28, the first Windows install): a 500,
because the agent read the router's config from a host path the container
does not have. The config and the ledger are shared volumes now, the
router seeds the config once, and the restart button explains itself."""
import os
import subprocess
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from agent import paths

REPO = paths.REPO_ROOT


def test_the_router_config_and_ledger_are_shared_volumes():
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    s = compose["services"]
    assert "routerconfig:/app/router-config" in s["router"]["volumes"]
    assert "routerconfig:/app/router-config" in s["agent"]["volumes"]
    assert "routerlogs:/app/router-logs:ro" in s["agent"]["volumes"]
    assert any(v.endswith(":/app/config.seed.yaml:ro") for v in s["router"]["volumes"]), "the operator's file is the seed"
    assert s["agent"]["environment"]["MODEL_ROUTER_CONFIG_PATH"] == "/app/router-config/config.yaml"
    assert s["agent"]["environment"]["MODEL_ROUTER_LEDGER"] == "/app/router-logs/routing.jsonl"
    assert "routerconfig" in compose["volumes"]
    dockerfile = (REPO / "docker/router/Dockerfile").read_text()
    assert "MODEL_ROUTER_CONFIG=/app/router-config/config.yaml" in dockerfile and "entrypoint.sh" in dockerfile


def _seed(tmp_path: Path, live_exists: bool) -> tuple[str, str]:
    seed = tmp_path / "seed.yaml"
    seed.write_text("model_list: [seed]\n")
    live = tmp_path / "vol" / "config.yaml"
    if live_exists:
        live.parent.mkdir()
        live.write_text("model_list: [the operators pins]\n")
    # `exec uvicorn` is replaced by a stand-in on PATH so only the seeding runs.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    (fake_bin / "uvicorn").write_text("#!/bin/sh\necho started\n")
    (fake_bin / "uvicorn").chmod(0o755)
    r = subprocess.run(["sh", str(REPO / "docker/router/entrypoint.sh")], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}",
                            "MODEL_ROUTER_CONFIG_SEED": str(seed), "MODEL_ROUTER_CONFIG": str(live)})
    assert r.returncode == 0, r.stderr
    return r.stdout, live.read_text()


def test_the_router_seeds_its_config_once_and_never_overwrites_the_operators_pins(tmp_path):
    out, live = _seed(tmp_path, live_exists=False)
    assert "seeded" in out and live == "model_list: [seed]\n"
    (tmp_path / "second").mkdir()
    out, live = _seed(tmp_path / "second", live_exists=True)
    assert "seeded" not in out and "the operators pins" in live


def test_in_the_bundle_the_restart_button_says_the_pin_is_already_live(monkeypatch):
    import agent.server as srv
    from agent.auth import User

    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    r = TestClient(srv.app).post("/api/model-config/restart-router")
    assert r.status_code == 409 and "docker compose restart router" in r.json()["detail"]


def test_in_the_bundle_the_page_is_told_there_is_no_restart_to_offer(monkeypatch):
    import agent.server as srv
    from agent import model_config
    from agent.auth import User

    async def pins():
        return {}

    me = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.auth.require_full_auth, lambda: me)
    monkeypatch.setattr(model_config, "get_current_pins_priced", pins)
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    r = TestClient(srv.app).get("/api/model-config")
    assert r.status_code == 200 and r.json()["router_restart"]["available"] is False
    assert "already live" in r.json()["router_restart"]["note"]
    monkeypatch.delenv("TEKTONIX_BUNDLE")
    assert TestClient(srv.app).get("/api/model-config").json()["router_restart"] == {"available": True, "note": None}


def test_the_agent_is_given_the_router_s_v1_base_like_a_host_install():
    """The first task of the first Windows install failed with the router's
    own 404: the agent's OpenAI client appends /chat/completions to its base
    URL, and compose gave it the bare origin."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["MODEL_ROUTER_URL"] == "http://router:4001/v1"
    assert (REPO / ".env.example").read_text().count("MODEL_ROUTER_URL=http://127.0.0.1:4001/v1") == 1
