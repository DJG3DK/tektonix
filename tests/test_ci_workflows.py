"""The two workflows, read as the claims they make.

A CI step that cannot fail is a step that lies, and the 2026-09-29 audit
found three: a bundle job whose service comparison counted the one-shot
image build as a crash (so the job never passed and nobody ran it), a
doctor step whose `grep -qv Traceback` passed on every traceback, and two
docker-backed suites that skipped on every run because nothing built the
image they wait for. These read the workflow files the way the runner
would, and run the one shell step whose logic was wrong.
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess

import pytest
import yaml

from agent import paths

REPO = paths.REPO_ROOT
CI = REPO / ".github/workflows/ci.yml"
RELEASE = REPO / ".github/workflows/release.yml"


def _jobs(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())["jobs"]


def _step(job: dict, name_part: str) -> dict:
    steps = [s for s in job["steps"] if name_part in (s.get("name") or "")]
    assert len(steps) == 1, f"{name_part!r}: {len(steps)} steps match"
    return steps[0]


# ---------------------------------------------------------------------------
# The docker jobs run on pushes, and the bundle job can pass
# ---------------------------------------------------------------------------

def test_the_bundle_and_sandbox_jobs_run_on_a_push_to_main_that_touches_the_bundle():
    jobs = _jobs(CI)
    gate = jobs["bundle-paths"]
    assert "push" in gate["if"] and "refs/heads/main" in gate["if"] and "workflow_dispatch" in gate["if"]
    decide = gate["steps"][-1]["run"]
    for path in ("docker", "docker-compose.yml", "agent"):
        assert re.search(rf"--[^\n]*\b{re.escape(path)}\b", decide), f"{path} does not trigger the docker jobs"
    for name in ("bundle", "sandbox"):
        assert jobs[name]["needs"] == "bundle-paths", name
        assert "bundle-paths.outputs.run" in jobs[name]["if"], name


def test_the_bundle_job_expects_a_one_shot_to_have_exited_rather_than_to_be_running():
    """sandbox-image has entrypoint true and restart "no": it exits at once
    by design. Diffing `config --services` against the running services
    listed it as missing on every run, so the job failed by construction."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    one_shots = {n for n, s in compose["services"].items() if str(s.get("restart")) == "no"}
    assert one_shots, "no one-shot service; this test guards the comparison against one"
    run = _step(_jobs(CI)["bundle"], "still running")["run"]
    assert "config --services" not in run, "the comparison counts one-shots as crashed again"
    assert 'restart != "no"' in run and 'restart == "no"' in run
    assert "exited 0" in run


def test_the_sandbox_job_builds_the_image_the_docker_suites_wait_for():
    job = _jobs(CI)["sandbox"]
    runs = "\n".join(s.get("run") or "" for s in job["steps"])
    assert "docker build -t tektonix-sandbox:latest docker/agent-sandbox" in runs
    for suite in ("tests/test_review_db_check_e2e.py", "tests/test_sandbox_gh.py"):
        assert suite in runs, suite
        assert (REPO / suite).exists(), suite
    assert "skipped" in runs, "a skipped suite must fail the job, or the gap is back"


# ---------------------------------------------------------------------------
# The doctor step, run for real
# ---------------------------------------------------------------------------

def _doctor_step_script() -> str:
    return _step(_jobs(CI)["shell"], "Doctor runs")["run"]


def _run_doctor_step(tmp_path: pathlib.Path, fake_doctor: str) -> int:
    """The step's shell, under the runner's own flags (bash -eo pipefail),
    with `python3` replaced by a stub that behaves like a given doctor."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "python3"
    stub.write_text("#!/usr/bin/env bash\n" + fake_doctor + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = _doctor_step_script().replace("/tmp/doctor.out", str(tmp_path / "doctor.out"))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, cwd=REPO,
                          capture_output=True, text=True).returncode


def test_the_doctor_step_passes_on_findings_and_fails_on_a_traceback(tmp_path):
    """`| grep -qv Traceback` succeeds whenever ANY output line lacks the
    word, which is true of every traceback. The step was green on a doctor
    that crashed. Now: findings (exit 1) pass, a traceback fails, and so
    does any exit code the doctor does not document."""
    assert "grep -qv" not in _doctor_step_script()
    findings = "echo '2 ok, 1 failure(s)'; exit 1"
    clean = "echo 'all ok'; exit 0"
    crash = "echo 'Traceback (most recent call last):' >&2; echo 'RuntimeError: x' >&2; exit 1"
    odd_exit = "echo 'usage'; exit 2"
    assert _run_doctor_step(tmp_path / "a", findings) == 0
    assert _run_doctor_step(tmp_path / "b", clean) == 0
    assert _run_doctor_step(tmp_path / "c", crash) != 0
    assert _run_doctor_step(tmp_path / "d", odd_exit) != 0


def test_the_real_doctor_survives_the_step_on_this_tree(tmp_path):
    """The step against the real script: whatever this checkout is missing,
    the answer is findings, never a traceback."""
    script = _doctor_step_script().replace("/tmp/doctor.out", str(tmp_path / "doctor.out"))
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
