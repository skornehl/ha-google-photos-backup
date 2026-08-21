"""Diagnostics support - redacted state dump for support requests.

Deliberately conservative about what leaves the user's machine. Two
things in this integration's config are outright credentials:

  - `token` (and `auth_implementation`): the OAuth access/refresh token.
  - `takeout_download_links`: Takeout email links carry Google-issued
    auth material in their query string - the same reasoning as issue
    #18, where these were removed from log output.

And the persisted sync state is dumped as *counts*, never as the raw
lists: `processed_hashes` alone holds one SHA-256 per backed-up file, so
a real library would produce a multi-megabyte diagnostics file full of
media identifiers - useless for support and needlessly revealing.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_TAKEOUT_DOWNLOAD_LINKS,
    CONF_TAKEOUT_DRIVE_FOLDER_ID,
    DOMAIN,
)
from .coordinator import GooglePhotosBackupCoordinator

TO_REDACT = {
    "token",
    "auth_implementation",
    CONF_TAKEOUT_DOWNLOAD_LINKS,
    # Not a credential, but it identifies a specific Drive location.
    # Presence/absence is what matters for support, not the value.
    CONF_TAKEOUT_DRIVE_FOLDER_ID,
}


def _state_summary(state_data: dict[str, Any]) -> dict[str, Any]:
    """Counts instead of contents - see module docstring."""
    summary: dict[str, Any] = {}
    for key in (
        "processed_ids",
        "processed_hashes",
        "processed_archives",
        "downloaded_takeout_links",
        "downloaded_drive_file_ids",
    ):
        value = state_data.get(key)
        summary[f"{key}_count"] = len(value) if isinstance(value, (list, dict)) else 0

    summary["files_backed_up_total"] = state_data.get("files_backed_up_total", 0)
    summary["last_sync"] = state_data.get("last_sync")
    # Error strings can contain filenames; they're the single most useful
    # thing in a support request, so keep them - but cap the volume.
    errors = state_data.get("last_errors") or []
    summary["last_errors"] = errors[:10] if isinstance(errors, list) else []
    summary["last_errors_truncated"] = isinstance(errors, list) and len(errors) > 10
    summary["has_pending_picker_session"] = bool(state_data.get("picker_session_id"))
    return summary


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return redacted diagnostics for a config entry."""
    coordinator: GooglePhotosBackupCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )

    diagnostics: dict[str, Any] = {
        "entry": {
            "version": entry.version,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator_loaded": coordinator is not None,
    }

    if coordinator is not None:
        diagnostics["backend_class"] = type(coordinator.backend).__name__
        diagnostics["update_interval_seconds"] = (
            coordinator.update_interval.total_seconds() if coordinator.update_interval else None
        )
        diagnostics["last_update_success"] = coordinator.last_update_success
        diagnostics["sync_state"] = _state_summary(coordinator.state_data)

    return diagnostics
