"""The first admin password: stored encrypted in the data directory, shown
once by scripts/show_initial_password.py, gone afterwards.

2026-09-28, the first Windows install: `docker compose exec` skips the
entrypoint, so the script found neither the DSN nor the signing key in its
environment and crashed with a KeyError. And the file sat at the repo root
inside the container, where one `up --build` before reading it would have
lost the only admin password. The script reads the key from the data
volume now, needs no database, and the file lives in that volume.
"""
import base64
import importlib.util
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent import auth, paths, server


def _load_script():
    spec = importlib.util.spec_from_file_location("show_initial_password", paths.REPO_ROOT / "scripts" / "show_initial_password.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setattr(paths, "DATA_DIR", d)
    monkeypatch.setattr(server, "_INITIAL_PASSWORD_PATH", d / ".initial-admin-password")
    monkeypatch.delenv("AUTH_SECRET_KEY", raising=False)
    return d


def test_the_password_is_stored_in_the_data_directory_and_shown_once_from_the_volume_s_key(data_dir, capsys):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    config = SimpleNamespace(auth_secret_key=key)
    server._store_initial_password.__globals__["config"] = config
    server._store_initial_password("hunter2-once")
    stored = data_dir / ".initial-admin-password"
    assert stored.exists() and "hunter2-once" not in stored.read_text()
    assert stored.stat().st_mode & 0o777 == 0o600

    # The bundle: no AUTH_SECRET_KEY in the exec'd shell, only the file the entrypoint wrote.
    (data_dir / "auth_secret_key").write_text(key + "\n")
    script = _load_script()
    assert script.main() == 0
    out = capsys.readouterr()
    assert out.out.strip() == "hunter2-once" and "removed" in out.err
    assert not stored.exists()
    assert script.main() == 1, "shown once"


def test_a_key_that_differs_from_the_one_the_agent_booted_with_is_said_so(data_dir, monkeypatch, capsys):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    server._store_initial_password.__globals__["config"] = SimpleNamespace(auth_secret_key=key)
    server._store_initial_password("pw")
    monkeypatch.setenv("AUTH_SECRET_KEY", base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
    script = _load_script()
    assert script.main() == 2 and "differs" in capsys.readouterr().err
    assert (data_dir / ".initial-admin-password").exists(), "a wrong key must not destroy the file"


def test_without_any_key_the_script_says_what_to_do(data_dir, capsys):
    (data_dir / ".initial-admin-password").parent.mkdir()
    (data_dir / ".initial-admin-password").write_text("x")
    script = _load_script()
    assert script.main() == 2 and "AUTH_SECRET_KEY is not set" in capsys.readouterr().err


def test_a_file_left_at_the_repo_root_by_an_older_install_is_still_read(data_dir, tmp_path, monkeypatch, capsys):
    key = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    legacy = tmp_path / "root" / ".initial-admin-password"
    legacy.parent.mkdir()
    legacy.write_text(auth._encrypt_totp_secret(SimpleNamespace(auth_secret_key=key), "old-pw"))
    monkeypatch.setenv("AUTH_SECRET_KEY", key)
    script = _load_script()
    monkeypatch.setattr(script, "LEGACY_PATH", legacy)
    assert script.main() == 0 and capsys.readouterr().out.strip() == "old-pw"
    assert not legacy.exists() and isinstance(Path(script.PATH), Path)
