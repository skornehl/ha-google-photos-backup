"""DataUpdateCoordinator driving the periodic backup runs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from aiohttp import ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .backends import BackupBackend, BackupStats, SyncStateStore, async_create_backend
from .backends.fsutil import free_bytes
from .const import (
    CONF_SYNC_INTERVAL_MINUTES,
    CONF_TARGET_DIR,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    DOMAIN,
    PROGRESS_MIN_INTERVAL_SECONDS,
    STORAGE_KEY_TEMPLATE,
    STORAGE_VERSION,
)

_LOGGER = logging.getLogger(__name__)

# Status codes from a failed OAuth token refresh (see
# config_entry_oauth2_flow.OAuth2Session.async_ensure_token_valid(), which
# raises the aiohttp response's own ClientResponseError via
# raise_for_status() on a non-2xx token endpoint response) that mean "the
# grant itself is bad" - revoked/expired refresh token, wrong scope, etc. -
# rather than a transient network/server problem. Distinguishing this
# matters: only these should trigger HA's reauth flow via
# ConfigEntryAuthFailed; anything else (5xx, timeouts, ...) should just be
# a normal, retried UpdateFailed.
AUTH_FAILURE_STATUS_CODES = {400, 401, 403}


@dataclass
class BackupData:
    last_sync: datetime | None
    files_backed_up_total: int
    last_run_files_downloaded: int
    last_run_files_skipped: int
    last_run_errors: list[str]
    free_space_bytes: int | None
    in_progress: bool = False
    #: What the backend is doing right now - see BackupStats in
    #: backends/base.py for the field-by-field meaning. All reset to
    #: their idle defaults once a run finishes (see _async_update_data).
    current_archive: str | None = None
    current_action: str | None = None
    current_archive_bytes_done: int = 0
    current_archive_bytes_total: int | None = None
    archives_total: int = 0
    archives_done: int = 0
    extract_files_done: int = 0
    extract_files_total: int = 0
    import_files_done: int = 0
    import_files_total: int = 0


class GooglePhotosBackupCoordinator(DataUpdateCoordinator[BackupData]):
    """Owns the persisted sync state and the active backend instance.

    files_backed_up_total and friends still only update once per
    completed run (see issue #21): a long initial import can sit "quiet"
    on those for a while before the numbers jump. current_archive/
    current_action/current_archive_bytes_*/extract_files_*/import_files_*
    are the exception - the backend reports those via _handle_progress
    *during* a run (per archive, per chunk within a download, and per
    ~50 members within an extraction or file-move pass), specifically to
    cover the gap the above leaves: a single 50 GB+ archive can otherwise
    show no movement at all for hours, whether it's still downloading,
    being unpacked, or having its files moved into the target library.
    """

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
        self._last_progress_at = 0.0

    @property
    def state_data(self) -> dict[str, Any]:
        """Read-only view of the persisted sync state.

        Exists so diagnostics.py doesn't have to reach into _state_data
        from another module. Callers must treat it as read-only - the
        coordinator owns writing and persisting it.
        """
        return self._state_data

    async def async_setup(self) -> None:
        self._state_data = await self._store.async_load() or {}
        self._files_backed_up_total = self._state_data.get("files_backed_up_total", 0)
        state = SyncStateStore(self._state_data)
        self.backend = await async_create_backend(
            self.hass, self.entry, state, on_progress=self._handle_progress
        )
        try:
            await self.backend.async_validate()
        except ClientResponseError as err:
            if err.status in AUTH_FAILURE_STATUS_CODES:
                raise ConfigEntryAuthFailed(
                    f"Google authorisation invalid or revoked ({err.status})"
                ) from err
            raise

    def _handle_progress(self, stats: BackupStats) -> None:
        """Push intermediate stats from a running backup to the sensors.

        Rate-limited: a large import calls this once per file, and every
        call fans out to a state write per entity. PROGRESS_MIN_INTERVAL
        keeps that from turning a backup run into a recorder flood.
        """
        now = self.hass.loop.time()
        if now - self._last_progress_at < PROGRESS_MIN_INTERVAL_SECONDS:
            return
        self._last_progress_at = now
        self.async_set_updated_data(self._build_data(stats, in_progress=True))

    def _build_data(self, stats: BackupStats, *, in_progress: bool) -> BackupData:
        """Snapshot for the sensors. free_space is only refreshed at the end
        of a run - it needs a blocking stat() on a possibly network-mounted
        path, which isn't worth doing on every progress tick."""
        last_sync_raw = self._state_data.get("last_sync")
        return BackupData(
            last_sync=datetime.fromisoformat(last_sync_raw) if last_sync_raw else None,
            files_backed_up_total=self._files_backed_up_total + stats.files_downloaded,
            last_run_files_downloaded=stats.files_downloaded,
            last_run_files_skipped=stats.files_skipped,
            last_run_errors=stats.errors,
            free_space_bytes=self.data.free_space_bytes if self.data else None,
            in_progress=in_progress,
            current_archive=stats.current_archive,
            current_action=stats.current_action,
            current_archive_bytes_done=stats.current_archive_bytes_done,
            current_archive_bytes_total=stats.current_archive_bytes_total,
            archives_total=stats.archives_total,
            archives_done=stats.archives_done,
            extract_files_done=stats.extract_files_done,
            extract_files_total=stats.extract_files_total,
            import_files_done=stats.import_files_done,
            import_files_total=stats.import_files_total,
        )

    async def _async_update_data(self) -> BackupData:
        assert self.backend is not None
        try:
            stats = await self.backend.async_run_backup()
        except ConfigEntryAuthFailed:
            raise
        except ClientResponseError as err:
            if err.status in AUTH_FAILURE_STATUS_CODES:
                raise ConfigEntryAuthFailed(
                    f"Google authorisation invalid or revoked ({err.status}) - "
                    "please re-authorise the integration."
                ) from err
            raise UpdateFailed(f"Backup run failed: {err}") from err
        except Exception as err:  # noqa: BLE001
            raise UpdateFailed(f"Backup run failed: {err}") from err

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
                _LOGGER.debug("Could not determine free disk space: %s", err)

        return BackupData(
            last_sync=datetime.fromisoformat(self._state_data["last_sync"]),
            files_backed_up_total=self._files_backed_up_total,
            last_run_files_downloaded=stats.files_downloaded,
            last_run_files_skipped=stats.files_skipped,
            last_run_errors=stats.errors,
            free_space_bytes=free,
            in_progress=False,
        )
