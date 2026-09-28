"""The service watcher polls pm2. The bundle has none, and the first Windows
install's agent log printed a traceback a minute for it (2026-09-28). Where
there is no pm2 the watcher says so once and stands down."""
import asyncio
import logging
import shutil

from agent import notify


def test_without_pm2_the_watcher_stands_down_after_one_line(monkeypatch, caplog):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    spawned = []

    async def no_spawn(*a, **k):
        spawned.append(a)
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", no_spawn)
    with caplog.at_level(logging.INFO, logger=notify.logger.name):
        asyncio.run(asyncio.wait_for(notify.watch_services(None, interval=0.01), timeout=1))
    assert spawned == []
    assert "no pm2 on this deployment" in caplog.text and "Traceback" not in caplog.text
