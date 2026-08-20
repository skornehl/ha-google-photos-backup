"""Backend abstraction: pick the right BackupBackend for a config entry."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from ..const import (
    BACKEND_LIBRARY_API,
    BACKEND_RCLONE,
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    CONF_TAKEOUT_DRIVE_SYNC,
)
from .base import BackupBackend, BackupStats, SyncStateStore
from .library_api import LibraryApiBackend
from .rclone_backend import RcloneBackend
from .takeout_backend import TakeoutBackend

__all__ = [
    "BackupBackend",
    "BackupStats",
    "SyncStateStore",
    "async_create_backend",
    "scopes_for_backend",
]

BACKEND_CLASSES: dict[str, type[BackupBackend]] = {
    BACKEND_LIBRARY_API: LibraryApiBackend,
    BACKEND_RCLONE: RcloneBackend,
    BACKEND_TAKEOUT: TakeoutBackend,
}


def scopes_for_backend(backend_type: str | None) -> list[str] | None:
    """OAuth2 scopes the given backend needs, or None if it needs none
    (or the backend type isn't known yet - the config flow asks before
    CONF_BACKEND is necessarily set).

    Lives here rather than as an if/elif chain in config_flow so adding
    an OAuth-using backend means declaring `oauth_scopes` on that one
    class and nothing else - see BackupBackend.oauth_scopes.
    """
    backend_class = BACKEND_CLASSES.get(backend_type or "")
    if backend_class is None:
        return None
    return backend_class.oauth_scopes


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
        oauth_session = None
        if entry.data.get(CONF_TAKEOUT_DRIVE_SYNC):
            implementation = await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
            oauth_session = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)
        return TakeoutBackend(hass, entry, state, oauth_session=oauth_session)

    raise ValueError(f"Unbekanntes Backend: {backend_type}")
