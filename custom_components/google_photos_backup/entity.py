"""Base entity shared by every platform of a Google Photos Backup entry."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BACKEND, DOMAIN
from .coordinator import GooglePhotosBackupCoordinator


class GooglePhotosBackupEntity(CoordinatorEntity[GooglePhotosBackupCoordinator]):
    """Groups all of an entry's entities under one service device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry, key: str
    ) -> None:
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
