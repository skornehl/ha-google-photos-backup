"""Regression tests for the download-speed sensor (coordinator's
_update_download_speed - see DownloadSpeedSensor in sensor.py).

_update_download_speed takes `now` as an explicit float rather than
reading hass.loop.time() itself, so these tests drive it directly with
controlled timestamps instead of needing a real event loop clock.
"""
from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.base import BackupStats
from custom_components.google_photos_backup.const import DOMAIN
from custom_components.google_photos_backup.coordinator import GooglePhotosBackupCoordinator


def _coordinator(hass) -> GooglePhotosBackupCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    return GooglePhotosBackupCoordinator(hass, entry)


def _stats(archive: str | None, bytes_done: int) -> BackupStats:
    stats = BackupStats()
    stats.download_archive = archive
    stats.download_bytes_done = bytes_done
    return stats


async def test_speed_is_the_byte_delta_over_elapsed_time(hass):
    coordinator = _coordinator(hass)

    coordinator._update_download_speed(_stats("a.zip", 0), now=0.0)
    assert coordinator._download_speed_bps == 0.0  # first tick for this archive, no delta yet

    coordinator._update_download_speed(_stats("a.zip", 5000), now=5.0)
    assert coordinator._download_speed_bps == 1000.0  # 5000 bytes / 5s


async def test_speed_resets_to_zero_when_the_archive_changes(hass):
    coordinator = _coordinator(hass)

    coordinator._update_download_speed(_stats("a.zip", 0), now=0.0)
    coordinator._update_download_speed(_stats("a.zip", 5000), now=5.0)
    assert coordinator._download_speed_bps == 1000.0

    # New archive starts from 0 bytes - without the archive-change guard
    # this would read as a huge negative delta against a.zip's 5000.
    coordinator._update_download_speed(_stats("b.zip", 0), now=6.0)
    assert coordinator._download_speed_bps == 0.0

    coordinator._update_download_speed(_stats("b.zip", 2000), now=8.0)
    assert coordinator._download_speed_bps == 1000.0  # 2000 bytes / 2s


async def test_speed_resets_to_zero_when_idle(hass):
    coordinator = _coordinator(hass)

    coordinator._update_download_speed(_stats("a.zip", 0), now=0.0)
    coordinator._update_download_speed(_stats("a.zip", 5000), now=5.0)
    assert coordinator._download_speed_bps == 1000.0

    coordinator._update_download_speed(_stats(None, 0), now=6.0)
    assert coordinator._download_speed_bps == 0.0


async def test_speed_resets_on_bytes_going_backwards(hass):
    """A retry of the same archive name restarting from 0 bytes must not
    read as a negative/bogus rate."""
    coordinator = _coordinator(hass)

    coordinator._update_download_speed(_stats("a.zip", 8000), now=0.0)
    coordinator._update_download_speed(_stats("a.zip", 2000), now=1.0)
    assert coordinator._download_speed_bps == 0.0
