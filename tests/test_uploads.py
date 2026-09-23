"""Operator attachments: pictures/PDFs/CSVs uploaded from the
dashboard land in the sandbox's .uploads/, PDFs get sibling extracted text,
git never sees any of it, and the goal note tells the agent how to consume
each kind."""

import subprocess

from fastapi.testclient import TestClient

import agent.server as srv
from agent.auth import User

_FAKE_ADMIN = User(id=1, email="test@example.com", role="admin", allowed_repos=None, totp_enabled=True, must_change_password=False)


def _client(monkeypatch, tmp_path):
    repo = tmp_path / "sandbox"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.setitem(srv.PROJECTS, "test-repo", {"sandbox": str(repo), "live": str(repo)})
    # /api/uploads requires a logged-in user (agent/auth.py) -- these tests
    # exercise upload handling itself, not auth, so bypass it the standard
    # FastAPI way rather than standing up a real Postgres-backed auth pool.
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: _FAKE_ADMIN)
    return TestClient(srv.app), repo


def test_csv_upload_lands_in_workspace_and_git_ignores_it(monkeypatch, tmp_path):
    client, repo = _client(monkeypatch, tmp_path)
    r = client.post("/api/uploads?repo=test-repo",
                    files=[("files", ("data.csv", b"sku,price\nA,9.99\n", "text/csv"))])
    assert r.status_code == 200
    entry = r.json()["files"][0]
    assert entry["kind"] == "text"
    assert (repo / entry["path"]).read_bytes() == b"sku,price\nA,9.99\n"
    status = subprocess.run(["git", "-C", str(repo), "status", "--short"],
                            capture_output=True, text=True).stdout
    assert ".uploads" not in status, "uploads must be invisible to git (core.excludesFile)"


def test_pdf_upload_extracts_sibling_text(monkeypatch, tmp_path):
    import pypdf
    from pypdf import PdfWriter
    client, repo = _client(monkeypatch, tmp_path)
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    pdf_path = tmp_path / "doc.pdf"
    with open(pdf_path, "wb") as f:
        w.write(f)
    r = client.post("/api/uploads?repo=test-repo",
                    files=[("files", ("doc.pdf", pdf_path.read_bytes(), "application/pdf"))])
    assert r.status_code == 200
    entry = r.json()["files"][0]
    assert entry["kind"] == "pdf"
    assert entry["extracted_text"] and (repo / entry["extracted_text"]).exists()
    assert entry["pages"] == 1


def test_unsupported_type_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    r = client.post("/api/uploads?repo=test-repo",
                    files=[("files", ("evil.exe", b"MZ", "application/octet-stream"))])
    assert r.status_code == 415


def test_attachments_note_covers_all_kinds():
    note = srv._attachments_note([
        {"path": ".uploads/x/shot.png", "kind": "image", "bytes": 10},
        {"path": ".uploads/x/spec.pdf", "kind": "pdf", "bytes": 10, "extracted_text": ".uploads/x/spec.pdf.txt", "pages": 3},
        {"path": ".uploads/x/data.csv", "kind": "text", "bytes": 10},
    ])
    assert "describe_image" in note
    assert "spec.pdf.txt" in note
    assert "read tool" in note
    assert "never commit" in note


def test_a_traversal_filename_lands_inside_its_batch(monkeypatch, tmp_path):
    """`Path(name).name` is what keeps a client-chosen filename from walking
    out of .uploads/<batch>/. Pinned (2026-09-23 review, finding 4.8)."""
    client, repo = _client(monkeypatch, tmp_path)
    r = client.post("/api/uploads?repo=test-repo",
                    files=[("files", ("../../../etc/passwd.csv", b"a,b\n", "text/csv"))])
    assert r.status_code == 200
    entry = r.json()["files"][0]
    parts = entry["path"].split("/")
    assert parts[0] == ".uploads" and len(parts) == 3 and parts[2] == "passwd.csv"
    written = (repo / entry["path"]).resolve()
    assert written.is_relative_to((repo / ".uploads").resolve())
    assert not (tmp_path / "etc").exists()


def test_the_batch_folder_is_the_servers_choice(monkeypatch, tmp_path):
    """No batch id comes from the request, so there is none to aim at another
    upload or another repo."""
    client, _ = _client(monkeypatch, tmp_path)
    paths = [client.post("/api/uploads?repo=test-repo",
                         files=[("files", ("a.csv", b"x\n", "text/csv"))]).json()["files"][0]["path"]
             for _ in range(2)]
    batches = {p.split("/")[1] for p in paths}
    assert len(batches) == 2 and all(len(b) == 8 for b in batches)


def test_a_user_without_the_repo_cannot_upload_into_it(monkeypatch, tmp_path):
    client, repo = _client(monkeypatch, tmp_path)
    other = User(id=2, email="u@example.com", role="user", allowed_repos=["other-repo"],
                 totp_enabled=True, must_change_password=False)
    monkeypatch.setitem(srv.app.dependency_overrides, srv.require_full_auth, lambda: other)
    r = client.post("/api/uploads?repo=test-repo",
                    files=[("files", ("a.csv", b"x\n", "text/csv"))])
    assert r.status_code == 403
    assert not (repo / ".uploads").exists() or not any((repo / ".uploads").iterdir())
