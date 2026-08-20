"""Tests for the config-entry lifecycle (issue #51).

The suite covered backends, config flow and helpers, but never actually
ran async_setup_entry/async_unload_entry. That left the #6 work
untested from the outside: async_terminate() had its own unit test, but
nothing verified that unloading an entry *calls* it - which is the part
that keeps a running rclone subprocess from outliving HA's reload.
"""
from __future__ import annotations

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
    SERVICE_BACKUP_NOW,
    SERVICE_START_PICKER_SESSION,
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


def _backend() -> MagicMock:
    backend = MagicMock()
    backend.async_validate = AsyncMock()
    backend.async_terminate = AsyncMock()
    backend.async_run_backup = AsyncMock(
        return_value=MagicMock(files_downloaded=0, files_skipped=0, errors=[])
    )
    return backend


async def test_setup_registers_coordinator_and_services(hass, tmp_path):
    entry = _entry(hass, tmp_path)
    with patch(
        "custom_components.google_photos_backup.coordinator.async_create_backend",
        AsyncMock(return_value=_backend()),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.entry_id in hass.data[DOMAIN]
    assert hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)
    assert hass.services.has_service(DOMAIN, SERVICE_START_PICKER_SESSION)


async def test_unload_terminates_the_backend(hass, tmp_path):
    """The regression this file exists for: #6 made unload responsible
    for killing an in-flight rclone subprocess."""
    entry = _entry(hass, tmp_path)
    backend = _backend()
    with patch(
        "custom_components.google_photos_backup.coordinator.async_create_backend",
        AsyncMock(return_value=backend),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    backend.async_terminate.assert_awaited_once()
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data.get(DOMAIN, {})


async def test_services_survive_until_the_last_entry_is_unloaded(hass, tmp_path):
    """Services are global, not per-entry - removing one of two entries
    must not pull them out from under the other."""
    first = _entry(hass, tmp_path)
    second = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BACKEND: BACKEND_RCLONE,
            CONF_TARGET_DIR: str(tmp_path / "second"),
            CONF_SYNC_INTERVAL_MINUTES: 60,
            CONF_RCLONE_REMOTE_NAME: "gphotos2",
        },
    )
    second.add_to_hass(hass)

    with patch(
        "custom_components.google_photos_backup.coordinator.async_create_backend",
        AsyncMock(side_effect=lambda *a, **k: _backend()),
    ):
        assert await hass.config_entries.async_setup(first.entry_id)
        await hass.async_block_till_done()
        # Setting up the domain can already pull in the second entry, in
        # which case setting it up again raises OperationNotAllowed - so
        # only do it if it isn't loaded yet.
        if second.state is not ConfigEntryState.LOADED:
            assert await hass.config_entries.async_setup(second.entry_id)
        await hass.async_block_till_done()
        assert first.state is ConfigEntryState.LOADED
        assert second.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_unload(first.entry_id)
        await hass.async_block_till_done()
        assert hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW), (
            "Services dürfen nicht verschwinden, solange noch ein Entry geladen ist"
        )

        assert await hass.config_entries.async_unload(second.entry_id)
        await hass.async_block_till_done()

    assert not hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW)


async def test_validation_failure_becomes_config_entry_not_ready(hass, tmp_path):
    """A bad path / missing rclone binary is a configuration problem, and
    must surface as ConfigEntryNotReady (retry) rather than crash setup."""
    entry = _entry(hass, tmp_path)
    backend = _backend()
    backend.async_validate = AsyncMock(side_effect=ValueError("Zielverzeichnis fehlt"))

    with patch(
        "custom_components.google_photos_backup.coordinator.async_create_backend",
        AsyncMock(return_value=backend),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_RETRY
