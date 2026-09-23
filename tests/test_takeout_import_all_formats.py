"""Regression test: a Takeout import must not silently drop media formats.

The import used to keep only a fixed allowlist of eleven suffixes (jpg,
png, heic, mp4, ...). Anything else - RAW files, .mkv, .webm, .tif, .avif,
motion-photo .mp, ... - was discarded without an error, and because the
archive was then marked processed, no later run would pick those files up
again. Now everything except Takeout's own metadata (.json sidecars, the
archive_browser.html index) is imported.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend

CONTENT_FILES = [
    "IMG_0001.jpg",
    "IMG_0002.DNG",
    "IMG_0003.CR2",
    "PXL_0004.MP",
    "clip_0005.mkv",
    "clip_0006.webm",
    "scan_0007.tif",
    "photo_0008.avif",
    "video_0009.MTS",
]
# 2021-06-15T12:00:00Z
TAKEN_AT = 1623758400


def _build_archive(path: Path) -> None:
    base = "Takeout/Google Fotos/Photos from 2021"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("Takeout/archive_browser.html", "<html></html>")
        zf.writestr(f"{base}/metadata.json", json.dumps({"title": "Photos from 2021"}))
        for i, name in enumerate(CONTENT_FILES):
            # Distinct bytes per file, otherwise the hash dedupe would
            # (correctly) skip all but the first.
            zf.writestr(f"{base}/{name}", f"content-{i}".encode())
            zf.writestr(
                f"{base}/{name}.supplemental-metadata.json",
                json.dumps({"photoTakenTime": {"timestamp": str(TAKEN_AT)}}),
            )


def test_imports_every_content_file_and_no_metadata(tmp_path: Path):
    archive = tmp_path / "takeout-20260923T000000Z-001.zip"
    _build_archive(archive)
    target = tmp_path / "target"
    target.mkdir()

    backend = TakeoutBackend(
        MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({})
    )
    stats = BackupStats()
    backend._import_archive(archive, str(target), stats)

    imported = sorted(p.name for p in target.rglob("*") if p.is_file())
    assert imported == sorted(CONTENT_FILES)
    assert stats.files_downloaded == len(CONTENT_FILES)
    # Filed by the sidecar's photoTakenTime, not by extraction time.
    assert all(p.parent == target / "2021" / "2021-06" for p in target.rglob("*") if p.is_file())
