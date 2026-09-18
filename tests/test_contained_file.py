"""paths.contained_file -- the one place a name becomes a path.

It exists because there were three hand-written versions of this check, each
slightly different, and "slightly different" is how one of them ends up wrong.
Ten of the repository's code-scanning alerts were those versions: correct to a
reader, invisible to the analyser, and in one case genuinely weaker than it
looked (a symlink inside the directory would have passed a `.parent ==`
check).

What it guards is not hypothetical. The names reaching it are a project name
from a config file and an archive filename from an HTTP request, and the
operations behind it are "write a private key here", "read this", "delete
this".
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import paths


@pytest.fixture
def base(tmp_path):
    d = tmp_path / "box"
    d.mkdir()
    (d / "ok.json").write_text("{}")
    (tmp_path / "outside.json").write_text("secret")
    return d


def test_a_plain_name_resolves_inside(base):
    got = paths.contained_file(base, "ok.json")
    assert got.name == "ok.json"
    assert got.read_text() == "{}"


def test_a_name_for_a_file_that_does_not_exist_yet_is_fine(base):
    """Callers write as well as read -- the check is about WHERE, not whether
    it is there already."""
    got = paths.contained_file(base, "new.json")
    assert got.parent == Path(os.path.normpath(str(base)))


@pytest.mark.parametrize("name", [
    "../outside.json",           # the obvious one
    "../../etc/passwd",
    "sub/deeper.json",           # not a single component
    "sub\\deeper.json",
    "/etc/passwd",               # absolute
    "",                          # empty
    ".",
    "..",
])
def test_anything_that_is_not_one_component_inside_is_refused(base, name):
    with pytest.raises(paths.UnsafePath):
        paths.contained_file(base, name)


def test_a_symlink_pointing_out_of_the_directory_is_refused(base, tmp_path):
    """The check the old spelling did not make. `Path.resolve()` followed the
    link and `.parent == base` then compared the RESOLVED parent, so a link
    inside the directory pointing anywhere passed as 'directly inside'."""
    os.symlink(tmp_path / "outside.json", base / "link.json")
    with pytest.raises(paths.UnsafePath):
        paths.contained_file(base, "link.json")


def test_a_symlink_to_a_sibling_inside_the_directory_is_allowed(base):
    """Containment, not a ban on links: one pointing at a file in the same
    directory is still inside it."""
    os.symlink(base / "ok.json", base / "alias.json")
    assert paths.contained_file(base, "alias.json").name == "alias.json"


def test_a_directory_that_is_itself_a_symlink_still_works(tmp_path):
    """AGENT_KEYS_DIR and AGENT_ARCHIVE_DIR are operator-set and may well be
    links. Resolving only the target would make every name look like an
    escape."""
    real = tmp_path / "real"
    real.mkdir()
    (real / "ok.json").write_text("{}")
    link = tmp_path / "linked"
    os.symlink(real, link)
    assert paths.contained_file(link, "ok.json").read_text() == "{}"


def test_the_error_names_what_was_refused_without_inventing_a_path(base):
    with pytest.raises(paths.UnsafePath) as e:
        paths.contained_file(base, "../outside.json")
    assert "outside.json" in str(e.value)


# --- the callers ----------------------------------------------------------

def test_deploy_keys_refuses_a_traversing_project_name(tmp_path, monkeypatch):
    from agent import deploy_keys

    monkeypatch.setattr(deploy_keys, "KEYS_DIR", tmp_path)
    for bad in ("../x", "a/b", ".hidden", ""):
        with pytest.raises(deploy_keys.DeployKeyError):
            deploy_keys._key_path(bad)
        with pytest.raises(deploy_keys.DeployKeyError):
            deploy_keys._pub_path(bad)


def test_deploy_keys_builds_both_halves_through_the_same_door(tmp_path, monkeypatch):
    """remove_key used to assemble the .pub name itself, which is the kind of
    second spelling that drifts from the first."""
    from agent import deploy_keys

    monkeypatch.setattr(deploy_keys, "KEYS_DIR", tmp_path)
    priv = deploy_keys._key_path("demo")
    pub = deploy_keys._pub_path("demo")
    assert priv.parent == pub.parent == Path(os.path.normpath(str(tmp_path)))
    assert pub.name == priv.name + ".pub"


def test_archives_refuse_a_traversing_filename(tmp_path, monkeypatch):
    from agent import project_removal as pr

    monkeypatch.setattr(pr, "ARCHIVE_DIR", tmp_path)
    for bad in ("../secrets.json", "a/b.json", "..", "/etc/passwd"):
        with pytest.raises(pr.RemovalError):
            pr.read_archive(bad)
        with pytest.raises(pr.RemovalError):
            pr.delete_archive(bad)


# --- what _run will and will not start -------------------------------------

def test_run_starts_only_the_two_programs_this_module_uses():
    """It configures git and mints ssh keys. A third program reaching it means
    something is wrong upstream, and the list is short enough to be a literal."""
    from agent import deploy_keys

    ok, detail = deploy_keys._run(["curl", "https://example.com"])
    assert ok is False
    assert "not a command this module runs" in detail

    ok, detail = deploy_keys._run([])
    assert ok is False and "no command" in detail


@pytest.mark.parametrize("bad", [
    "has\nnewline",
    "has\x00null",
    "semi;colon",
    "back`tick`",
    "dollar$(sub)",
    "pipe|it",
])
def test_run_refuses_arguments_with_characters_it_has_no_use_for(bad):
    """There is no shell here -- subprocess gets a list -- so this is not about
    quoting. It is about a value arriving somewhere it was never meant to,
    which is easier to notice at the boundary than to reason about at each
    call site."""
    from agent import deploy_keys

    ok, detail = deploy_keys._run(["git", "config", bad])
    assert ok is False
    assert "not allowed" in detail
    # The suspect value is not echoed back.
    assert bad not in detail


def test_run_refuses_a_path_shaped_argument_that_starts_with_a_dash():
    """Option injection: ssh-keygen would read `-f/evil/key` as a flag, not a
    filename."""
    from agent import deploy_keys

    ok, detail = deploy_keys._run(["ssh-keygen", "-lf", "-/tmp/evil.key"])
    assert ok is False and "starts with" in detail


def test_run_still_accepts_every_argument_shape_this_module_really_passes(tmp_path):
    """The guard is worthless if it rejects the real calls, and each of these
    is a literal from somewhere in this file.

    `cwd=tmp_path` is not tidiness. Two of these are real `git config` writes,
    and without it they ran in whatever repository the suite was started from
    -- which is this one. That is exactly what happened: the suite wrote a
    core.sshCommand pointing at a key path invented for this test into the
    working checkout, and every later push warned about a missing identity
    file. A test that reaches outside its own directory is a test that edits
    the machine it is run on.
    """
    from agent import deploy_keys

    for args in (
        ["git", "config", "--local", "--get", "remote.origin.url"],
        ["git", "config", "core.sshCommand",
         "ssh -i /home/x/keys/demo.key -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"],
        ["ssh-keygen", "-lf", "/home/x/keys/demo.key"],
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-q", "-C", "tektonix-demo",
         "-f", "/home/x/keys/demo.key"],
    ):
        ok, detail = deploy_keys._run(args, cwd=str(tmp_path), timeout=1)
        # It may fail because the file is not there; it must not be refused.
        assert "refusing to run" not in detail, f"{args} was refused: {detail}"
