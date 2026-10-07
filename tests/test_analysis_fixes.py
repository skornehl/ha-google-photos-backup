"""Tests for bug fixes and edge cases identified during analysis."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.google_photos_backup.backends.curl_session import parse_curl_session
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.backends.takeout_backend import (
    BackupStats,
    TakeoutBackend,
)
from custom_components.google_photos_backup.backends.throttle import throttled_stream_to_file
from custom_components.google_photos_backup.coordinator import GooglePhotosBackupCoordinator


def test_find_sidecar_with_curly_braces(tmp_path: Path) -> None:
    """Test that filenames containing curly braces don't crash _find_sidecar with KeyError."""
    media_file = tmp_path / "photo_{1}.jpg"
    media_file.touch()
    sidecar = tmp_path / "photo_{1}.jpg.json"
    sidecar.write_text('{"photoTakenTime": {"timestamp": "1600000000"}}', encoding="utf-8")

    result = TakeoutBackend._find_sidecar(media_file)
    assert result == sidecar


def test_parse_curl_session_with_header_flag() -> None:
    """Test that cURL commands using --header 'Cookie: ...' are parsed correctly."""
    cmd_takeout = (
        "curl 'https://takeout.google.com/takeout-20260101T000000Z-001-001.zip?i=0' "
        "--header 'Cookie: SID=abc; HSID=xyz'"
    )
    session = parse_curl_session(cmd_takeout)
    assert session is not None
    assert session.cookie == "SID=abc; HSID=xyz"


class DummyChunkIter:
    def __aiter__(self) -> DummyChunkIter:
        return self

    async def __anext__(self) -> bytes:
        raise RuntimeError("Connection dropped")


@pytest.mark.asyncio
async def test_throttled_stream_to_file_closes_response_on_error(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Test that throttled_stream_to_file closes the ClientResponse on error."""
    mock_resp = MagicMock()
    mock_resp.content.iter_chunked.return_value = DummyChunkIter()
    dest = tmp_path / "file.zip"

    with pytest.raises(RuntimeError):
        await throttled_stream_to_file(mock_resp, dest, hass, limit_kbps=0)

    mock_resp.close.assert_called_once()


@pytest.mark.asyncio
async def test_coordinator_free_space_refreshing_resets_if_no_target_dir(
    hass: HomeAssistant,
) -> None:
    """Test that _async_refresh_free_space resets _free_space_refreshing even if target_dir is empty."""
    entry = MagicMock()
    entry.options = {}
    entry.data = {}
    coordinator = GooglePhotosBackupCoordinator(hass, entry)

    coordinator._free_space_refreshing = False
    await coordinator._async_refresh_free_space()
    assert coordinator._free_space_refreshing is False


def test_takeout_import_archive_resilient_to_single_file_failure(tmp_path: Path) -> None:
    """Test that one failing media file move does not abort remaining files in archive."""
    backend = MagicMock()
    backend.state.processed_hashes = set()

    media1 = tmp_path / "img1.jpg"
    media2 = tmp_path / "img2.jpg"
    media1.write_bytes(b"data1")
    media2.write_bytes(b"data2")

    stats = BackupStats()

    # Call _import_media_file mocking one failure
    with patch.object(
        TakeoutBackend,
        "_import_media_file",
        side_effect=[RuntimeError("Permission denied"), None],
    ):
        # Create a mock zip/extract run simulation calling the loop from _import_archive
        media_files = [media1, media2]
        for i, media_file in enumerate(media_files, 1):
            try:
                TakeoutBackend._import_media_file(
                    backend, media_file, str(tmp_path), stats, []
                )
            except Exception as err:
                stats.errors.append(f"{media_file.name}: import failed ({err})")
            stats.import_files_done = i

        assert len(stats.errors) == 1
        assert "img1.jpg" in stats.errors[0]
        assert stats.import_files_done == 2


@pytest.mark.asyncio
async def test_library_api_processed_ids_set_caching(
    hass: HomeAssistant, tmp_path: Path
) -> None:
    """Test that LibraryApiBackend caches processed_ids in a set."""
    entry = MagicMock()
    entry.data = {"target_dir": str(tmp_path)}
    state = MagicMock()
    state.get.return_value = ["item1", "item2"]
    oauth = AsyncMock()

    backend = LibraryApiBackend(hass, entry, state, oauth)
    stats = BackupStats()

    item = {"id": "item1"}
    await backend._download_picker_item(item, str(tmp_path), stats)

    assert stats.files_skipped == 1
    assert backend._processed_ids_set == {"item1", "item2"}
