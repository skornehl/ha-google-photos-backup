"""Regression tests for long Takeout runs surviving failure and scale.

  - A producer exception (e.g. a revoked Drive token) skipped the queue
    sentinel; gather() returned early and left the consumer waiting on the
    queue forever as a detached task.
  - Sync state was only saved at the very end of a successful run, so an
    HA restart/reload mid-import lost processed_hashes/processed_archives
    and the next run re-imported everything as `_1` duplicates.
  - processed_hashes rebuilt a set from the full list on every lookup:
    quadratic over a library-sized import.
  - An odd sidecar timestamp (out of range, wrong type) failed the whole
    archive, on every run.
  - Duplicate names ("IMG_1(1).jpg") got the original's sidecar/date.
"""
from __future__ import annotations

import asyncio
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import CONF_TAKEOUT_WATCH_DIR, CONF_TARGET_DIR


def _make_backend(tmp_path: Path, *, oauth=None, request_save=None) -> tuple[TakeoutBackend, Path]:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    entry = SimpleNamespace(
        entry_id="e",
        title="t",
        data={CONF_TAKEOUT_WATCH_DIR: str(watch_dir), CONF_TARGET_DIR: str(target_dir)},
        options={},
    )
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    state = SyncStateStore({}, request_save=request_save)
    return TakeoutBackend(hass, entry, state, oauth_session=oauth), watch_dir


def _write_archive(path: Path, files: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


async def test_producer_failure_does_not_leave_the_consumer_running(tmp_path: Path):
    oauth = MagicMock()
    oauth.async_ensure_token_valid = AsyncMock(side_effect=RuntimeError("invalid_grant"))
    backend, _ = _make_backend(tmp_path, oauth=oauth)
    before = {t for t in asyncio.all_tasks()}

    with pytest.raises(RuntimeError, match="invalid_grant"):
        await asyncio.wait_for(backend.async_run_backup(), timeout=5)

    await asyncio.sleep(0)
    leftover = {t for t in asyncio.all_tasks() if not t.done()} - before
    leftover.discard(asyncio.current_task())
    assert leftover == set(), f"tasks left running after the run failed: {leftover}"


async def test_state_save_is_requested_after_each_imported_archive(tmp_path: Path):
    saves = MagicMock()
    backend, watch_dir = _make_backend(tmp_path, request_save=saves)
    for n in (1, 2):
        _write_archive(watch_dir / f"takeout-x-1-00{n}.zip", {f"T/a{n}.jpg": f"c{n}"})

    stats = await backend.async_run_backup()

    assert stats.archives_done == 2
    assert saves.call_count == 2


def test_processed_hashes_is_a_live_set_kept_in_sync_with_the_list():
    data: dict = {"processed_hashes": ["a"]}
    state = SyncStateStore(data)

    hashes = state.processed_hashes
    assert hashes is state.processed_hashes, "set rebuilt on every access"
    state.add_processed_hash("b")
    state.add_processed_hash("b")

    assert "b" in hashes
    assert data["processed_hashes"] == ["a", "b"]


@pytest.mark.parametrize(
    "sidecar",
    [
        {"photoTakenTime": {"timestamp": "99999999999999999"}},  # out of range
        {"photoTakenTime": "not-a-dict"},
        ["not", "a", "dict"],
    ],
)
async def test_odd_sidecar_falls_back_to_mtime_instead_of_failing_the_archive(
    tmp_path: Path, sidecar
):
    backend, watch_dir = _make_backend(tmp_path)
    _write_archive(
        watch_dir / "takeout-x-1-001.zip",
        {"T/a.jpg": "content", "T/a.jpg.json": json.dumps(sidecar)},
    )

    stats = await backend.async_run_backup()

    assert stats.errors == []
    assert stats.archives_done == 1
    assert stats.files_downloaded == 1


def test_duplicate_name_gets_its_own_sidecar(tmp_path: Path):
    (tmp_path / "IMG_1.jpg").write_text("original")
    (tmp_path / "IMG_1(1).jpg").write_text("duplicate")
    (tmp_path / "IMG_1.jpg.supplemental-metadata.json").write_text("{}")
    dup_sidecar = tmp_path / "IMG_1.jpg.supplemental-metadata(1).json"
    dup_sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "1623758400"}}))

    assert TakeoutBackend._find_sidecar(tmp_path / "IMG_1(1).jpg") == dup_sidecar
    backend = TakeoutBackend(MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({}))
    taken_at = backend._resolve_taken_at(tmp_path / "IMG_1(1).jpg")
    assert taken_at == datetime(2021, 6, 15, 12, 0, tzinfo=timezone.utc)
