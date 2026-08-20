"""Tests for diagnostics redaction (issue #49).

The whole point of these is negative assertions: a diagnostics export
gets pasted into public issue threads, so the tests check that specific
secret values are *absent* from the serialized output, not merely that
some redaction function was called.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.google_photos_backup.const import (
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    CONF_TAKEOUT_DOWNLOAD_LINKS,
    CONF_TARGET_DIR,
    DOMAIN,
)

_ACCESS_TOKEN = "ya29.SECRET-ACCESS-TOKEN"
_REFRESH_TOKEN = "1//SECRET-REFRESH-TOKEN"
_LINK_SECRET = "SECRET-DOWNLOAD-TOKEN"


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Google Photos Backup (Takeout)",
        data={
            CONF_BACKEND: BACKEND_TAKEOUT,
            CONF_TARGET_DIR: "/media/google_photos",
            CONF_TAKEOUT_DOWNLOAD_LINKS: f"https://takeout.google.com/d?token={_LINK_SECRET}",
            "token": {"access_token": _ACCESS_TOKEN, "refresh_token": _REFRESH_TOKEN},
            "auth_implementation": "google_photos_backup",
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_no_secret_survives_serialization(hass):
    """Serialize the whole result and assert the raw secrets don't appear
    anywhere in it - nesting or future key additions can't sneak one
    through this check."""
    result = await async_get_config_entry_diagnostics(hass, _entry(hass))
    blob = json.dumps(result)

    for secret in (_ACCESS_TOKEN, _REFRESH_TOKEN, _LINK_SECRET):
        assert secret not in blob, f"Secret im Diagnostics-Export: {secret}"


async def test_useful_non_secret_context_is_kept(hass):
    result = await async_get_config_entry_diagnostics(hass, _entry(hass))

    assert result["entry"]["data"][CONF_BACKEND] == BACKEND_TAKEOUT
    assert result["entry"]["data"][CONF_TARGET_DIR] == "/media/google_photos"
    assert result["entry"]["version"] is not None


async def test_sync_state_is_summarized_not_dumped(hass):
    """processed_hashes holds one SHA-256 per backed-up file - dumping it
    would make the export huge and full of media identifiers."""
    entry = _entry(hass)
    coordinator = MagicMock()
    coordinator.backend = MagicMock()
    coordinator.update_interval = None
    coordinator.last_update_success = True
    coordinator.state_data = {
        "processed_hashes": [f"hash{i}" for i in range(500)],
        "processed_ids": ["a", "b"],
        "files_backed_up_total": 502,
        "last_errors": [f"err{i}" for i in range(25)],
        "picker_session_id": "sess1",
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    result = await async_get_config_entry_diagnostics(hass, entry)
    state = result["sync_state"]
    blob = json.dumps(result)

    assert state["processed_hashes_count"] == 500
    assert "hash499" not in blob, "Rohliste statt Kennzahl exportiert"
    assert state["processed_ids_count"] == 2
    assert state["has_pending_picker_session"] is True
    # Errors are the most useful support signal, but capped.
    assert len(state["last_errors"]) == 10
    assert state["last_errors_truncated"] is True


async def test_works_when_entry_is_not_loaded(hass):
    """Diagnostics are often pulled precisely because setup failed."""
    result = await async_get_config_entry_diagnostics(hass, _entry(hass))

    assert result["coordinator_loaded"] is False
    assert "sync_state" not in result
