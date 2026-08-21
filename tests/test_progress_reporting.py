"""Tests for mid-run progress reporting (issue #21).

Sensors used to update only once a run had finished, so a long initial
import looked stalled. Backends now publish intermediate BackupStats
through a callback the coordinator supplies - deliberately one-way, so
backends still never read coordinator state.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.google_photos_backup.backends.base import (
    BackupBackend,
    BackupStats,
    SyncStateStore,
)
from custom_components.google_photos_backup.const import PROGRESS_MIN_INTERVAL_SECONDS


class _Backend(BackupBackend):
    async def async_validate(self) -> None: ...
    async def async_run_backup(self) -> BackupStats: ...


def _backend(on_progress=None) -> _Backend:
    return _Backend(MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({}), on_progress)


def test_reporting_without_a_callback_is_a_noop():
    """Backends call _report_progress unconditionally; constructing one
    without a callback (tests, direct use) must not blow up."""
    _backend()._report_progress(BackupStats())


def test_callback_receives_the_stats():
    seen = []
    b = _backend(on_progress=seen.append)
    stats = BackupStats(files_downloaded=3)
    b._report_progress(stats)

    assert seen == [stats]
    assert seen[0].files_downloaded == 3


def test_stats_carry_an_in_progress_flag():
    assert BackupStats().in_progress is False
    assert BackupStats(in_progress=True).in_progress is True


def test_progress_interval_is_sane():
    """Guard: a value of 0 would publish once per file and flood the
    recorder on a large import - the exact problem the throttle exists
    to prevent."""
    assert PROGRESS_MIN_INTERVAL_SECONDS >= 1.0
