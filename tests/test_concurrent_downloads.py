"""Tests for concurrent item downloads (issue #20).

Two things make concurrency here non-trivial, and both are what these
tests actually target:

  1. bandwidth_limit_kbps must stay a *total*. One pacer per download
     would let N workers each run at the full configured rate, silently
     multiplying the user's cap by N.
  2. Destination names are only reserved by existence, but the file
     appears at the final rename - so two overlapping downloads handed
     the same filename would otherwise pick the same path and one would
     overwrite the other.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.google_photos_backup.backends import library_api as library_api_module
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.fsutil import unique_destination
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.backends.throttle import BandwidthPacer
from custom_components.google_photos_backup.const import (
    CONF_DOWNLOAD_CONCURRENCY,
    CONF_TARGET_DIR,
    DEFAULT_DOWNLOAD_CONCURRENCY,
    MAX_DOWNLOAD_CONCURRENCY,
)


def _backend(tmp_path: Path, options: dict | None = None) -> LibraryApiBackend:
    entry = SimpleNamespace(data={CONF_TARGET_DIR: str(tmp_path)}, options=options or {})
    oauth = MagicMock()
    oauth.async_request = AsyncMock(return_value=MagicMock())
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    return LibraryApiBackend(hass, entry, SyncStateStore({}), oauth_session=oauth)


def _item(item_id: str, filename: str) -> dict:
    return {
        "id": item_id,
        "createTime": "2026-08-01T00:00:00Z",
        "mediaFile": {
            "baseUrl": f"https://example.com/{item_id}",
            "filename": filename,
            "mimeType": "image/jpeg",
        },
    }


# -- 1. shared bandwidth budget -------------------------------------------


async def test_one_pacer_shared_across_workers_keeps_the_limit_total():
    """Ten 'downloads' of 100 KiB each through one 100 KiB/s pacer must
    take about ten seconds' worth of accounted time - not one second,
    which is what per-worker pacers would produce."""
    pacer = BandwidthPacer(limit_kbps=100)
    slept = 0.0
    real_sleep = asyncio.sleep

    async def _fake_sleep(d):
        nonlocal slept
        slept += d
        await real_sleep(0)

    library_api_module.asyncio.sleep  # noqa: B018 - module import sanity
    import custom_components.google_photos_backup.backends.throttle as throttle_mod

    orig = throttle_mod.asyncio.sleep
    throttle_mod.asyncio.sleep = _fake_sleep
    try:
        await asyncio.gather(*(pacer.account(100 * 1024) for _ in range(10)))
    finally:
        throttle_mod.asyncio.sleep = orig

    # 10 x 100 KiB at 100 KiB/s == ~10s of pacing, minus the first chunk
    # which needs no delay.
    assert slept >= 8.0, f"zu wenig gedrosselt: {slept}s"


async def test_unlimited_pacer_never_sleeps():
    pacer = BandwidthPacer(limit_kbps=0)
    assert pacer.unlimited is True
    await pacer.account(10 * 1024 * 1024)  # must return immediately


# -- 2. destination reservation -------------------------------------------


def test_unique_destination_skips_reserved_but_nonexistent_paths(tmp_path: Path):
    """The core race: a reserved path doesn't exist yet, because the file
    only appears at the final rename."""
    first = unique_destination(tmp_path, "IMG_0001.jpg", set())
    reserved = {first}

    second = unique_destination(tmp_path, "IMG_0001.jpg", reserved)

    assert first.name == "IMG_0001.jpg"
    assert second != first, "zweiter Worker bekam denselben Pfad"
    assert second.name == "IMG_0001_1.jpg"


def test_unique_destination_without_reservations_behaves_as_before(tmp_path: Path):
    assert unique_destination(tmp_path, "a.jpg").name == "a.jpg"
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert unique_destination(tmp_path, "a.jpg").name == "a_1.jpg"


async def test_two_same_named_items_do_not_overwrite_each_other(monkeypatch, tmp_path: Path):
    """End to end: two concurrent downloads whose Google filenames collide
    must produce two distinct files."""
    started = asyncio.Event()

    async def _slow_stream(resp, dest, hass, limit_kbps, pacer=None):
        # Hold the first download open so the second reserves while the
        # first is still in flight - the exact overlap that breaks a
        # pure existence check.
        if not started.is_set():
            started.set()
            await asyncio.sleep(0.05)
        dest.write_bytes(b"data")
        return 4

    monkeypatch.setattr(library_api_module, "throttled_stream_to_file", _slow_stream)

    backend = _backend(tmp_path)
    stats = BackupStats()
    await asyncio.gather(
        backend._download_picker_item(_item("a", "IMG_0001.jpg"), str(tmp_path), stats),
        backend._download_picker_item(_item("b", "IMG_0001.jpg"), str(tmp_path), stats),
    )

    written = sorted(p.name for p in (tmp_path / "2026" / "2026-08").iterdir())
    assert written == ["IMG_0001.jpg", "IMG_0001_1.jpg"], written
    assert stats.files_downloaded == 2
    assert backend._reserved_destinations == set(), "reservations were not released"


async def test_reservation_is_released_even_when_the_download_fails(monkeypatch, tmp_path: Path):
    async def _boom(resp, dest, hass, limit_kbps, pacer=None):
        raise ConnectionError("weg")

    monkeypatch.setattr(library_api_module, "throttled_stream_to_file", _boom)

    backend = _backend(tmp_path)
    stats = BackupStats()
    await backend._download_picker_item(_item("a", "x.jpg"), str(tmp_path), stats)

    assert stats.errors, "error was not reported"
    assert backend._reserved_destinations == set(), "reservation not released after an error"


# -- 3. concurrency configuration ------------------------------------------


def test_concurrency_defaults_and_clamps(tmp_path: Path):
    assert _backend(tmp_path)._download_concurrency() == DEFAULT_DOWNLOAD_CONCURRENCY
    assert _backend(tmp_path, {CONF_DOWNLOAD_CONCURRENCY: 1})._download_concurrency() == 1
    # A typo must not fire hundreds of parallel requests at Google.
    assert (
        _backend(tmp_path, {CONF_DOWNLOAD_CONCURRENCY: 999})._download_concurrency()
        == MAX_DOWNLOAD_CONCURRENCY
    )
    assert _backend(tmp_path, {CONF_DOWNLOAD_CONCURRENCY: 0})._download_concurrency() == 1
    assert (
        _backend(tmp_path, {CONF_DOWNLOAD_CONCURRENCY: "kaputt"})._download_concurrency()
        == DEFAULT_DOWNLOAD_CONCURRENCY
    )
