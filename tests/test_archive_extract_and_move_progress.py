"""Tests for the extract/move progress _import_archive reports for a
single archive (member count while unpacking, matched-file count while
moving into the target library) - the finer-grained layer below
test_archive_progress_reporting.py's per-archive counts.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend

TAKEN_AT = 1623758400  # 2021-06-15T12:00:00Z


def _build_archive(path: Path, tags: list[str]) -> None:
    base = "Takeout/Google Fotos/Photos from 2021"
    with zipfile.ZipFile(path, "w") as zf:
        for tag in tags:
            zf.writestr(f"{base}/{tag}.jpg", f"content-{tag}".encode())
            zf.writestr(
                f"{base}/{tag}.jpg.supplemental-metadata.json",
                json.dumps({"photoTakenTime": {"timestamp": str(TAKEN_AT)}}),
            )


def _make_backend() -> TakeoutBackend:
    backend = TakeoutBackend(MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({}))
    # _report_progress_threadsafe marshals through hass.loop.call_soon_threadsafe -
    # make it call straight through so the test can observe every report.
    backend.hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    return backend


def test_extract_and_move_progress_report_member_and_file_counts(tmp_path: Path):
    archive = tmp_path / "takeout-20260923T000000Z-001.zip"
    _build_archive(archive, ["a", "b", "c"])
    target = tmp_path / "target"
    target.mkdir()

    backend = _make_backend()
    snapshots: list[tuple[str | None, int, int, int, int]] = []
    backend._on_progress = lambda s: snapshots.append(
        (s.current_action, s.extract_files_done, s.extract_files_total, s.import_files_done, s.import_files_total)
    )
    stats = BackupStats()

    backend._import_archive(archive, str(target), stats)

    assert stats.files_downloaded == 3
    # 3 media files + 3 json sidecars = 6 members total to extract.
    extracting = [s for s in snapshots if s[0] == "extracting"]
    assert extracting, "expected at least one 'extracting' progress report"
    assert extracting[-1][2] == 6  # extract_files_total
    assert extracting[-1][1] == 6  # extract_files_done reaches the total

    # Only the 3 real media files (not the json sidecars) get moved.
    moving = [s for s in snapshots if s[0] == "moving"]
    assert moving, "expected at least one 'moving' progress report"
    assert moving[-1][4] == 3  # import_files_total
    assert moving[-1][3] == 3  # import_files_done reaches the total


def test_extract_progress_reports_before_move_starts(tmp_path: Path):
    """The two phases must never overlap in what they report - moving
    only starts once every member has actually been extracted."""
    archive = tmp_path / "takeout-20260923T000000Z-001.zip"
    _build_archive(archive, ["a"])
    target = tmp_path / "target"
    target.mkdir()

    backend = _make_backend()
    actions_seen: list[str | None] = []
    backend._on_progress = lambda s: actions_seen.append(s.current_action)
    stats = BackupStats()

    backend._import_archive(archive, str(target), stats)

    first_moving_index = actions_seen.index("moving")
    assert all(a == "extracting" for a in actions_seen[:first_moving_index])
