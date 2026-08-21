"""Tests for per-page baseUrl refresh (issue #46).

Google Photos baseUrls expire ~60 min after issue. Listing and
downloading were already interleaved per page, so run length alone isn't
the problem - but one page holds 100 items, and a low
bandwidth_limit_kbps can make those outlive their URLs. The page is now
re-requested (same pageToken) once its URLs approach expiry.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends import library_api as library_api_module
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.const import (
    CONF_PICKER_SESSION_ID,
    CONF_TARGET_DIR,
)


def _resp(status=200, json_data=None):
    r = MagicMock(status=status)
    r.raise_for_status = MagicMock()
    r.json = AsyncMock(return_value=json_data or {})
    return r


def _items(*ids):
    return [
        {
            "id": i,
            "createTime": "2026-08-01T00:00:00Z",
            "mediaFile": {
                "baseUrl": f"https://example.com/{i}",
                "filename": f"{i}.jpg",
                "mimeType": "image/jpeg",
            },
        }
        for i in ids
    ]


def _make_backend(tmp_path, responses):
    entry = SimpleNamespace(data={CONF_TARGET_DIR: str(tmp_path)}, options={})
    oauth = MagicMock()
    oauth.async_request = AsyncMock(side_effect=responses)
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    state = SyncStateStore({CONF_PICKER_SESSION_ID: "sess1"})
    return LibraryApiBackend(hass, entry, state, oauth_session=oauth)


async def test_page_is_not_refetched_when_urls_are_fresh(monkeypatch, tmp_path):
    downloaded = []

    async def _fake_download(self, item, target_dir, stats):
        downloaded.append(item["id"])

    monkeypatch.setattr(LibraryApiBackend, "_download_picker_item", _fake_download)

    backend = _make_backend(
        tmp_path,
        [
            _resp(200, {"mediaItemsSet": True}),                       # session status
            _resp(200, {"mediaItems": _items("a", "b"), "nextPageToken": None}),
            _resp(200, {}),                                            # session DELETE
        ],
    )
    await backend._finish_pending_picker_session(BackupStats())

    assert downloaded == ["a", "b"]
    # status + one listing + delete == 3; no extra refetch
    assert backend._oauth.async_request.await_count == 3


async def test_page_is_refetched_when_urls_age_out(monkeypatch, tmp_path):
    """Simulated slow downloads push the page past the age limit, so the
    same page must be requested again to get fresh baseUrls."""
    monkeypatch.setattr(library_api_module, "BASEURL_MAX_AGE_SECONDS", 0)

    downloaded = []

    async def _fake_download(self, item, target_dir, stats):
        downloaded.append(item["mediaFile"]["baseUrl"])

    monkeypatch.setattr(LibraryApiBackend, "_download_picker_item", _fake_download)

    refreshed = [
        {
            "id": i,
            "createTime": "2026-08-01T00:00:00Z",
            "mediaFile": {
                "baseUrl": f"https://example.com/FRESH-{i}",
                "filename": f"{i}.jpg",
                "mimeType": "image/jpeg",
            },
        }
        for i in ("a", "b")
    ]

    backend = _make_backend(
        tmp_path,
        [
            _resp(200, {"mediaItemsSet": True}),
            _resp(200, {"mediaItems": _items("a", "b"), "nextPageToken": None}),
            _resp(200, {"mediaItems": refreshed, "nextPageToken": None}),   # refresh
            _resp(200, {"mediaItems": refreshed, "nextPageToken": None}),   # refresh
            _resp(200, {}),
        ],
    )
    await backend._finish_pending_picker_session(BackupStats())

    # Every download used a refreshed URL, and each item ran exactly once.
    assert downloaded == ["https://example.com/FRESH-a", "https://example.com/FRESH-b"]


async def test_shorter_page_after_refresh_does_not_index_out_of_range(monkeypatch, tmp_path):
    """Defensive: if the refreshed page comes back shorter (selection
    changed mid-run), the loop must stop rather than IndexError."""
    monkeypatch.setattr(library_api_module, "BASEURL_MAX_AGE_SECONDS", 0)

    async def _fake_download(self, item, target_dir, stats):
        return None

    monkeypatch.setattr(LibraryApiBackend, "_download_picker_item", _fake_download)

    backend = _make_backend(
        tmp_path,
        [
            _resp(200, {"mediaItemsSet": True}),
            _resp(200, {"mediaItems": _items("a", "b", "c"), "nextPageToken": None}),
            _resp(200, {"mediaItems": [], "nextPageToken": None}),   # shrunk
            _resp(200, {}),
        ],
    )
    stats = BackupStats()
    await backend._finish_pending_picker_session(stats)   # must not raise

    assert stats.errors == []
