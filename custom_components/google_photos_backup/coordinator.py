"""DataUpdateCoordinator driving the periodic backup runs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .backends import BackupBackend, BackupStats, SyncStateStore, async_create_backend
from .backends.fsutil import free_bytes
from .const import (
    CONF_SYNC_INTERVAL_MINUTES,
    CONF_TARGET_DIR,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    DOMAIN,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class BackupData:
    last_sync: datetime | None
    files_backed_up_total: int
    last_run_files_downloaded: int
    last_run_files_skipped: int
    last_run_errors: list[str]
    free_space_bytes: int | None


class GooglePhotosBackupCoordinator(DataUpdateCoordinator[BackupData]):
    """Owns the persisted sync state and the active backend instance."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        interval_minutes = entry.options.get(
            CONF_SYNC_INTERVAL_MINUTES,
            entry.data.get(CONF_SYNC_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES),
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            update_interval=timedelta(minutes=interval_minutes),
        )
        self.entry = entry
        self._store: Store = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_TEMPLATE.format(entry_id=entry.entry_id)
        )
        self._state_data: dict[str, Any] = {}
        self.backend: BackupBackend | None = None
        self._files_backed_up_total = 0

    async def async_setup(self) -> None:
        self._state_data = await self._store.async_load() or {}
        self._files_backed_up_total = self._state_data.get("files_backed_up_total", 0)
        state = SyncStateStore(self._state_data)
        self.backend = await async_create_backend(self.hass, self.entry, state)
        await self.backend.async_validate()

    async def _async_update_data(self) -> BackupData:
        assert self.backend is not None
        try:
            stats = await self.backend.async_run_backup()
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Backup-Lauf fehlgeschlagen: {err}") from err

        self._files_backed_up_total += stats.files_downloaded
        self._state_data["files_backed_up_total"] = self._files_backed_up_total
        self._state_data["last_sync"] = datetime.now(timezone.utc).isoformat()
        self._state_data["last_errors"] = stats.errors
        await self._store.async_save(self._state_data)

        target_dir = self.entry.data.get(CONF_TARGET_DIR)
        free = None
        if target_dir:
            try:
                free = await self.hass.async_add_executor_job(free_bytes, target_dir)
            except OSError as err:
                _LOGGER.debug("Konnte freien Speicherplatz nicht ermitteln: %s", err)

        return BackupData(
            last_sync=datetime.fromisoformat(self._state_data["last_sync"]),
            files_backed_up_total=self._files_backed_up_total,
            last_run_files_downloaded=stats.files_downloaded,
            last_run_files_skipped=stats.files_skipped,
            last_run_errors=stats.errors,
            free_space_bytes=free,
        )
