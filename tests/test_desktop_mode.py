"""The desktop app's sign-in rules, and that nothing else gets them.

TEKTONIX_DESKTOP=1 is written by the desktop app into its own .env and by
nothing else. With it: the first account's second factor is optional and a
session lasts ninety days. Without it, which is every host install and the
plain compose bundle: forced 2FA for the admin and seven-day sessions,
exactly as before (2026-09-29)."""
import yaml

from agent import auth, paths
from agent.auth import User
from agent.routers.auth import _user_public

ADMIN_NO_2FA = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=False, must_change_password=False)
ADMIN_FRESH = User(id=1, email="a@b.co", role="admin", allowed_repos=None, totp_enabled=False, must_change_password=True)


def test_a_host_install_keeps_the_full_rules(monkeypatch):
    monkeypatch.delenv("TEKTONIX_DESKTOP", raising=False)
    assert auth.desktop_install() is False
    assert auth.forced_screen_block(ADMIN_NO_2FA) == "2FA setup required before using this"
    assert auth.session_ttl_seconds() == 7 * 24 * 3600
    assert _user_public(ADMIN_NO_2FA)["require_totp_setup"] is True


def test_a_desktop_install_makes_the_second_factor_optional_and_sessions_long(monkeypatch):
    monkeypatch.setenv("TEKTONIX_DESKTOP", "1")
    # The premise the flag rests on, stated rather than inherited from the
    # box's own .env (a reference deployment binds wide and licenses accounts).
    monkeypatch.setenv("BIND_ADDRESS", "127.0.0.1")
    monkeypatch.delenv("TEKTONIX_FEATURES", raising=False)
    assert auth.desktop_install() is True
    assert auth.forced_screen_block(ADMIN_NO_2FA) is None
    assert auth.forced_screen_block(ADMIN_FRESH) == "password change required before using this", "a generated password is still replaced first"
    assert auth.session_ttl_seconds() == 90 * 24 * 3600
    assert _user_public(ADMIN_NO_2FA)["require_totp_setup"] is False


def test_the_desktop_rules_stop_where_their_premise_stops(monkeypatch):
    """The waiver rests on the machine's own login being the boundary. Bound
    to anything but loopback, or with accounts beyond the first licensed,
    it is not, and the full sign-in is back (2026-09-29)."""
    monkeypatch.setenv("TEKTONIX_DESKTOP", "1")
    monkeypatch.delenv("TEKTONIX_FEATURES", raising=False)
    for bind in ("", "127.0.0.1", "localhost", "::1", "[::1]"):
        monkeypatch.setenv("BIND_ADDRESS", bind)
        assert auth.desktop_install() is True, f"BIND_ADDRESS={bind!r} is this machine"
    for bind in ("0.0.0.0", "192.168.1.20", "::"):
        monkeypatch.setenv("BIND_ADDRESS", bind)
        assert auth.desktop_install() is False, f"BIND_ADDRESS={bind!r} reaches the network"
        assert auth.forced_screen_block(ADMIN_NO_2FA) == "2FA setup required before using this"
        assert auth.session_ttl_seconds() == 7 * 24 * 3600
    monkeypatch.setenv("BIND_ADDRESS", "127.0.0.1")
    monkeypatch.setenv("TEKTONIX_FEATURES", "multi-user")
    assert auth.desktop_install() is False, "a second account is not the machine's owner"
    compose = yaml.safe_load((paths.REPO_ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["BIND_ADDRESS"] == "${BIND_ADDRESS:-127.0.0.1}", "the agent sees the bind it was given"


def test_only_the_desktop_app_switches_it_on():
    compose = yaml.safe_load((paths.REPO_ROOT / "docker-compose.yml").read_text())
    assert compose["services"]["agent"]["environment"]["TEKTONIX_DESKTOP"] == "${TEKTONIX_DESKTOP:-}", "empty unless the app's .env sets it"
    for f in ("docker/.env.example", ".env.example", "install.sh", "install.ps1"):
        assert "TEKTONIX_DESKTOP" not in (paths.REPO_ROOT / f).read_text(), f"{f} must not set the desktop rules"
    rust = (paths.REPO_ROOT / "app/src-tauri/src/stack.rs").read_text()
    assert 'set_env_line(&content, "TEKTONIX_DESKTOP", "1")' in rust, "the app writes it into its own .env"
