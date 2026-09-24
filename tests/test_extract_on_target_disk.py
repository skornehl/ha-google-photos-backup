"""Regression test: archive extraction must happen on target_dir's own
filesystem, not the OS default temp location.

Home Assistant OS mounts /tmp as tmpfs (RAM-backed) - confirmed in
practice 2026-09-24 that a 50 GB+ Takeout archive fails to extract there
("only 3918 MiB available") regardless of how much space the actual
target disk has. _import_archive must pass dir=target_dir to
tempfile.TemporaryDirectory so extraction lands on the same filesystem
as the final output (which also turns the closing shutil.move() into a
same-filesystem rename instead of a cross-filesystem copy).
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend


def test_extraction_temp_dir_is_created_under_target_dir(monkeypatch, tmp_path: Path):
    archive = tmp_path / "takeout-20260923T000000Z-001.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("Takeout/Google Fotos/a.jpg", b"content")
        zf.writestr(
            "Takeout/Google Fotos/a.jpg.supplemental-metadata.json",
            json.dumps({"photoTakenTime": {"timestamp": "1623758400"}}),
        )
    target = tmp_path / "target"
    target.mkdir()

    seen_extract_dirs: list[Path] = []
    original_extract = TakeoutBackend._extract

    @staticmethod
    def _spying_extract(archive_path: Path, dest: Path) -> None:
        seen_extract_dirs.append(dest)
        original_extract(archive_path, dest)

    monkeypatch.setattr(TakeoutBackend, "_extract", _spying_extract)

    backend = TakeoutBackend(MagicMock(), None, SyncStateStore({}))
    stats = BackupStats()
    backend._import_archive(archive, str(target), stats)

    assert len(seen_extract_dirs) == 1
    extract_dir = seen_extract_dirs[0]
    # The whole point: the temp extraction dir must be a child of
    # target_dir (same filesystem), not somewhere under the system temp
    # root (which would resolve to /tmp - tmpfs on Home Assistant OS).
    assert extract_dir.parent == target
    assert stats.files_downloaded == 1
    assert stats.errors == []
    # And it must be gone again afterwards - TemporaryDirectory cleans up,
    # this isn't meant to leave visible clutter in the photo library.
    assert not extract_dir.exists()
