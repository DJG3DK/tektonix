"""scripts/doctor.py: does it catch the misconfigurations, and does it keep
its one promise -- never printing a secret?

Each check exists because its failure appears somewhere else entirely: a
mismatched router key looks like every model call failing, a mismatched review
secret looks like a task that builds and reviews and is then refused at the
merge. These tests build small fake installations and assert the finding.
"""
import base64
import importlib.util
import json
import secrets
import sys
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "doctor", Path(__file__).resolve().parent.parent / "scripts" / "doctor.py")
doctor = importlib.util.module_from_spec(SPEC)
sys.modules["doctor"] = doctor
SPEC.loader.exec_module(doctor)

GOOD_KEY = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
ROUTER_KEY = "sk-" + secrets.token_hex(24)
REVIEW_SECRET = secrets.token_hex(32)


def install(tmp_path, *, agent_env=None, router_env=None, shared_env=None,
            projects=None, mode=0o600):
    """A fake installation root, with only the files a test cares about."""
    root = tmp_path / "install"
    (root / "services/llm-router").mkdir(parents=True)
    (root / "services/shared").mkdir(parents=True)

    def write(path: Path, values: dict | None):
        if values is None:
            return
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
        path.chmod(mode)

    write(root / ".env", {
        "LANGGRAPH_PG_DSN": "postgresql://u:p@localhost:5432/db",
        "MODEL_ROUTER_URL": "http://127.0.0.1:4000/v1",
        "MODEL_ROUTER_KEY": ROUTER_KEY,
        "AUTH_SECRET_KEY": GOOD_KEY,
        "ADMIN_EMAIL": "a@e.com",
        "REVIEW_CONTROL_SECRET": REVIEW_SECRET,
        **(agent_env or {}),
    } if agent_env != "absent" else None)
    write(root / "services/llm-router/.env", {
        "OPENROUTER_API_KEY": "sk-or-" + secrets.token_hex(16),
        "MODEL_ROUTER_KEY": ROUTER_KEY,
        **(router_env or {}),
    } if router_env != "absent" else None)
    write(root / "services/shared/.env",
          {"REVIEW_CONTROL_SECRET": REVIEW_SECRET, **(shared_env or {})}
          if shared_env != "absent" else None)

    if projects is not None:
        (root / "projects.json").write_text(json.dumps({"projects": projects}))
    return root


@pytest.fixture
def at(monkeypatch):
    """Point the doctor at a fake root, and stub the checks that reach out to
    the world so a test is about configuration, not this machine."""
    def _at(root: Path, *, skip_external=True):
        monkeypatch.setattr(doctor, "ROOT", root)
        monkeypatch.setattr(doctor, "AGENT_ENV", root / ".env")
        monkeypatch.setattr(doctor, "ROUTER_ENV", root / "services/llm-router/.env")
        monkeypatch.setattr(doctor, "SHARED_ENV", root / "services/shared/.env")
        monkeypatch.setattr(doctor, "PROJECTS_JSON", root / "projects.json")
        monkeypatch.setattr(doctor, "KEYS_DIR", root / "keys")
        monkeypatch.setattr(doctor, "REVIEW_SECRETS", root / "services/commit-reviewer/review-secrets")
        if skip_external:
            monkeypatch.setattr(doctor, "CHECKS", tuple(
                c for c in doctor.CHECKS
                if c.__name__ not in ("check_pm2", "check_sandbox_image", "check_dashboard")))
        return doctor.run_all()
    return _at


def findings(report, level=None):
    return [(n, d) for lv, n, d in report.findings if level is None or lv == level]


def test_a_healthy_installation_reports_no_failures(at, tmp_path):
    report = at(install(tmp_path, projects={}))
    assert not report.failed, findings(report, doctor.FAIL)


def test_a_router_key_mismatch_is_caught_and_explained(at, tmp_path):
    root = install(tmp_path, router_env={"MODEL_ROUTER_KEY": "sk-" + secrets.token_hex(24)})
    report = at(root)
    assert report.failed
    hit = [d for n, d in findings(report, doctor.FAIL) if "router key MISMATCH" in n]
    assert hit and "401" in hit[0], "the finding must say what the operator will actually see"


def test_a_review_secret_mismatch_is_caught(at, tmp_path):
    root = install(tmp_path, shared_env={"REVIEW_CONTROL_SECRET": secrets.token_hex(32)})
    report = at(root)
    assert report.failed
    assert any("review secret MISMATCH" in n for n, _ in findings(report, doctor.FAIL))


def test_the_legacy_secret_location_is_a_warning_not_a_failure(at, tmp_path):
    """An upgrade that has not re-run the installer still works; it should be
    told to move the secret, not told it is broken."""
    root = install(tmp_path, router_env={"REVIEW_CONTROL_SECRET": REVIEW_SECRET})
    report = at(root)
    assert not report.failed
    assert any("still in services/model-router/.env" in n for n, _ in findings(report, doctor.WARN))


