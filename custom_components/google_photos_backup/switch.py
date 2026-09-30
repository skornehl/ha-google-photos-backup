"""Live controls for a Google Photos Backup config entry."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_BACKEND, DOMAIN
from .coordinator import GooglePhotosBackupCoordinator


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GooglePhotosBackupCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PauseDownloadSwitch(coordinator, entry)])


class PauseDownloadSwitch(CoordinatorEntity[GooglePhotosBackupCoordinator], SwitchEntity):
    """Pauses an in-progress (or not-yet-started) download.

    Turning this on doesn't cancel or abandon anything - it just makes the
    current/next download loop iteration block (see
    BackupBackend._wait_if_paused / throttled_stream_to_file's
    pause_event) until turned back off. Import of archives already fully
    downloaded keeps running while paused; only fetching *new* bytes over
    the network stops. Backed directly by coordinator.backend.
    download_resume rather than the coordinator's own polled data, so it
    reflects and controls the live state immediately rather than waiting
    for the next progress tick.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "pause_download"
    _attr_icon = "mdi:pause-circle-outline"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pause_download"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Google Photos Backup",
            model=entry.data.get(CONF_BACKEND),
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def is_on(self) -> bool:
        backend = self.coordinator.backend
        return backend is not None and backend.download_paused

    @property
    def available(self) -> bool:
        return self.coordinator.backend is not None

    async def async_turn_on(self, **kwargs: object) -> None:
        assert self.coordinator.backend is not None
        self.coordinator.backend.pause_downloads()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: object) -> None:
        assert self.coordinator.backend is not None
        self.coordinator.backend.resume_downloads()
        self.async_write_ha_state()
