"""Renaming a stored GitHub token.

Operators rename tokens in GitHub as their understanding of what each one is
for improves, and the names here have to be able to follow. Without a rename
the only route was remove-and-re-add: paste the secret again, and lose every
project mapped to it on the way.
"""
from __future__ import annotations

import base64
import secrets
from types import SimpleNamespace

import pytest

from agent import github_settings as gs


def _config():
    # Same shape the other settings tests use: a real Fernet-derivable key,
    # because encrypt/decrypt is exercised here rather than mocked.
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    return SimpleNamespace(auth_secret_key=key, github_token=None)


@pytest.fixture
def settings():
    cfg = _config()
    s = gs.normalize({})
    s = gs.apply_patch(cfg, s, {"add_tokens": {"old-name": "github_pat_" + "x" * 30}})
    s = gs.apply_patch(cfg, s, {"projects": {"demo": {"token": "old-name"}}})
    return cfg, s


def test_a_rename_keeps_the_secret_and_the_projects_pointing_at_it(settings):
    cfg, s = settings
    before = s["tokens"]["old-name"]["enc"]

    out = gs.apply_patch(cfg, s, {"rename_tokens": {"old-name": "DJG3dk-Projects"}})

    assert "old-name" not in out["tokens"]
    assert out["tokens"]["DJG3dk-Projects"]["enc"] == before, "the secret was not re-encrypted"
    assert out["projects"]["demo"]["token"] == "DJG3dk-Projects", (
        "a project points at a token BY NAME -- if the rename does not follow, "
        "it silently falls back to the environment token"
    )


def test_renaming_onto_an_existing_name_is_refused(settings):
    cfg, s = settings
    s = gs.apply_patch(cfg, s, {"add_tokens": {"other": "github_pat_" + "y" * 30}})
    with pytest.raises(ValueError, match="already exists"):
        gs.apply_patch(cfg, s, {"rename_tokens": {"old-name": "other"}})


def test_renaming_something_that_is_not_there_is_refused(settings):
    cfg, s = settings
    with pytest.raises(ValueError, match="no token named"):
        gs.apply_patch(cfg, s, {"rename_tokens": {"ghost": "whatever"}})


def test_renaming_to_the_same_name_is_a_no_op(settings):
    cfg, s = settings
    out = gs.apply_patch(cfg, s, {"rename_tokens": {"old-name": "old-name"}})
    assert "old-name" in out["tokens"]
    assert out["projects"]["demo"]["token"] == "old-name"


@pytest.mark.parametrize("bad", ["", "   ", "x" * 41])
def test_a_name_that_is_empty_or_too_long_is_refused(settings, bad):
    cfg, s = settings
    with pytest.raises(ValueError, match="1-40 characters"):
        gs.apply_patch(cfg, s, {"rename_tokens": {"old-name": bad}})
