"""Code scanning (2026-09-27) flagged every route that turns a request value
into a file name, although each one matches the value against a pattern
without a separator first. safe_path.under says the same guarantee the way
an analyser recognises -- normalise, then check the prefix -- and the
routes go through it."""
import pytest

from agent import safe_path


def test_a_plain_name_lands_under_the_root(tmp_path):
    assert safe_path.under(tmp_path, "run-1", "summary.json") == tmp_path / "run-1" / "summary.json"
    assert safe_path.under(tmp_path) == tmp_path


@pytest.mark.parametrize("bad", ["../etc", "a/../../b", "/etc/passwd", "..", "x/../.."])
def test_anything_that_leaves_the_root_is_refused(tmp_path, bad):
    with pytest.raises(safe_path.PathOutsideRoot):
        safe_path.under(tmp_path, bad)


def test_a_sibling_with_the_root_as_a_prefix_is_outside(tmp_path):
    with pytest.raises(safe_path.PathOutsideRoot):
        safe_path.under(tmp_path / "runs", "../runs2/x")


def test_the_swebench_instance_id_check_is_linear():
    import time

    from agent.routers.swebench import _is_instance_id

    assert _is_instance_id("astropy__astropy-14598") and _is_instance_id("django__django-11099")
    for bad in ("astropy-14598", "astropy__astropy", "astropy__astropy-x", "a/b__c-1", "../x__y-1"):
        assert not _is_instance_id(bad), bad
    crafted = "-__" * 5000
    t = time.perf_counter()
    assert not _is_instance_id(crafted)
    assert time.perf_counter() - t < 0.05


def test_an_artifact_id_is_compared_never_globbed(tmp_path, monkeypatch):
    from agent import artifacts
    from agent.config import PROJECTS

    monkeypatch.setattr(artifacts, "ROOT", tmp_path)
    monkeypatch.setitem(PROJECTS, "shop", {"live": str(tmp_path)})
    (tmp_path / "shop").mkdir()
    (tmp_path / "shop" / ("a" * 32 + ".png")).write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    assert artifacts.load("shop", "a" * 32) is not None
    assert artifacts.load("shop", "*") is None and artifacts.load("shop", "a" * 31 + "?") is None
    assert artifacts.load("nope", "a" * 32) is None


def test_a_link_inside_the_root_that_leaves_it_is_refused_only_when_resolved(tmp_path):
    """The lexical check cannot see a symlink. Where the path must exist,
    `resolve=True` follows it and compares the real paths."""
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir(), outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "link").symlink_to(outside)
    (root / "inner").mkdir()
    (root / "inner-link").symlink_to(root / "inner")
    assert safe_path.under(root, "link", "secret") == root / "link" / "secret"     # lexical: passes
    with pytest.raises(safe_path.PathOutsideRoot):
        safe_path.under(root, "link", "secret", resolve=True)
    assert safe_path.under(root, "inner-link", "f", resolve=True) == (root / "inner" / "f").resolve()
    assert safe_path.under(root, "missing", "f", resolve=True) == (root / "missing" / "f").resolve()
