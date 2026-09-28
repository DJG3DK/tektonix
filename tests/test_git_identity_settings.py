"""The agent's commits are the operator's: GIT_USER_NAME / GIT_USER_EMAIL,
from the app's form, the installer or .env, become git's own author and
committer variables on a host install (the bundle maps them in compose).
Blank leaves git's own config alone; an explicit git variable wins."""
from agent import config, env_config


def test_the_two_settings_become_gits_variables_when_those_are_unset():
    env = {"GIT_USER_NAME": "Danny", "GIT_USER_EMAIL": "danny@example.test"}
    config.apply_git_identity(env)
    assert env["GIT_AUTHOR_NAME"] == env["GIT_COMMITTER_NAME"] == "Danny"
    assert env["GIT_AUTHOR_EMAIL"] == env["GIT_COMMITTER_EMAIL"] == "danny@example.test"


def test_blank_settings_and_explicit_git_variables_are_left_alone():
    env = {"GIT_USER_NAME": " ", "GIT_USER_EMAIL": "", "GIT_AUTHOR_NAME": "Someone Else"}
    config.apply_git_identity(env)
    assert env == {"GIT_USER_NAME": " ", "GIT_USER_EMAIL": "", "GIT_AUTHOR_NAME": "Someone Else"}
    env = {"GIT_USER_NAME": "Danny", "GIT_AUTHOR_NAME": "Explicit"}
    config.apply_git_identity(env)
    assert env["GIT_AUTHOR_NAME"] == "Explicit" and env["GIT_COMMITTER_NAME"] == "Danny"


def test_the_environment_page_offers_both_as_plain_values():
    keys = {k.key: k for k in env_config.MANAGED_KEYS}
    assert keys["GIT_USER_NAME"].group == "Git" and keys["GIT_USER_NAME"].secret is False
    assert keys["GIT_USER_EMAIL"].restarts == ("tektonix",)
