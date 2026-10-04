"""Regression tests for the reauth flow and auth-failure propagation.

Reauth used to call async_update_entry(data=self._data), where _data only
held what the reauth flow itself had collected (backend, drive-sync flag,
token). That *replaced* entry.data and dropped target_dir, watch_dir and
every other setup field - the reload right after then failed with a
KeyError, so a routine token refresh (every 7 days for an OAuth client in
"Testing" status) left the entry permanently broken.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.config_flow import GooglePhotosBackupFlowHandler
from custom_components.google_photos_backup.const import (
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    CONF_TAKEOUT_DRIVE_SYNC,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DOMAIN,
)

_OLD_TOKEN = {"access_token": "old", "refresh_token": "old-refresh"}
_NEW_TOKEN = {"access_token": "new", "refresh_token": "new-refresh"}


async def test_reauth_merges_new_token_into_existing_entry_data(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_BACKEND: BACKEND_TAKEOUT,
            CONF_TAKEOUT_DRIVE_SYNC: True,
            CONF_TARGET_DIR: "/media/google_photos",
            CONF_TAKEOUT_WATCH_DIR: "/media/google_takeout_incoming",
            "auth_implementation": "google_photos_backup",
            "token": _OLD_TOKEN,
        },
    )
    entry.add_to_hass(hass)

    flow = GooglePhotosBackupFlowHandler()
    flow.hass = hass
    flow.handler = DOMAIN
    flow.flow_id = "reauth-test"
    flow.context = {"source": config_entries.SOURCE_REAUTH, "entry_id": entry.entry_id}
    await flow.async_step_reauth(dict(entry.data))

    with patch.object(hass.config_entries, "async_reload", AsyncMock()):
        result = await flow.async_oauth_create_entry(
            {"auth_implementation": "google_photos_backup", "token": _NEW_TOKEN}
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["token"] == _NEW_TOKEN
    assert entry.data[CONF_TARGET_DIR] == "/media/google_photos"
    assert entry.data[CONF_TAKEOUT_WATCH_DIR] == "/media/google_takeout_incoming"
    assert entry.data[CONF_TAKEOUT_DRIVE_SYNC] is True


async def test_library_api_run_lets_token_refresh_failure_escape():
    """The picker/library calls catch every exception into stats.errors -
    a revoked grant must still reach the coordinator so it can raise
    ConfigEntryAuthFailed instead of logging "sync failed" forever."""
    oauth = MagicMock()
    oauth.async_ensure_token_valid = AsyncMock(side_effect=RuntimeError("invalid_grant"))
    oauth.async_request = AsyncMock()
    entry = SimpleNamespace(data={CONF_TARGET_DIR: "/media/google_photos"}, options={})
    backend = LibraryApiBackend(MagicMock(), entry, SyncStateStore({}), oauth_session=oauth)

    with pytest.raises(RuntimeError, match="invalid_grant"):
        await backend.async_run_backup()
    oauth.async_request.assert_not_called()
