"""Tests for backends/fsutil.py - pure filesystem helpers, no HA needed.

Kept dependency-free on purpose (no `hass` fixture): these are the fastest,
lowest-risk tests in the suite and a good canary if the environment itself
is broken (missing pytest, bad Python version, ...).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from custom_components.google_photos_backup.backends.fsutil import (
    dest_dir_for_date,
    ensure_target_dir,
    sha256_file,
    unique_destination,
)


def test_dest_dir_for_date_uses_yyyy_yyyy_mm_layout():
    taken_at = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    assert dest_dir_for_date("/media/photos", taken_at) == Path("/media/photos/2026/2026-08")


def test_dest_dir_for_date_pads_single_digit_months():
    taken_at = datetime(2026, 1, 3, tzinfo=timezone.utc)
    assert dest_dir_for_date("/media/photos", taken_at) == Path("/media/photos/2026/2026-01")


def test_unique_destination_returns_original_name_when_free(tmp_path: Path):
    dest = unique_destination(tmp_path, "IMG_0001.jpg")
    assert dest == tmp_path / "IMG_0001.jpg"


def test_unique_destination_appends_numeric_suffix_on_collision(tmp_path: Path):
    (tmp_path / "IMG_0001.jpg").write_bytes(b"existing file")

    dest = unique_destination(tmp_path, "IMG_0001.jpg")

    assert dest == tmp_path / "IMG_0001_1.jpg"


def test_unique_destination_keeps_incrementing_past_multiple_collisions(tmp_path: Path):
    (tmp_path / "IMG_0001.jpg").write_bytes(b"a")
    (tmp_path / "IMG_0001_1.jpg").write_bytes(b"b")
    (tmp_path / "IMG_0001_2.jpg").write_bytes(b"c")

    dest = unique_destination(tmp_path, "IMG_0001.jpg")

    assert dest == tmp_path / "IMG_0001_3.jpg"


def test_unique_destination_creates_missing_parent_dirs(tmp_path: Path):
    dest_dir = tmp_path / "2026" / "2026-08"

    dest = unique_destination(dest_dir, "IMG_0001.jpg")

    assert dest_dir.is_dir()
    assert dest == dest_dir / "IMG_0001.jpg"


def test_sha256_file_matches_known_digest(tmp_path: Path):
    path = tmp_path / "hello.txt"
    path.write_bytes(b"hello world")

    # sha256("hello world") - independently verifiable, not derived from
    # the implementation under test.
    expected = "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    assert sha256_file(path) == expected


def test_sha256_file_is_stable_across_chunk_boundaries(tmp_path: Path):
    """Regression guard: sha256_file reads in 1 MiB chunks - make sure a
    file straddling that boundary still hashes identically to a single
    hashlib.sha256(data).hexdigest() call."""
    import hashlib

    data = b"x" * (1024 * 1024 + 12345)  # > CHUNK_SIZE
    path = tmp_path / "big.bin"
    path.write_bytes(data)

    assert sha256_file(path) == hashlib.sha256(data).hexdigest()


def test_ensure_target_dir_raises_on_missing_directory(tmp_path: Path):
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ValueError, match="does not exist"):
        ensure_target_dir(str(missing))


def test_ensure_target_dir_raises_on_file_instead_of_directory(tmp_path: Path):
    a_file = tmp_path / "not-a-dir"
    a_file.write_bytes(b"")
    with pytest.raises(ValueError, match="does not exist"):
        ensure_target_dir(str(a_file))


def test_ensure_target_dir_accepts_writable_directory(tmp_path: Path):
    # Should not raise, and should not leave the write-test probe behind.
    ensure_target_dir(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_ensure_target_dir_raises_on_readonly_directory(tmp_path: Path):
    import os

    if os.getuid() == 0:
        pytest.skip("root bypasses filesystem permission checks")

    tmp_path.chmod(0o500)  # read + execute, no write
    try:
        with pytest.raises(ValueError, match="not writable"):
            ensure_target_dir(str(tmp_path))
    finally:
        tmp_path.chmod(0o700)  # restore so pytest can clean up tmp_path
