"""Status sensors for a Google Photos Backup config entry."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CURRENT_ACTIVITY,
    ATTR_FILES_BACKED_UP,
    ATTR_FREE_SPACE,
    ATTR_LAST_ERROR,
    ATTR_LAST_SYNC,
    ATTR_PROGRESS_PERCENT,
    CONF_BACKEND,
    DOMAIN,
)
from .coordinator import GooglePhotosBackupCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GooglePhotosBackupCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            LastSyncSensor(coordinator, entry),
            FilesBackedUpSensor(coordinator, entry),
            LastErrorSensor(coordinator, entry),
            FreeSpaceSensor(coordinator, entry),
            CurrentActivitySensor(coordinator, entry),
            ProgressPercentSensor(coordinator, entry),
        ]
    )


class _BaseSensor(CoordinatorEntity[GooglePhotosBackupCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Google Photos Backup",
            # Which backend this instance uses, surfaced in the device
            # dialog so two entries (e.g. one takeout, one rclone) are
            # distinguishable by more than just their title.
            model=entry.data.get(CONF_BACKEND),
            entry_type=DeviceEntryType.SERVICE,
        )


class LastSyncSensor(_BaseSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_LAST_SYNC)

    @property
    def native_value(self) -> datetime | None:
        return self.coordinator.data.last_sync if self.coordinator.data else None


class FilesBackedUpSensor(_BaseSensor):
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = "files"
    _attr_icon = "mdi:image-multiple"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_FILES_BACKED_UP)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.files_backed_up_total if self.coordinator.data else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "last_run_downloaded": self.coordinator.data.last_run_files_downloaded,
            "last_run_skipped": self.coordinator.data.last_run_files_skipped,
            # Distinguishes "quiet because idle" from "quiet because a long
            # import is still running" (issue #21).
            "run_active": self.coordinator.data.in_progress,
        }


class LastErrorSensor(_BaseSensor):
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_LAST_ERROR)

    @property
    def native_value(self) -> str:
        if not self.coordinator.data or not self.coordinator.data.last_run_errors:
            return "None"
        first = self.coordinator.data.last_run_errors[0]
        return first[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {"all_errors": self.coordinator.data.last_run_errors}


class FreeSpaceSensor(_BaseSensor):
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = "GB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:harddisk"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_FREE_SPACE)

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data or self.coordinator.data.free_space_bytes is None:
            return None
        return round(self.coordinator.data.free_space_bytes / 1_000_000_000, 2)


class CurrentActivitySensor(_BaseSensor):
    """What the backend is doing right now.

    Exists specifically for the gap FilesBackedUpSensor/LastSyncSensor
    can't cover: those only change once a whole archive has been
    imported, but a single Takeout archive download, extraction, or
    file-move pass can run for hours on its own with zero other visible
    signal (see coordinator.py). "idle" covers both "nothing to do" and
    "between runs" - distinguishing those isn't worth a fifth state, the
    last_sync sensor already answers "when did something last happen".

    "importing" used to be one opaque state covering both unpacking an
    archive and moving its matched files into the target library -
    split into "extracting"/"moving" since each is slow enough on its
    own (tens of thousands of members/files) to want its own progress,
    not just "importing, no further detail".
    """

    _attr_icon = "mdi:sync"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_CURRENT_ACTIVITY)

    @property
    def native_value(self) -> str:
        if not self.coordinator.data or not self.coordinator.data.current_action:
            return "idle"
        action = self.coordinator.data.current_action
        return action

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "current_archive": self.coordinator.data.current_archive,
            "archives_done": self.coordinator.data.archives_done,
            "archives_total": self.coordinator.data.archives_total,
        }


class ProgressPercentSensor(_BaseSensor):
    """Percentage through whatever current_activity currently says is
    happening - downloading (by bytes), extracting, or moving (both by
    file/member count, known upfront from the archive's own index).

    Unavailable (None) while idle, or while downloading a response with
    no Content-Length header (not guaranteed - see takeout_backend.py) -
    deliberately unavailable rather than a stale or misleading number.
    """

    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:progress-check"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, ATTR_PROGRESS_PERCENT)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        if not data:
            return None
        if data.current_action == "downloading":
            if not data.current_archive_bytes_total:
                return None
            done, total = data.current_archive_bytes_done, data.current_archive_bytes_total
        elif data.current_action == "extracting":
            if not data.extract_files_total:
                return None
            done, total = data.extract_files_done, data.extract_files_total
        elif data.current_action == "moving":
            if not data.import_files_total:
                return None
            done, total = data.import_files_done, data.import_files_total
        else:
            return None
        return round(100 * done / total, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        data = self.coordinator.data
        return {
            "download_bytes_done": data.current_archive_bytes_done,
            "download_bytes_total": data.current_archive_bytes_total,
            "extract_files_done": data.extract_files_done,
            "extract_files_total": data.extract_files_total,
            "move_files_done": data.import_files_done,
            "move_files_total": data.import_files_total,
        }
