"""scripts/doctor.py fails an install whose nginx still injects the review secret."""
import importlib.util
import pathlib

SPEC = importlib.util.spec_from_file_location("doctor", pathlib.Path("scripts/doctor.py"))


def _doctor():
    mod = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(mod)
    return mod


class _Report:
    def __init__(self):
        self.lines = []

    def ok(self, *a):
        self.lines.append(("ok", *a))

    def warn(self, *a):
        self.lines.append(("warn", *a))

    def fail(self, *a):
        self.lines.append(("fail", *a))


def test_an_injecting_vhost_fails_and_names_the_file(tmp_path):
    d = _doctor()
    site = tmp_path / "sites-enabled"
    site.mkdir()
    (site / "tektonix.conf").write_text("location /_review/ {\n    proxy_set_header   X-Review-Secret abc;\n}\n")
    r = _Report()
    d.check_nginx_review_block(r, nginx_root=str(tmp_path))
    assert r.lines[0][0] == "fail" and "tektonix.conf" in r.lines[0][2] and "rotate" in r.lines[0][2]


def test_a_clean_vhost_passes_and_no_nginx_is_fine(tmp_path):
    d = _doctor()
    r = _Report()
    d.check_nginx_review_block(r, nginx_root=str(tmp_path / "absent"))
    assert r.lines == []
    (tmp_path / "a.conf").write_text("location / { proxy_pass http://127.0.0.1:8100; }\n")
    d.check_nginx_review_block(r, nginx_root=str(tmp_path))
    assert r.lines[-1][0] == "ok"
