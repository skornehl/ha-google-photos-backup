"""Live controls for a Google Photos Backup config entry."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import GooglePhotosBackupCoordinator
from .entity import GooglePhotosBackupEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: GooglePhotosBackupCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([PauseDownloadSwitch(coordinator, entry)])


class PauseDownloadSwitch(GooglePhotosBackupEntity, SwitchEntity):
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

    _attr_icon = "mdi:pause-circle-outline"

    def __init__(self, coordinator: GooglePhotosBackupCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "pause_download")

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
