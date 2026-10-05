"""Regression tests for the 0.10.7 Takeout fixes.

  - free_space stayed "unknown" for the whole run - it was only measured at
    the end, and a big first import runs for days.
  - Extraction dirs were created directly in the photo library, and runs
    interrupted mid-import (HA restart/reload) left them there for good.
  - Edited copies ("-edited", "-bearbeitet") never found the original's
    sidecar when the original's name was short, so they were filed by
    file mtime.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import (
    EXTRACT_PARENT,
    TakeoutBackend,
)
from custom_components.google_photos_backup.const import (
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DOMAIN,
    FREE_SPACE_REFRESH_SECONDS,
)
from custom_components.google_photos_backup.coordinator import GooglePhotosBackupCoordinator

TAKEN_AT = 1623758400  # 2021-06-15T12:00:00Z


async def test_free_space_is_refreshed_during_a_run_not_only_at_its_end(hass, tmp_path):
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_TARGET_DIR: str(tmp_path)})
    entry.add_to_hass(hass)
    coordinator = GooglePhotosBackupCoordinator(hass, entry)
    readings = iter([111, 222, 333])
    clock = {"now": 1000.0}

    with (
        patch(
            "custom_components.google_photos_backup.coordinator.free_bytes",
            side_effect=lambda _d: next(readings),
        ),
        patch.object(hass.loop, "time", side_effect=lambda: clock["now"]),
    ):
        await coordinator._async_refresh_free_space()
        assert coordinator._free_space_bytes == 111

        clock["now"] += 10  # too soon: no new measurement
        coordinator._handle_progress(BackupStats())
        await hass.async_block_till_done()
        assert coordinator.data.free_space_bytes == 111

        clock["now"] += FREE_SPACE_REFRESH_SECONDS
        coordinator._handle_progress(BackupStats())
        await hass.async_block_till_done()
        assert coordinator._free_space_bytes == 222


def _make_backend(tmp_path: Path) -> tuple[TakeoutBackend, Path, Path]:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "library"
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
    return TakeoutBackend(hass, entry, SyncStateStore({})), watch_dir, target_dir


async def test_extraction_happens_in_hidden_dir_and_leftovers_are_removed(tmp_path: Path):
    backend, watch_dir, library = _make_backend(tmp_path)
    legacy = library / "gpb_takeout_abc123"
    (legacy / "Takeout").mkdir(parents=True)
    (legacy / "Takeout" / "half.jpg").write_bytes(b"truncated")
    stale = library / EXTRACT_PARENT / "gpb_takeout_def456"
    stale.mkdir(parents=True)
    unrelated = library / "2021"
    unrelated.mkdir()

    seen_extract_dirs: list[Path] = []
    real_extract = backend._extract

    def _spy(archive, dest, stats):
        seen_extract_dirs.append(dest)
        real_extract(archive, dest, stats)

    backend._extract = _spy
    with zipfile.ZipFile(watch_dir / "takeout-x-1-001.zip", "w") as zf:
        zf.writestr("Takeout/Google Photos/a.jpg", b"content")

    stats = await backend.async_run_backup()

    assert stats.errors == []
    assert not legacy.exists() and not stale.exists()
    assert unrelated.exists()
    assert seen_extract_dirs[0].parent == library / EXTRACT_PARENT
    assert list((library / EXTRACT_PARENT).iterdir()) == []


def test_edited_copy_uses_the_originals_sidecar(tmp_path: Path):
    (tmp_path / "EFFECTS.jpg").write_text("original")
    (tmp_path / "EFFECTS-bearbeitet.jpg").write_text("edited")
    (tmp_path / "IMG_9-edited.jpg").write_text("edited")
    sidecar = tmp_path / "EFFECTS.jpg.supplemental-metadata.json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": str(TAKEN_AT)}}))
    img_sidecar = tmp_path / "IMG_9.jpg.json"
    img_sidecar.write_text("{}")

    assert TakeoutBackend._find_sidecar(tmp_path / "EFFECTS-bearbeitet.jpg") == sidecar
    assert TakeoutBackend._find_sidecar(tmp_path / "IMG_9-edited.jpg") == img_sidecar
    backend = TakeoutBackend(MagicMock(), SimpleNamespace(data={}, options={}), SyncStateStore({}))
    assert backend._resolve_taken_at(tmp_path / "EFFECTS-bearbeitet.jpg") == datetime(
        2021, 6, 15, 12, 0, tzinfo=timezone.utc
    )
