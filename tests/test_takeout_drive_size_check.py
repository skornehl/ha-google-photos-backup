"""Regression tests for the Drive-download integrity check (issue #2).

_local_file_matches_drive_size() is what stops _sync_drive_folder() from
permanently trusting a truncated file left behind by a crash mid-download
(HA restart while throttled_stream_to_file() was still writing).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.google_photos_backup.backends.takeout_backend import (
    TakeoutBackend,
)


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    path = tmp_path / "takeout-20260801T000000Z-001.zip"
    path.write_bytes(b"x" * 1000)
    return path


def test_matches_when_size_equal_string(archive: Path):
    assert TakeoutBackend._local_file_matches_drive_size(archive, "1000") is True


def test_matches_when_size_equal_int(archive: Path):
    assert TakeoutBackend._local_file_matches_drive_size(archive, 1000) is True


def test_does_not_match_when_local_file_is_smaller(archive: Path):
    """The exact scenario from issue #2: a crash mid-download leaves a
    truncated file - Drive says 1000 bytes, disk only has part of it."""
    assert TakeoutBackend._local_file_matches_drive_size(archive, "5_000_000") is False


def test_does_not_match_when_sizes_differ_slightly(archive: Path):
    assert TakeoutBackend._local_file_matches_drive_size(archive, "999") is False
    assert TakeoutBackend._local_file_matches_drive_size(archive, "1001") is False


def test_trusts_existing_file_when_drive_reports_no_size(archive: Path):
    """Defensive fallback: don't be stricter than the Drive API itself."""
    assert TakeoutBackend._local_file_matches_drive_size(archive, None) is True


def test_treats_unparseable_size_as_mismatch(archive: Path):
    assert TakeoutBackend._local_file_matches_drive_size(archive, "not-a-number") is False


def test_treats_missing_file_as_mismatch(tmp_path: Path):
    missing = tmp_path / "gone.zip"
    assert TakeoutBackend._local_file_matches_drive_size(missing, "1000") is False
