"""Web push: the key that must not move, and the fan-out that must not widen.

Two properties carry this feature, and both fail silently:

  The VAPID keypair is the identity every existing subscription was issued
  against. Regenerating it invalidates all of them at once, and the symptom is
  "notifications just stopped" with nothing in any log -- so the file is
  written once and read back forever after.

  The push fan-out has to use the SAME scoping rule as the Telegram one.
  Alert bodies carry the repo name, a goal excerpt and failure detail, so a
  second transport that forgot `_may_hear_about` would hand a single-repo user
  a live feed of every project -- which is exactly the audit finding (H1) the
  Telegram path already had to fix once.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent import notify, push


@pytest.fixture
def keys_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "KEYS_DIR", tmp_path)
    monkeypatch.setattr(push, "VAPID_FILE", tmp_path / "vapid.json")
    monkeypatch.setattr(push, "_cache", None)
    return tmp_path


def test_the_keypair_is_generated_once_and_then_reused(keys_dir, monkeypatch):
    first = push.public_key()
    # A new process: drop the in-memory copy and read the file back.
    monkeypatch.setattr(push, "_cache", None)
    assert push.public_key() == first, "the key moved, invalidating every subscription"


def test_the_private_half_is_not_world_readable(keys_dir):
    push.public_key()
    mode = (keys_dir / "vapid.json").stat().st_mode & 0o777
    assert mode == 0o600, f"vapid.json is {oct(mode)}; it can sign pushes to every subscriber"


def test_the_public_key_is_base64url_with_no_padding(keys_dir):
    """What the browser passes to applicationServerKey. Standard base64 or a
    trailing '=' makes PushManager.subscribe throw InvalidCharacterError."""
    key = push.public_key()
    assert "=" not in key and "+" not in key and "/" not in key
    # An uncompressed P-256 point is 65 bytes -> 87 base64url chars.
    assert len(key) == 87


def test_configured_is_false_before_a_key_exists_and_true_after(keys_dir):
    assert push.configured() is False
    push.public_key()
    assert push.configured() is True


def test_a_payload_is_small_and_carries_only_what_the_worker_reads(keys_dir):
    data = json.loads(push._payload("Task escalated", "x" * 5000, url="/", tag="3d-bot"))
    assert set(data) == {"title", "body", "url", "tag"}
    assert len(data["body"]) <= push._MAX_BODY, "push services reject oversized payloads"
    assert data["tag"] == "3d-bot"


class _Pool:
    """Stands in for the auth pool; only the calls notify makes are real."""


@pytest.fixture
def fanout(monkeypatch, keys_dir):
    """A push fan-out with three subscribers of different scope."""
    push.public_key()
    targets = [
        {"endpoint": "https://push/admin", "p256dh": "a", "auth": "b",
         "role": "admin", "allowed_repos": None},
        {"endpoint": "https://push/scoped", "p256dh": "a", "auth": "b",
         "role": "user", "allowed_repos": ["3d-bot"]},
        {"endpoint": "https://push/other", "p256dh": "a", "auth": "b",
         "role": "user", "allowed_repos": ["3DSteals"]},
    ]
    sent: list[tuple[str, str]] = []
    deleted: list[str] = []

    async def get_push_targets(pool, user_id=None):
        return list(targets)

    async def mark_push_ok(pool, endpoint):
        return None

    async def delete_push_subscription(pool, endpoint):
        deleted.append(endpoint)

    # `from agent import auth` reads the attribute off the already-imported
    # package, so swapping sys.modules would not be seen. Patch the functions.
    monkeypatch.setattr("agent.auth.get_push_targets", get_push_targets)
    monkeypatch.setattr("agent.auth.mark_push_ok", mark_push_ok)
    monkeypatch.setattr("agent.auth.delete_push_subscription", delete_push_subscription)

    async def send_one(sub, title, body, url="/", tag=None):
        sent.append((sub["endpoint"], title))
        return True, 200

    monkeypatch.setattr(push, "send_one", send_one)
    return SimpleNamespace(sent=sent, deleted=deleted, targets=targets)


@pytest.mark.asyncio
async def test_a_project_alert_reaches_only_accounts_that_may_see_that_project(fanout):
    n = await notify._push_operators(_Pool(), "Task DONE\n3d-bot: ship it", repo="3d-bot")
    reached = {e for e, _ in fanout.sent}
    assert reached == {"https://push/admin", "https://push/scoped"}
    assert "https://push/other" not in reached, "a user was told about another project"
    assert n == 2


@pytest.mark.asyncio
async def test_an_infrastructure_alert_goes_to_admins_only(fanout):
    """repo=None names the box, not anyone's code -- same rule the Telegram
    half applies."""
    await notify._push_operators(_Pool(), "agent backend restarted", repo=None)
    assert {e for e, _ in fanout.sent} == {"https://push/admin"}


@pytest.mark.asyncio
async def test_the_first_line_becomes_the_title(fanout):
    await notify._push_operators(_Pool(), "Task ESCALATED\nneeds you", repo=None)
    assert fanout.sent[0][1] == "Task ESCALATED"


@pytest.mark.asyncio
async def test_a_gone_subscription_is_deleted_rather_than_retried(monkeypatch, fanout):
    """404/410 is the push service saying the browser uninstalled the app or
    cleared its data. Those never recover, so a row that stays costs a failed
    send on every alert forever."""
    async def gone(sub, title, body, url="/", tag=None):
        return False, 410

    monkeypatch.setattr(push, "send_one", gone)
    sent = await notify._push_operators(_Pool(), "Task DONE\nx", repo=None)
    assert sent == 0
    assert fanout.deleted == ["https://push/admin"]


@pytest.mark.asyncio
async def test_no_keypair_means_no_sends_rather_than_an_error(monkeypatch, keys_dir):
    """An install that has never had push configured must not pay for it on
    every alert."""
    monkeypatch.setattr(push, "configured", lambda: False)
    assert await notify._push_operators(_Pool(), "anything", repo=None) == 0


@pytest.mark.asyncio
async def test_a_push_failure_can_never_cost_a_telegram_alert(monkeypatch):
    """Push is additive. notify_operators fans out to both, and the older,
    load-bearing transport must not be able to lose a message because the new
    one threw."""
    async def boom(pool, text, repo):
        raise RuntimeError("push exploded")

    async def one_telegram_target(pool):
        return [("tok", "chat", "admin", None)]

    async def send_ok(token, chat_id, text):
        return True

    monkeypatch.setattr(notify, "_push_operators", boom)
    monkeypatch.setattr(notify, "send_telegram", send_ok)
    monkeypatch.setattr("agent.auth.get_telegram_targets", one_telegram_target)

    sent = await notify.notify_operators(_Pool(), "Task DONE\nx", repo=None)
    assert sent == 1, "the Telegram alert was lost when push failed"
