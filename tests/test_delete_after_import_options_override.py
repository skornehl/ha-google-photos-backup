"""Regression test: takeout_delete_after_import must be read via the
options-flow override (entry.options), not entry.data directly.

Found live 2026-09-30: entry.data still held the stale `False` collected
during initial setup, while entry.options held the `True` set later via
the integration's "..." options menu. async_run_backup() read entry.data
directly instead of going through the _option() helper (which already
does the correct entry.options -> entry.data fallback, and is used for
the sibling delete_drive_after flag right next to it) - so every archive
was extracted/imported successfully but never deleted from watch_dir,
and got fully re-processed (extract + hash + move) on every subsequent
run.
"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import (
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
)

TAKEN_AT = 1623758400  # 2021-06-15T12:00:00Z


def _build_archive(path: Path, tag: str) -> None:
    base = "Takeout/Google Fotos/Photos from 2021"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{base}/{tag}.jpg", f"content-{tag}".encode())
        zf.writestr(
            f"{base}/{tag}.jpg.supplemental-metadata.json",
            json.dumps({"photoTakenTime": {"timestamp": str(TAKEN_AT)}}),
        )


def _make_backend(watch_dir: Path, target_dir: Path, *, options: dict) -> TakeoutBackend:
    entry = SimpleNamespace(
        data={
            CONF_TAKEOUT_WATCH_DIR: str(watch_dir),
            CONF_TARGET_DIR: str(target_dir),
            CONF_TAKEOUT_DELETE_AFTER_IMPORT: False,
        },
        options=options,
    )
    backend = TakeoutBackend(MagicMock(), entry, SyncStateStore({}))
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    backend.hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    return backend


async def test_options_flow_override_deletes_archive_even_when_data_says_false(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    archive = watch_dir / "takeout-20260923T000000Z-1-001.zip"
    _build_archive(archive, "a")

    backend = _make_backend(
        watch_dir, target_dir, options={CONF_TAKEOUT_DELETE_AFTER_IMPORT: True}
    )

    stats = await backend.async_run_backup()

    assert not archive.exists()
    assert stats.errors == []


async def test_no_options_override_falls_back_to_data(tmp_path: Path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    archive = watch_dir / "takeout-20260923T000000Z-1-001.zip"
    _build_archive(archive, "a")

    backend = _make_backend(watch_dir, target_dir, options={})

    await backend.async_run_backup()

    assert archive.exists()
