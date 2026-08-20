"""Regression tests for the picker-service backend guard (issue #15).

The service used to compare entry.data[CONF_BACKEND] to a string and
then call a library_api-only method behind a `# type: ignore`. Now
guarded with isinstance() against the actual backend class - these tests
confirm the guard still rejects non-library_api backends and still lets
library_api through.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.backends.rclone_backend import RcloneBackend
from custom_components.google_photos_backup.const import (
    DOMAIN,
    SERVICE_START_PICKER_SESSION,
)


async def _setup_with_backend(hass, backend) -> MockConfigEntry:
    """Register the services with a coordinator whose .backend is the
    given object, without going through a full config-entry setup."""
    from custom_components.google_photos_backup import _async_register_services

    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)

    coordinator = MagicMock()
    coordinator.entry = entry
    coordinator.backend = backend
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    _async_register_services(hass)
    return entry


async def test_rejects_non_library_api_backend(hass):
    entry = await _setup_with_backend(hass, MagicMock(spec=RcloneBackend))

    with pytest.raises(HomeAssistantError, match="library_api"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_START_PICKER_SESSION,
            {"config_entry_id": entry.entry_id},
            blocking=True,
        )


async def test_accepts_library_api_backend(hass):
    backend = MagicMock(spec=LibraryApiBackend)
    backend.async_start_picker_session = AsyncMock(return_value="https://picker.example/abc")
    entry = await _setup_with_backend(hass, backend)

    with patch(
        "custom_components.google_photos_backup.persistent_notification.async_create"
    ) as notify:
        await hass.services.async_call(
            DOMAIN,
            SERVICE_START_PICKER_SESSION,
            {"config_entry_id": entry.entry_id},
            blocking=True,
        )

    backend.async_start_picker_session.assert_awaited_once()
    assert "https://picker.example/abc" in notify.call_args[0][1]


async def test_rejects_when_backend_is_none(hass):
    """Defensive: a coordinator whose setup failed leaves backend=None -
    must raise the clear HomeAssistantError, not AttributeError."""
    entry = await _setup_with_backend(hass, None)

    with pytest.raises(HomeAssistantError, match="library_api"):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_START_PICKER_SESSION,
            {"config_entry_id": entry.entry_id},
            blocking=True,
        )
