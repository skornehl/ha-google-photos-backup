"""Regression test for streaming media downloads (issue #45).

_download_picker_item() used to read the whole response into memory via
throttled_read() before writing it. Google Photos serves multi-GB 4K
video through the same code path, and HA commonly runs on 2-4 GB
hardware, so one large item could OOM the entire HA process. It now
streams to disk via throttled_stream_to_file().
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends import library_api as library_api_module
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.const import CONF_TARGET_DIR

_ITEM = {
    "id": "item1",
    "createTime": "2026-08-01T00:00:00Z",
    "mediaFile": {
        "baseUrl": "https://example.com/video",
        "filename": "clip.mp4",
        "mimeType": "video/mp4",
    },
}


def _make_backend(tmp_path: Path) -> LibraryApiBackend:
    entry = SimpleNamespace(data={CONF_TARGET_DIR: str(tmp_path)}, options={})
    oauth = MagicMock()
    oauth.async_request = AsyncMock(return_value=MagicMock())
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return LibraryApiBackend(hass, entry, SyncStateStore({}), oauth_session=oauth)


async def test_download_streams_to_disk_instead_of_buffering(monkeypatch, tmp_path: Path):
    """The point of the fix: the response body must reach the filesystem
    through the streaming helper, never as one in-memory bytes object."""
    seen: dict[str, object] = {}

    async def _fake_stream_to_file(resp, dest, hass, limit_kbps, pacer=None):
        seen["dest"] = dest
        dest.write_bytes(b"payload")
        return 7

    monkeypatch.setattr(library_api_module, "throttled_stream_to_file", _fake_stream_to_file)

    backend = _make_backend(tmp_path)
    stats = BackupStats()
    await backend._download_picker_item(_ITEM, str(tmp_path), stats)

    assert seen, "throttled_stream_to_file was not called"
    assert stats.errors == []
    assert stats.files_downloaded == 1
    assert stats.bytes_downloaded == 7


async def test_the_in_memory_read_helper_is_gone():
    """Guard against a future change quietly reintroducing full
    buffering by importing the old helper again."""
    from custom_components.google_photos_backup.backends import throttle

    assert not hasattr(throttle, "throttled_read")
    assert not hasattr(library_api_module, "throttled_read")


async def test_file_lands_in_the_date_folder_with_capture_mtime(monkeypatch, tmp_path: Path):
    async def _fake_stream_to_file(resp, dest, hass, limit_kbps, pacer=None):
        dest.write_bytes(b"payload")
        return 7

    monkeypatch.setattr(library_api_module, "throttled_stream_to_file", _fake_stream_to_file)

    backend = _make_backend(tmp_path)
    await backend._download_picker_item(_ITEM, str(tmp_path), BackupStats())

    written = tmp_path / "2026" / "2026-08" / "clip.mp4"
    assert written.is_file(), f"not found: {list(tmp_path.rglob('*'))}"
    # createTime 2026-08-01T00:00:00Z -> mtime must follow it, not "now"
    assert written.stat().st_mtime == 1785542400.0


async def test_failed_download_is_recorded_and_not_marked_processed(monkeypatch, tmp_path: Path):
    async def _boom(resp, dest, hass, limit_kbps, pacer=None):
        raise ConnectionError("abgebrochen")

    monkeypatch.setattr(library_api_module, "throttled_stream_to_file", _boom)

    backend = _make_backend(tmp_path)
    stats = BackupStats()
    await backend._download_picker_item(_ITEM, str(tmp_path), stats)

    assert any("fehlgeschlagen" in e for e in stats.errors)
    assert stats.files_downloaded == 0
    # Not recorded -> retried on the next run rather than silently lost.
    assert backend.state.get("processed_ids", []) == []
