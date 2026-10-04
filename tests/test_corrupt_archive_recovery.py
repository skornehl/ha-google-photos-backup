"""Regression test: a corrupt/truncated archive must be moved out of the
way, not left in watch_dir to fail identically forever.

Found live 2026-10-01: "takeout-...-047.zip: File is not a zip file" kept
reappearing on every run because the except-and-continue path in _consume
never deleted the file - _list_new_archives found it again next time
since it was never added to processed_archives either.

By default it is renamed to `<name>.corrupt` rather than deleted (a manually
placed archive can't be re-fetched); deletion only happens with
takeout_delete_after_import on - see test_drive_cleanup_and_corrupt_archives.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import CONF_TAKEOUT_WATCH_DIR, CONF_TARGET_DIR


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
    backend.hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    return backend


async def test_corrupt_zip_is_quarantined_and_not_marked_processed(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    bad = watch_dir / "takeout-20260930T081441Z-1-047.zip"
    bad.write_bytes(b"this is not a zip file")

    backend = _make_backend(watch_dir, target_dir)
    stats = await backend.async_run_backup()

    assert not bad.exists(), "corrupt archive was left in place"
    assert (watch_dir / f"{bad.name}.corrupt").exists()
    assert "processed_archives" not in backend.state.data or (
        bad.name not in backend.state.data["processed_archives"]
    )
    assert len(stats.errors) == 1
    assert "corrupt archive" in stats.errors[0]
    assert "downloaded again" in stats.errors[0]
    assert stats.archives_done == 0
