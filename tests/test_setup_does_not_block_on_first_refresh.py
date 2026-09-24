"""Regression test: setting up the config entry must not wait for the
first backup run to complete.

Confirmed in practice (2026-09-24): a large first import can take hours,
and Home Assistant's own bootstrap has a hard timeout on how long it
waits for an integration's async_setup_entry to return. Blocking on
coordinator.async_config_entry_first_refresh() (the usual coordinator
pattern) got the whole config entry cancelled and dumped into
setup_error mid-restart, even though the backup itself was still making
progress and didn't need to be interrupted.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.const import (
    BACKEND_RCLONE,
    CONF_BACKEND,
    CONF_RCLONE_REMOTE_NAME,
    CONF_SYNC_INTERVAL_MINUTES,
    CONF_TARGET_DIR,
    DOMAIN,
)


def _entry(hass, tmp_path) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BACKEND: BACKEND_RCLONE,
            CONF_TARGET_DIR: str(tmp_path),
            CONF_SYNC_INTERVAL_MINUTES: 60,
            CONF_RCLONE_REMOTE_NAME: "gphotos",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_completes_while_first_backup_run_is_still_in_flight(hass, tmp_path):
    backup_may_finish = asyncio.Event()
    backup_started = asyncio.Event()

    async def _slow_backup():
        backup_started.set()
        await backup_may_finish.wait()
        return MagicMock(files_downloaded=1, files_skipped=0, errors=[])

    backend = MagicMock()
    backend.async_validate = AsyncMock()
    backend.async_terminate = AsyncMock()
    backend.async_run_backup = _slow_backup

    entry = _entry(hass, tmp_path)
    with patch(
        "custom_components.google_photos_backup.coordinator.async_create_backend",
        AsyncMock(return_value=backend),
    ):
        # The point of the fix: this must resolve without ever waiting on
        # backup_may_finish, which we deliberately never set before this
        # call returns.
        setup_ok = await asyncio.wait_for(
            hass.config_entries.async_setup(entry.entry_id), timeout=5
        )

    assert setup_ok is True
    assert entry.state is ConfigEntryState.LOADED

    # The background refresh should have been kicked off, though - it's
    # just not blocking setup, not skipped entirely.
    await asyncio.wait_for(backup_started.wait(), timeout=5)

    # Cleanup: let the still-running backup finish before the test ends,
    # so it isn't torn down mid-await.
    backup_may_finish.set()
    await hass.async_block_till_done()
