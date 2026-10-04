"""Regression tests for rclone's file/byte counting.

rclone logs per-file "Copied (new)" messages at INFO level, which never
appear at its default NOTICE level - so counting those always reported 0
files backed up. The cumulative `stats` object of rclone's JSON stats
lines is what's counted now, with the stats raised to NOTICE level.
"""
from __future__ import annotations

import json

from custom_components.google_photos_backup.backends.base import BackupStats
from custom_components.google_photos_backup.backends.rclone_backend import RcloneBackend


def _line(**fields) -> str:
    return json.dumps({"time": "2026-10-04T10:00:00Z", "source": "x", **fields})


def test_last_stats_line_provides_the_totals():
    log = "\n".join(
        [
            _line(level="notice", msg="stats", stats={"transfers": 2, "bytes": 200, "checks": 1}),
            _line(level="error", object="a.jpg", msg="Failed to copy: boom"),
            _line(level="notice", msg="stats", stats={"transfers": 5, "bytes": 512, "checks": 7}),
        ]
    )
    stats = BackupStats()

    RcloneBackend._parse_json_log(log, stats)

    assert (stats.files_downloaded, stats.bytes_downloaded, stats.files_skipped) == (5, 512, 7)
    assert stats.errors == ["Failed to copy: boom"]


def test_per_file_lines_are_still_counted_without_stats():
    log = _line(level="info", object="a.jpg", objectType="*x.Object", msg="Copied (new)", size=10)
    stats = BackupStats()

    RcloneBackend._parse_json_log(log, stats)

    assert (stats.files_downloaded, stats.bytes_downloaded) == (1, 10)


def test_garbage_and_non_object_lines_are_ignored():
    stats = BackupStats()
    RcloneBackend._parse_json_log('not json\n{broken\n[1, 2]\n"str"', stats)
    assert stats == BackupStats()
