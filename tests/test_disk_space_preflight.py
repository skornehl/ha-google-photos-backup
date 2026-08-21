"""Tests for the pre-extraction free-space check (issue #19)."""
from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from custom_components.google_photos_backup.backends import takeout_backend as takeout_module
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend

_Usage = namedtuple("_Usage", ["total", "used", "free"])


def test_passes_when_plenty_of_space(tmp_path: Path, monkeypatch):
    archive = tmp_path / "takeout-001.zip"
    archive.write_bytes(b"x" * 1000)
    monkeypatch.setattr(
        takeout_module.shutil, "disk_usage", lambda _p: _Usage(10**9, 0, 10**9)
    )

    TakeoutBackend._check_free_space(archive, tmp_path)  # must not raise


def test_raises_a_clear_error_when_space_is_short(tmp_path: Path, monkeypatch):
    archive = tmp_path / "takeout-001.zip"
    archive.write_bytes(b"x" * 10_000)
    monkeypatch.setattr(takeout_module.shutil, "disk_usage", lambda _p: _Usage(10_000, 9_000, 500))

    with pytest.raises(ValueError, match="Not enough free disk space"):
        TakeoutBackend._check_free_space(archive, tmp_path)


def test_error_mentions_the_archive_and_that_it_will_be_retried(tmp_path: Path, monkeypatch):
    archive = tmp_path / "takeout-20260801-001.zip"
    archive.write_bytes(b"x" * 10_000)
    monkeypatch.setattr(takeout_module.shutil, "disk_usage", lambda _p: _Usage(10_000, 9_000, 500))

    with pytest.raises(ValueError) as excinfo:
        TakeoutBackend._check_free_space(archive, tmp_path)

    message = str(excinfo.value)
    assert "takeout-20260801-001.zip" in message
    assert "retried on the next run" in message


def test_stays_out_of_the_way_when_it_cannot_measure(tmp_path: Path):
    """A missing archive (or an unreadable dir) must not turn into a
    spurious 'not enough space' error - let the extraction itself fail
    with whatever the real problem is."""
    TakeoutBackend._check_free_space(tmp_path / "does-not-exist.zip", tmp_path)
