"""Regression tests for reauth-on-auth-failure (issue #3).

Only a genuine auth-failure status code (400/401/403) from the backend
should trigger ConfigEntryAuthFailed (which HA turns into the reauth
flow) - anything else (a transient 5xx, a plain network error) must keep
surfacing as a normal UpdateFailed, so a temporary Google outage doesn't
send the user through a pointless re-login.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientResponseError
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.const import DOMAIN
from custom_components.google_photos_backup.coordinator import GooglePhotosBackupCoordinator


def _client_response_error(status: int) -> ClientResponseError:
    return ClientResponseError(
        request_info=SimpleNamespace(real_url="https://example.invalid"),
        history=(),
        status=status,
        message="boom",
    )


@pytest.fixture
def coordinator(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    return GooglePhotosBackupCoordinator(hass, entry)


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_auth_failure_status_raises_config_entry_auth_failed(coordinator, status):
    coordinator.backend = AsyncMock()
    coordinator.backend.async_run_backup.side_effect = _client_response_error(status)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_non_auth_failure_status_raises_plain_update_failed(coordinator, status):
    coordinator.backend = AsyncMock()
    coordinator.backend.async_run_backup.side_effect = _client_response_error(status)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_generic_exception_still_raises_update_failed(coordinator):
    coordinator.backend = AsyncMock()
    coordinator.backend.async_run_backup.side_effect = RuntimeError("network blip")

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()
