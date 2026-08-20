"""Backend abstraction: pick the right BackupBackend for a config entry."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from ..const import BACKEND_LIBRARY_API, BACKEND_RCLONE, BACKEND_TAKEOUT, CONF_BACKEND
from .base import BackupBackend, BackupStats, SyncStateStore
from .library_api import LibraryApiBackend
from .rclone_backend import RcloneBackend
from .takeout_backend import TakeoutBackend

__all__ = ["BackupBackend", "BackupStats", "SyncStateStore", "async_create_backend"]


async def async_create_backend(
    hass: HomeAssistant, entry: ConfigEntry, state: SyncStateStore
) -> BackupBackend:
    backend_type = entry.data[CONF_BACKEND]

    if backend_type == BACKEND_LIBRARY_API:
        implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
        oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        return LibraryApiBackend(hass, entry, state, oauth_session)

    if backend_type == BACKEND_RCLONE:
        return RcloneBackend(hass, entry, state)

    if backend_type == BACKEND_TAKEOUT:
        return TakeoutBackend(hass, entry, state)

    raise ValueError(f"Unbekanntes Backend: {backend_type}")
