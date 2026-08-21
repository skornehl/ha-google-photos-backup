"""Regression tests for explicit download timeouts (issue #8).

Only covers the two OAuth-based download call sites (library_api item
download, Drive archive download) where mocking is straightforward. The
plain aiohttp `session.get()` call in takeout_backend.py's
_download_links() also passes `timeout=DOWNLOAD_TIMEOUT` (see the diff)
but isn't covered here - mocking an async context manager response adds
enough complexity that it wasn't worth it for a one-line "is this kwarg
present" check; covered by code review instead.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import CONF_TARGET_DIR, DOWNLOAD_TIMEOUT


def test_download_timeout_has_no_total_cap_but_bounds_stalls():
    """total=None is deliberate (see const.py) - a throttled large
    download can legitimately run for hours. sock_read/sock_connect are
    what actually catch a hung connection."""
    assert DOWNLOAD_TIMEOUT.total is None
    assert DOWNLOAD_TIMEOUT.sock_read is not None
    assert DOWNLOAD_TIMEOUT.sock_connect is not None


async def test_library_api_item_download_passes_explicit_timeout(monkeypatch, tmp_path):
    import custom_components.google_photos_backup.backends.library_api as library_api_module

    async def _fake_stream_to_file(resp, dest, hass, limit_kbps, pacer=None):
        dest.write_bytes(b"fake bytes")
        return len(b"fake bytes")

    monkeypatch.setattr(
        library_api_module, "throttled_stream_to_file", _fake_stream_to_file
    )

    entry = SimpleNamespace(data={CONF_TARGET_DIR: str(tmp_path)}, options={})
    oauth = MagicMock()
    oauth.async_request = AsyncMock(return_value=MagicMock())
    # Runs the callable for real, so unique_destination()/os.utime() do
    # what they'd do in production instead of returning a canned value.
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    backend = LibraryApiBackend(hass, entry, SyncStateStore({}), oauth_session=oauth)
    stats = BackupStats()

    await backend._download_picker_item(
        {
            "id": "item1",
            "createTime": "2026-08-01T00:00:00Z",
            "mediaFile": {
                "baseUrl": "https://example.com/photo",
                "filename": "a.jpg",
                "mimeType": "image/jpeg",
            },
        },
        str(tmp_path),
        stats,
    )

    _, kwargs = oauth.async_request.call_args
    assert kwargs.get("timeout") is DOWNLOAD_TIMEOUT
    assert stats.errors == []


async def test_drive_archive_download_passes_explicit_timeout(monkeypatch, tmp_path):
    import custom_components.google_photos_backup.backends.takeout_backend as takeout_module

    async def _fake_throttled_stream_to_file(resp, dest, hass, limit_kbps, pacer=None):
        return 0

    monkeypatch.setattr(
        takeout_module, "throttled_stream_to_file", _fake_throttled_stream_to_file
    )

    oauth = MagicMock()
    # _sync_drive_folder awaits this before doing anything else - a plain
    # MagicMock isn't awaitable, so it has to be an AsyncMock too.
    oauth.async_ensure_token_valid = AsyncMock()
    list_resp = MagicMock()
    list_resp.raise_for_status = MagicMock()
    list_resp.json = AsyncMock(
        return_value={"files": [{"id": "f1", "name": "takeout-1-001.zip", "size": "10"}]}
    )
    download_resp = MagicMock()
    download_resp.raise_for_status = MagicMock()
    oauth.async_request = AsyncMock(side_effect=[list_resp, download_resp])

    entry = SimpleNamespace(data={}, options={})
    # Not a bare MagicMock: _sync_drive_folder offloads its filesystem
    # calls via async_add_executor_job, so this has to actually await and
    # run them. Executing the callable for real (rather than returning a
    # canned value) also keeps this test from caring how many executor
    # round-trips the production code happens to make.
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))

    backend = TakeoutBackend(hass, entry, SyncStateStore({}), oauth_session=oauth)
    stats = BackupStats()

    # Real (non-existent) path so dest.exists() -> False and the code
    # actually reaches the download call we want to inspect.
    await backend._sync_drive_folder(tmp_path, stats)

    assert stats.errors == [], f"unexpected errors: {stats.errors}"

    # First call is the files.list, second is the actual content download.
    _, download_kwargs = oauth.async_request.call_args_list[1]
    assert download_kwargs.get("timeout") is DOWNLOAD_TIMEOUT
