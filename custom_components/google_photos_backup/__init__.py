"""The Google Photos Backup integration."""
from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .backends.library_api import LibraryApiBackend
from .const import (
    CONFIG_ENTRY_VERSION,
    DOMAIN,
    PLATFORMS,
    SERVICE_BACKUP_NOW,
    SERVICE_START_PICKER_SESSION,
)
from .coordinator import GooglePhotosBackupCoordinator

_LOGGER = logging.getLogger(__name__)

SERVICE_ENTRY_SCHEMA = vol.Schema({vol.Required("config_entry_id"): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = GooglePhotosBackupCoordinator(hass, entry)
    try:
        await coordinator.async_setup()
    except ValueError as err:
        # Backend validation failures (missing binary, bad path, ...) are
        # configuration problems, not transient - surface them plainly
        # instead of endlessly retrying async_setup_entry.
        raise ConfigEntryNotReady(str(err)) from err

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _async_register_services(hass)
    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Bring an existing config entry up to the current schema.

    Currently a no-op: the schema is still at VERSION 1 (see
    config_flow.GooglePhotosBackupFlowHandler) and every change so far has
    been additive - new options are read through `.get()` with defaults,
    so older entries keep working untouched.

    It exists anyway because the moment a *non*-additive change is needed
    (renaming a key, changing a value's shape), there has to be somewhere
    to put the migration - and by then it's too late to add the mechanism,
    since entries written by the older version are already on disk. For
    this integration that's worse than usual: a broken entry also loses
    the persisted sync state (processed_ids / processed_hashes /
    processed_archives), which means re-downloading the entire library.

    Returning False on an unknown (newer) version is deliberate: that
    happens when a user downgrades the integration, and refusing to load
    is far safer than letting old code write a schema it doesn't
    understand.
    """
    if entry.version == CONFIG_ENTRY_VERSION:
        return True

    if entry.version > CONFIG_ENTRY_VERSION:
        _LOGGER.error(
            "Config-Entry hat Version %s, diese Version der Integration kennt "
            "nur bis %s - vermutlich wurde die Integration herabgestuft. "
            "Entry wird nicht geladen, um die gespeicherten Daten nicht zu "
            "beschädigen.",
            entry.version,
            CONFIG_ENTRY_VERSION,
        )
        return False

    _LOGGER.error(
        "Kein Migrationspfad von Config-Entry-Version %s auf %s",
        entry.version,
        CONFIG_ENTRY_VERSION,
    )
    return False


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: GooglePhotosBackupCoordinator | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if coordinator is not None and coordinator.backend is not None:
        # Stop anything the backend might have in flight (e.g. rclone's
        # subprocess) before tearing down the entry, so unload/reload
        # never leaves it running detached from HA - see issue #6.
        await coordinator.backend.async_terminate()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
            for service in (SERVICE_BACKUP_NOW, SERVICE_START_PICKER_SESSION):
                hass.services.async_remove(DOMAIN, service)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_BACKUP_NOW):
        return

    async def _handle_backup_now(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data["config_entry_id"])
        await coordinator.async_request_refresh()

    async def _handle_start_picker_session(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass, call.data["config_entry_id"])
        # isinstance rather than comparing entry.data[CONF_BACKEND] to a
        # string: async_start_picker_session() only exists on this one
        # backend class, so checking the class itself is both the honest
        # precondition and something the type checker can narrow on (the
        # string comparison needed a `# type: ignore[union-attr]` to
        # silence exactly the check that would have caught a mismatch).
        backend = coordinator.backend
        if not isinstance(backend, LibraryApiBackend):
            raise HomeAssistantError(
                "start_picker_session ist nur für das library_api-Backend verfügbar"
            )
        picker_uri = await backend.async_start_picker_session()
        persistent_notification.async_create(
            hass,
            f"Öffne diesen Link und wähle die zu sichernden Fotos aus:\n\n{picker_uri}",
            title="Google Photos Backup: Auswahl erforderlich",
            notification_id=f"{DOMAIN}_picker_{coordinator.entry.entry_id}",
        )

    hass.services.async_register(
        DOMAIN, SERVICE_BACKUP_NOW, _handle_backup_now, schema=SERVICE_ENTRY_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_START_PICKER_SESSION,
        _handle_start_picker_session,
        schema=SERVICE_ENTRY_SCHEMA,
    )


def _get_coordinator(hass: HomeAssistant, entry_id: str) -> GooglePhotosBackupCoordinator:
    try:
        return hass.data[DOMAIN][entry_id]
    except KeyError as err:
        raise HomeAssistantError(
            f"Kein Google Photos Backup Config-Entry mit ID {entry_id} gefunden"
        ) from err
