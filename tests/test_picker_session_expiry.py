"""Regression tests for client-side picker-session expiry (issue #13).

CONF_PICKER_SESSION_EXPIRES was persisted but never read - a stale
session only ever got cleaned up once Google itself returned 404. Now
checked client-side first, so a known-expired session is discarded
without an API round-trip, and independently of whether Google's 404
behavior for expired sessions ever changes.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.const import (
    CONF_PICKER_SESSION_EXPIRES,
    CONF_PICKER_SESSION_ID,
    CONF_TARGET_DIR,
)


def _make_backend(expires_raw) -> LibraryApiBackend:
    entry = SimpleNamespace(data={CONF_TARGET_DIR: "/media/x"}, options={})
    state = SyncStateStore(
        {CONF_PICKER_SESSION_ID: "sess123", CONF_PICKER_SESSION_EXPIRES: expires_raw}
    )
    oauth = MagicMock()
    oauth.async_request = AsyncMock()
    return LibraryApiBackend(MagicMock(), entry, state, oauth_session=oauth)


def test_is_picker_session_expired_true_for_past_timestamp():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    backend = _make_backend(past)
    assert backend._is_picker_session_expired() is True


def test_is_picker_session_expired_false_for_future_timestamp():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    backend = _make_backend(future)
    assert backend._is_picker_session_expired() is False


def test_is_picker_session_expired_false_when_missing():
    backend = _make_backend(None)
    assert backend._is_picker_session_expired() is False


def test_is_picker_session_expired_false_when_unparseable():
    backend = _make_backend("not-a-date")
    assert backend._is_picker_session_expired() is False


async def test_expired_session_is_cleared_without_an_api_call():
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    backend = _make_backend(past)
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)

    backend._oauth.async_request.assert_not_called()
    assert backend.state.get(CONF_PICKER_SESSION_ID) is None


async def test_non_expired_session_still_calls_the_api():
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z")
    backend = _make_backend(future)
    # Session status check returns 404 -> the existing (unrelated to this
    # fix) cleanup path - just confirms we actually reached the API call.
    resp = MagicMock(status=404)
    backend._oauth.async_request.return_value = resp
    stats = BackupStats()

    await backend._finish_pending_picker_session(stats)

    backend._oauth.async_request.assert_called_once()
