"""Tests for archive-level progress during TakeoutBackend.async_run_backup.

Downloads report their own progress (see test_curl_session_download.py);
a single archive's extraction/move-into-library report theirs (see
test_archive_extract_and_move_progress.py). This covers the layer above:
archives_total/archives_done while async_run_backup works through the
list of archives found in watch_dir.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import CONF_TAKEOUT_WATCH_DIR, CONF_TARGET_DIR

TAKEN_AT = 1623758400  # 2021-06-15T12:00:00Z


def _build_archive(path: Path, tag: str) -> None:
    base = "Takeout/Google Fotos/Photos from 2021"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{base}/{tag}.jpg", f"content-{tag}".encode())
        zf.writestr(
            f"{base}/{tag}.jpg.supplemental-metadata.json",
            json.dumps({"photoTakenTime": {"timestamp": str(TAKEN_AT)}}),
        )


def _make_backend(watch_dir: Path, target_dir: Path) -> TakeoutBackend:
    entry = SimpleNamespace(
        data={
            CONF_TAKEOUT_WATCH_DIR: str(watch_dir),
            CONF_TARGET_DIR: str(target_dir),
        },
        options={},
    )
    backend = TakeoutBackend(MagicMock(), entry, SyncStateStore({}))
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    # _extract/the move loop report via _report_progress_threadsafe, which
    # marshals onto hass.loop.call_soon_threadsafe - make that call
    # straight through synchronously rather than wiring a real event loop.
    backend.hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    return backend


async def test_archive_counts_move_through_the_import_loop(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    _build_archive(watch_dir / "takeout-20260923T000000Z-1-001.zip", "a")
    _build_archive(watch_dir / "takeout-20260923T000000Z-1-002.zip", "b")

    backend = _make_backend(watch_dir, target_dir)
    snapshots: list[tuple[int, int, str | None, str | None]] = []
    backend._on_progress = lambda s: snapshots.append(
        (s.archives_total, s.archives_done, s.current_action, s.current_archive)
    )

    stats = await backend.async_run_backup()

    assert stats.files_downloaded == 2
    assert stats.archives_total == 2
    assert stats.archives_done == 2
    # Both sub-phases were seen, against the right archive, while archive
    # 1 was still in progress (archives_done == 0).
    assert (2, 0, "extracting", "takeout-20260923T000000Z-1-001.zip") in snapshots
    assert (2, 0, "moving", "takeout-20260923T000000Z-1-001.zip") in snapshots
    # And again for archive 2, after archive 1 finished (archives_done == 1).
    assert (2, 1, "extracting", "takeout-20260923T000000Z-1-002.zip") in snapshots
    assert (2, 1, "moving", "takeout-20260923T000000Z-1-002.zip") in snapshots
    # Idle again once the whole run has finished.
    assert stats.current_action is None
    assert stats.current_archive is None
    assert stats.extract_files_done == 0
    assert stats.extract_files_total == 0
    assert stats.import_files_done == 0
    assert stats.import_files_total == 0


async def test_a_failed_archive_still_clears_current_action(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    bad = watch_dir / "takeout-20260923T000000Z-1-001.zip"
    bad.write_bytes(b"not a zip file")

    backend = _make_backend(watch_dir, target_dir)
    stats = await backend.async_run_backup()

    assert len(stats.errors) == 1
    assert stats.current_action is None
    assert stats.current_archive is None
    assert stats.archives_done == 0
