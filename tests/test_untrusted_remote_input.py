"""Regression tests for treating remote names and archive headers as
untrusted input.

  - Drive's files.list returned files *shared with* the user too, so a
    stranger could share a "takeout-x.zip" and have it imported.
  - Drive file names may contain "/", and were joined onto watch_dir
    unchecked - "../" or absolute names escaped it.
  - The Drive folder ID was interpolated into the query without escaping.
  - library_api joined the API-supplied filename onto the target dir as-is.
  - The free-space check only looked at the compressed archive size, so a
    zip bomb could fill the target disk.
"""
from __future__ import annotations

import zipfile
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.google_photos_backup.backends import takeout_backend as takeout_module
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.library_api import _safe_filename
from custom_components.google_photos_backup.backends.takeout_backend import (
    TakeoutBackend,
    _drive_query_literal,
    _is_plain_filename,
)
from custom_components.google_photos_backup.const import CONF_TAKEOUT_DRIVE_FOLDER_ID

_Usage = namedtuple("_Usage", ["total", "used", "free"])


@pytest.mark.parametrize(
    "name",
    ["../takeout-1.zip", "/etc/takeout-1.zip", "a/takeout-1.zip", "..\\takeout-1.zip", "..", ""],
)
def test_rejects_names_that_are_not_plain_filenames(name):
    assert _is_plain_filename(name) is False


def test_accepts_a_normal_takeout_name():
    assert _is_plain_filename("takeout-20260923T121036Z-1-001.zip") is True


def test_drive_query_literal_escapes_quotes_and_backslashes():
    assert _drive_query_literal("abc") == "'abc'"
    assert _drive_query_literal("a' or name contains '") == "'a\\' or name contains \\''"
    assert _drive_query_literal("a\\b") == "'a\\\\b'"


class _Resp:
    def __init__(self, payload):
        self.status = 200
        self._payload = payload

    def raise_for_status(self):
        pass

    async def json(self):
        return self._payload


async def test_drive_sync_only_lists_own_files_and_skips_unsafe_names(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    oauth = MagicMock()
    oauth.async_ensure_token_valid = AsyncMock()
    oauth.async_request = AsyncMock(
        return_value=_Resp(
            {"files": [{"id": "f1", "name": "../../escaped/takeout-1-001.zip", "size": "1"}]}
        )
    )
    entry = SimpleNamespace(
        entry_id="e", title="t", data={}, options={CONF_TAKEOUT_DRIVE_FOLDER_ID: "fold'er"}
    )
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    backend = TakeoutBackend(hass, entry, SyncStateStore({}), oauth_session=oauth)
    stats = BackupStats()

    await backend._sync_drive_folder(watch_dir, stats, MagicMock())

    query = oauth.async_request.call_args_list[0].kwargs["params"]["q"]
    assert "'me' in owners" in query
    assert "'fold\\'er' in parents" in query
    # Only the listing call happened - the unsafe file was never downloaded.
    assert oauth.async_request.call_count == 1
    assert any("not a plain file name" in e for e in stats.errors)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("IMG_1.jpg", "IMG_1.jpg"),
        ("../../etc/IMG_1.jpg", "IMG_1.jpg"),
        ("/abs/IMG_1.jpg", "IMG_1.jpg"),
        ("..\\..\\IMG_1.jpg", "IMG_1.jpg"),
        ("..", "fallback.jpg"),
        ("", "fallback.jpg"),
        (None, "fallback.jpg"),
    ],
)
def test_library_api_filename_is_reduced_to_a_bare_name(raw, expected):
    assert _safe_filename(raw, fallback="fallback.jpg") == expected


def test_zip_bomb_is_rejected_by_uncompressed_size(tmp_path: Path, monkeypatch):
    archive = tmp_path / "takeout-1-001.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Takeout/Google Photos/zeros.bin", b"\0" * (20 * 1024 * 1024))
    assert archive.stat().st_size < 1024 * 1024
    # 5 MiB free: enough for the compressed-size estimate, not for 20 MiB.
    monkeypatch.setattr(
        takeout_module.shutil, "disk_usage", lambda _p: _Usage(10**9, 0, 5 * 1024 * 1024)
    )
    dest = tmp_path / "extract"
    dest.mkdir()
    backend = TakeoutBackend(MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({}))

    TakeoutBackend._check_free_space(archive, dest)  # compressed estimate passes
    with pytest.raises(ValueError, match="Not enough free disk space"):
        backend._extract(archive, dest, BackupStats())
    assert list(dest.iterdir()) == []