def test_a_short_auth_key_is_caught_with_the_openssl_trap_named(at, tmp_path):
    """`openssl rand -hex 32` produces 48 bytes and bricks 2FA setup -- the
    exact mistake this check exists for."""
    root = install(tmp_path, agent_env={"AUTH_SECRET_KEY": base64.urlsafe_b64encode(b"x" * 48).decode()})
    report = at(root)
    assert report.failed
    hit = [d for n, d in findings(report, doctor.FAIL) if "AUTH_SECRET_KEY decodes to 48" in n]
    assert hit and "openssl" in hit[0]


def test_a_non_base64_auth_key_is_caught(at, tmp_path):
    report = at(install(tmp_path, agent_env={"AUTH_SECRET_KEY": "not base64 at all!!"}))
    assert report.failed


def test_missing_required_values_are_named(at, tmp_path):
    root = install(tmp_path, agent_env={"LANGGRAPH_PG_DSN": "", "ADMIN_EMAIL": ""})
    report = at(root)
    hit = [d for n, d in findings(report, doctor.FAIL) if "missing required values" in n]
    assert hit and "LANGGRAPH_PG_DSN" in hit[0] and "ADMIN_EMAIL" in hit[0]


def test_a_world_readable_env_is_a_failure(at, tmp_path):
    report = at(install(tmp_path, mode=0o644))
    assert report.failed
    assert any("mode 644" in n for n, _ in findings(report, doctor.FAIL))


def test_a_project_pointing_at_nothing_is_caught(at, tmp_path):
    report = at(install(tmp_path, projects={"ghost": {"live": "/nowhere/x", "sandbox": "/nowhere/y"}}))
    assert report.failed
    hit = [d for n, d in findings(report, doctor.FAIL) if n == "project ghost"]
    assert hit and "live" in hit[0] and "sandbox" in hit[0]


def test_a_live_path_that_is_not_a_git_checkout_is_caught(at, tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    (tmp_path / "sandbox").mkdir()
    report = at(install(tmp_path, projects={"p": {"live": str(live), "sandbox": str(tmp_path / "sandbox")}}))
    assert any("not a git checkout" in d for _, d in findings(report, doctor.FAIL))


def test_unparseable_projects_json_fails_rather_than_raising(at, tmp_path):
    root = install(tmp_path, projects={})
    (root / "projects.json").write_text("{ not json")
    report = at(root)
    assert any("does not parse" in n for n, _ in findings(report, doctor.FAIL))


def test_one_broken_check_does_not_hide_the_others(at, tmp_path, monkeypatch):
    def explodes(report):
        raise RuntimeError("boom")

    monkeypatch.setattr(doctor, "CHECKS", (explodes, doctor.check_agent_env))
    report = at(install(tmp_path), skip_external=False)
    assert any("explodes raised" in n for n, _ in findings(report, doctor.FAIL))
    assert any(".env has every required value" in n for n, _ in findings(report))


# ---------------------------------------------------------------------------
# the promise: no secret ever reaches the output
# ---------------------------------------------------------------------------

def test_no_secret_value_appears_in_the_output(at, tmp_path):
    report = at(install(tmp_path, projects={}))
    out = doctor.render(report)
    for secret in (GOOD_KEY, ROUTER_KEY, REVIEW_SECRET):
        assert secret not in out
        assert secret[:12] not in out


def test_a_mismatch_report_still_leaks_nothing(at, tmp_path):
    other = secrets.token_hex(32)
    report = at(install(tmp_path, shared_env={"REVIEW_CONTROL_SECRET": other}))
    out = doctor.render(report)
    assert REVIEW_SECRET not in out and other not in out
    assert "MISMATCH" in out


def test_fingerprints_compare_without_revealing():
    a, b = secrets.token_hex(32), secrets.token_hex(32)
    assert doctor.fingerprint(a) == doctor.fingerprint(a)
    assert doctor.fingerprint(a) != doctor.fingerprint(b)
    assert len(doctor.fingerprint(a)) == 8
    assert a[:8] not in doctor.fingerprint(a)


def test_the_renderer_refuses_to_print_a_report_that_looks_like_it_leaked():
    """Last line of defence: if a future check ever puts a secret in a detail
    string, the tool says so instead of printing it."""
    leaked = doctor.Report()
    leaked.fail("careless check", "the key is sk-" + "a" * 40)
    out = doctor.render(leaked)
    assert "sk-" + "a" * 40 not in out
    assert "refusing to print" in out


def test_exit_code_is_zero_for_warnings_and_one_for_failures(at, tmp_path):
    warned = doctor.Report()
    warned.warn("something worth saying")
    assert warned.failed is False

    failed = doctor.Report()
    failed.fail("something broken")
    assert failed.failed is True
