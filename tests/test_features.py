"""Accounts beyond the first are a licensed feature (agent/features.py).
The public build refuses to mint them and does not show the page; a
licensed deployment names the feature in TEKTONIX_FEATURES. Existing
accounts are never affected."""
from fastapi.testclient import TestClient

import agent.server as srv
from agent import features
from agent.auth import User

_ADMIN = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


def test_the_switch_reads_a_comma_list_and_is_off_by_default(monkeypatch):
    monkeypatch.delenv("TEKTONIX_FEATURES", raising=False)
    assert not features.enabled(features.MULTI_USER) and features.public() == {"multi_user": False}
    monkeypatch.setenv("TEKTONIX_FEATURES", " Multi-User , other ")
    assert features.enabled(features.MULTI_USER) and features.public() == {"multi_user": True}


def test_the_public_build_refuses_to_create_an_account_and_says_why(monkeypatch):
    monkeypatch.delenv("TEKTONIX_FEATURES", raising=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _ADMIN)
    monkeypatch.setattr(srv.app.state, "auth_pool", object(), raising=False)
    c = TestClient(srv.app)
    r = c.post("/api/auth/users", json={"email": "x@y.co", "password": "Long-enough-passw0rd!", "role": "user", "allowed_repos": []})
    assert r.status_code == 403 and "licensed feature" in r.json()["detail"]


def test_the_bundle_passes_the_switch_from_the_host_env_to_the_agent():
    """The agent service lists its environment explicitly, so a variable
    missing from the list never reaches the container: a licensed bundle
    with TEKTONIX_FEATURES in its .env still got a 403 (2026-09-29)."""
    import yaml
    from agent import paths

    compose = yaml.safe_load((paths.REPO_ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["TEKTONIX_FEATURES"] == "${TEKTONIX_FEATURES:-}", \
        "empty unless the host's .env sets it"


def test_the_dashboard_is_told_which_features_this_deployment_has(monkeypatch):
    from agent.routers.auth import _user_public

    monkeypatch.delenv("TEKTONIX_FEATURES", raising=False)
    assert _user_public(_ADMIN)["features"] == {"multi_user": False}
    monkeypatch.setenv("TEKTONIX_FEATURES", "multi-user")
    assert _user_public(_ADMIN)["features"] == {"multi_user": True}
