"""Regression tests for rclone subprocess timeout + termination (issue #6).

Patches asyncio.create_subprocess_exec with a fake process whose
communicate() never returns on its own, and patches the module's
RCLONE_TIMEOUT_SECONDS down to a few milliseconds so the test doesn't
actually wait a (deliberately generous, see const.py) 24h timeout.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends import (
    rclone_backend as rclone_backend_module,
)
from custom_components.google_photos_backup.backends.rclone_backend import RcloneBackend
from custom_components.google_photos_backup.const import (
    CONF_RCLONE_REMOTE_NAME,
    CONF_TARGET_DIR,
    DOMAIN,
)


class _HangingProcess:
    """Fake subprocess whose communicate() never returns on its own."""

    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = False

    async def communicate(self):
        await asyncio.sleep(100)  # much longer than the patched timeout below
        return b"", b""  # pragma: no cover - never actually reached

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self):
        return self.returncode


def _make_backend(hass, monkeypatch, timeout_seconds: float = 0.05) -> RcloneBackend:
    monkeypatch.setattr(rclone_backend_module, "RCLONE_TIMEOUT_SECONDS", timeout_seconds)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_TARGET_DIR: "/tmp", CONF_RCLONE_REMOTE_NAME: "gphotos"},
    )
    entry.add_to_hass(hass)
    return RcloneBackend(hass, entry, MagicMock())


async def test_hanging_process_is_killed_after_timeout(hass, monkeypatch):
    backend = _make_backend(hass, monkeypatch)
    fake_proc = _HangingProcess()

    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)):
        stats = await backend.async_run_backup()

    assert fake_proc.killed
    assert any("killed after" in err for err in stats.errors)
    # Cleared after timeout handling, so a later async_terminate() call
    # (e.g. from unload racing right after) doesn't try to kill it again.
    assert backend._proc is None


async def test_async_terminate_kills_an_in_flight_process(hass, monkeypatch):
    backend = _make_backend(hass, monkeypatch)
    fake_proc = _HangingProcess()
    backend._proc = fake_proc

    await backend.async_terminate()

    assert fake_proc.killed


async def test_async_terminate_is_a_noop_when_nothing_running(hass, monkeypatch):
    backend = _make_backend(hass, monkeypatch)

    await backend.async_terminate()  # must not raise


async def test_async_terminate_does_not_kill_an_already_finished_process(hass, monkeypatch):
    backend = _make_backend(hass, monkeypatch)
    finished_proc = _HangingProcess()
    finished_proc.returncode = 0  # already exited on its own
    backend._proc = finished_proc

    await backend.async_terminate()

    assert not finished_proc.killed
