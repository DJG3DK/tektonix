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
