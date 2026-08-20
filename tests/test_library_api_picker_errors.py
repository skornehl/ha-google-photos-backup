"""Regression tests for picker-session error handling (issue #7).

_finish_pending_picker_session() previously let a transient error during
the session-status check, the mediaItems pagination, or the session
DELETE propagate uncaught, aborting the whole coordinator update - unlike
_sync_app_created_items() right next to it, which already degraded
gracefully. These tests drive _finish_pending_picker_session() directly
against a small scripted fake OAuth session, so they don't need a real
`hass` fixture (no code path exercised here touches self.hass).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.const import CONF_PICKER_SESSION_ID, CONF_TARGET_DIR


class _FakeResponse:
    def __init__(self, status: int = 200, json_data: dict | None = None):
        self.status = status
        self._json_data = json_data or {}

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._json_data


class _ScriptedOAuth:
    """Returns/raises the next entry in `script` on each async_request()
    call, in order - enough control for these tests without needing a
    real aiohttp/OAuth2Session."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, str]] = []

    async def async_request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


def _make_backend(oauth: _ScriptedOAuth, session_id: str = "sess123") -> LibraryApiBackend:
    entry = SimpleNamespace(data={CONF_TARGET_DIR: "/media/google_photos"}, options={})
    state = SyncStateStore({CONF_PICKER_SESSION_ID: session_id})
    return LibraryApiBackend(MagicMock(), entry, state, oauth_session=oauth)


async def test_listing_failure_is_recorded_not_raised():
    oauth = _ScriptedOAuth(
        [
            _FakeResponse(200, {"mediaItemsSet": True}),  # session status
            RuntimeError("network blip"),  # mediaItems listing fails
        ]
    )
    backend = _make_backend(oauth)
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)  # must not raise

    assert any("fehlgeschlagen" in err for err in stats.errors)
    # Session wasn't cleared - the caller should get another chance next run.
    assert backend.state.get(CONF_PICKER_SESSION_ID) == "sess123"


async def test_session_status_failure_is_recorded_not_raised():
    oauth = _ScriptedOAuth([RuntimeError("timeout")])
    backend = _make_backend(oauth)
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)

    assert any("fehlgeschlagen" in err for err in stats.errors)


async def test_session_delete_failure_is_recorded_not_raised():
    oauth = _ScriptedOAuth(
        [
            _FakeResponse(200, {"mediaItemsSet": True}),  # session status
            _FakeResponse(200, {"mediaItems": [], "nextPageToken": None}),  # empty page
            RuntimeError("delete failed"),  # DELETE session
        ]
    )
    backend = _make_backend(oauth)
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)

    assert any("konnte nicht aufgeräumt werden" in err for err in stats.errors)
    # Cleanup failed, so the session must NOT have been cleared - the next
    # run needs to see it again (harmless: Google either 404s it by then,
    # or every item gets skipped via processed_ids).
    assert backend.state.get(CONF_PICKER_SESSION_ID) == "sess123"


async def test_expired_session_is_cleared_without_an_error():
    """Pre-existing behavior (not part of the fix) - make sure wrapping
    the block in try/except didn't change this."""
    oauth = _ScriptedOAuth([_FakeResponse(404)])
    backend = _make_backend(oauth)
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)

    assert stats.errors == []
    assert backend.state.get(CONF_PICKER_SESSION_ID) is None
