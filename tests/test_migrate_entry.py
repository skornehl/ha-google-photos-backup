"""Tests for the config-entry migration hook (issue #47)."""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup import async_migrate_entry
from custom_components.google_photos_backup.const import CONFIG_ENTRY_VERSION, DOMAIN


async def test_current_version_migrates_trivially(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=CONFIG_ENTRY_VERSION)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True


async def test_newer_version_is_refused(hass):
    """A downgraded integration must refuse to load an entry written by a
    newer one, rather than write a schema it doesn't understand."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, version=CONFIG_ENTRY_VERSION + 1)
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is False


async def test_config_flow_version_matches_the_constant():
    """Both sides must read from the same constant - if they drift, HA
    silently stops calling the migration hook."""
    from custom_components.google_photos_backup.config_flow import (
        GooglePhotosBackupFlowHandler,
    )

    assert GooglePhotosBackupFlowHandler.VERSION == CONFIG_ENTRY_VERSION
