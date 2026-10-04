"""Regression tests for Drive cleanup and corrupt-archive recovery.

  - First run: archives downloaded via Drive sync were never cleaned up
    from Drive, because the consumer looked them up in a dict captured
    before _remember_drive_file had stored anything (state.get() hands out
    a fresh default while the key doesn't exist yet). Later runs never
    revisited them either, since they were already in processed_archives.
  - Permanent deletion always failed with 403: files.delete needs the full
    drive scope, which this integration does not request.
  - A corrupt Drive-synced archive was deleted locally but stayed in
    downloaded_drive_file_ids, so it was never downloaded again.
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import (
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
)

ARCHIVE = "takeout-20260923T000000Z-1-001.zip"


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Takeout/Google Photos/a.jpg", b"content-a")
        zf.writestr(
            "Takeout/Google Photos/a.jpg.json",
            json.dumps({"photoTakenTime": {"timestamp": "1623758400"}}),
        )
    return buf.getvalue()


class _Resp:
    def __init__(self, status: int = 200, payload=None, body: bytes = b""):
        self.status = status
        self._payload = payload or {}

        async def _iter_chunked(_size):
            yield body

        self.content = SimpleNamespace(iter_chunked=_iter_chunked)

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")

    async def json(self):
        return self._payload


class _FakeDrive:
    """Routes the handful of Drive calls _sync_drive_folder/_cleanup_drive_file
    make to canned responses, recording every (method, url) pair."""

    def __init__(self, files: list[dict], body: bytes = b"", delete_status: int = 204):
        self.files = files
        self.body = body
        self.delete_status = delete_status
        self.calls: list[tuple[str, str]] = []
        self.async_ensure_token_valid = AsyncMock()

    async def async_request(self, method: str, url: str, **kwargs):
        self.calls.append((method, url))
        if method == "GET" and url.endswith("/files"):
            return _Resp(payload={"files": self.files})
        if method == "GET":
            return _Resp(body=self.body)
        if method == "DELETE":
            return _Resp(status=self.delete_status)
        return _Resp()


def _make_backend(tmp_path: Path, oauth, *, options: dict, state: dict | None = None):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(exist_ok=True)
    target_dir = tmp_path / "target"
    target_dir.mkdir(exist_ok=True)
    entry = SimpleNamespace(
        entry_id="e",
        title="t",
        data={CONF_TAKEOUT_WATCH_DIR: str(watch_dir), CONF_TARGET_DIR: str(target_dir)},
        options=options,
    )
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    backend = TakeoutBackend(hass, entry, SyncStateStore(state or {}), oauth_session=oauth)
    return backend, watch_dir


async def test_first_run_cleans_up_drive_copy_after_import(tmp_path: Path):
    drive = _FakeDrive([{"id": "f1", "name": ARCHIVE}], body=_zip_bytes())
    backend, _ = _make_backend(
        tmp_path, drive, options={CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC: True}
    )

    stats = await backend.async_run_backup()

    assert stats.errors == []
    assert stats.archives_done == 1
    assert ("PATCH", "https://www.googleapis.com/drive/v3/files/f1") in drive.calls
    assert backend.state.get("drive_file_id_by_name") == {}


async def test_archives_missed_by_an_earlier_run_are_cleaned_up_now(tmp_path: Path):
    drive = _FakeDrive([])
    backend, _ = _make_backend(
        tmp_path,
        drive,
        options={CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC: True},
        state={
            "processed_archives": [ARCHIVE],
            "drive_file_id_by_name": {ARCHIVE: "old1", "not-imported-yet.zip": "old2"},
        },
    )

    await backend.async_run_backup()

    assert ("PATCH", "https://www.googleapis.com/drive/v3/files/old1") in drive.calls
    assert not any(url.endswith("/old2") for _, url in drive.calls)
    assert backend.state.get("drive_file_id_by_name") == {"not-imported-yet.zip": "old2"}


async def test_refused_permanent_delete_falls_back_to_trash(tmp_path: Path):
    drive = _FakeDrive([], delete_status=403)
    backend, _ = _make_backend(
        tmp_path, drive, options={CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY: True}
    )
    stats = BackupStats()

    await backend._cleanup_drive_file("f1", ARCHIVE, stats)

    assert [m for m, _ in drive.calls] == ["DELETE", "PATCH"]
    assert len(stats.errors) == 1
    assert "moved to Drive's trash instead" in stats.errors[0]


async def test_corrupt_drive_archive_is_downloaded_again(tmp_path: Path):
    drive = _FakeDrive([{"id": "f1", "name": ARCHIVE}], body=b"not a zip")
    backend, watch_dir = _make_backend(tmp_path, drive, options={})

    stats = await backend.async_run_backup()

    assert any("corrupt archive" in e for e in stats.errors)
    assert (watch_dir / f"{ARCHIVE}.corrupt").exists()
    assert not (watch_dir / ARCHIVE).exists()
    assert "f1" not in backend.state.get("downloaded_drive_file_ids", [])

    # Next run fetches it from Drive again instead of skipping the ID.
    drive.body = _zip_bytes()
    drive.calls.clear()
    stats = await backend.async_run_backup()
    assert ("GET", "https://www.googleapis.com/drive/v3/files/f1") in drive.calls
    assert stats.archives_done == 1


async def test_corrupt_archive_is_deleted_when_delete_after_import_is_on(tmp_path: Path):
    backend, watch_dir = _make_backend(
        tmp_path, None, options={CONF_TAKEOUT_DELETE_AFTER_IMPORT: True}
    )
    bad = watch_dir / ARCHIVE
    bad.write_bytes(b"not a zip")

    stats = await backend.async_run_backup()

    assert not bad.exists()
    assert not (watch_dir / f"{ARCHIVE}.corrupt").exists()
    assert "deleted" in stats.errors[0]
