"""Config flow: pick a backend, then collect backend-specific options.

library_api always needs OAuth2 (via
config_entry_oauth2_flow.AbstractOAuth2FlowHandler / Application
Credentials). rclone never does. takeout normally doesn't either - unless
the user opts into Drive sync, in which case it needs its own OAuth2 round
trip too, requesting the drive.readonly scope instead of the Photos scopes
(see `extra_authorize_data` below).
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .backends import scopes_for_backend
from .const import (
    BACKEND_LIBRARY_API,
    BACKEND_RCLONE,
    BACKEND_TAKEOUT,
    BACKENDS,
    CONF_BACKEND,
    CONFIG_ENTRY_VERSION,
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_RCLONE_BINARY,
    CONF_RCLONE_CONFIG_PATH,
    CONF_RCLONE_REMOTE_NAME,
    CONF_RCLONE_SOURCE_PATH,
    CONF_SYNC_INTERVAL_MINUTES,
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_DOWNLOAD_LINKS,
    CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    CONF_TAKEOUT_DRIVE_FOLDER_ID,
    CONF_TAKEOUT_DRIVE_SYNC,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_RCLONE_BINARY,
    DEFAULT_RCLONE_SOURCE_PATH,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT,
    DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    DEFAULT_TAKEOUT_DRIVE_FOLDER_ID,
    DEFAULT_TAKEOUT_DRIVE_SYNC,
    DOMAIN,
    MIN_SYNC_INTERVAL_MINUTES,
    OAUTH2_SCOPES,
)

_LOGGER = logging.getLogger(__name__)

_MULTILINE_TEXT = selector.selector({"text": {"multiline": True}})


def _common_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Fields every backend collects: target dir, sync interval, and a
    bandwidth limit (a no-op for whichever backend/path doesn't happen to
    transfer bytes at all, harmless to always offer)."""
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_TARGET_DIR, default=defaults.get(CONF_TARGET_DIR, "/media/google_photos")): str,
            vol.Required(
                CONF_SYNC_INTERVAL_MINUTES,
                default=defaults.get(CONF_SYNC_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SYNC_INTERVAL_MINUTES)),
            vol.Optional(
                CONF_BANDWIDTH_LIMIT_KBPS,
                default=defaults.get(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),
        }
    )


def _takeout_schema(*, drive_enabled: bool, defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    schema = _common_schema(defaults).extend(
        {
            vol.Required(
                CONF_TAKEOUT_WATCH_DIR,
                default=defaults.get(CONF_TAKEOUT_WATCH_DIR, "/media/google_takeout_incoming"),
            ): str,
            vol.Optional(
                CONF_TAKEOUT_DELETE_AFTER_IMPORT,
                default=defaults.get(CONF_TAKEOUT_DELETE_AFTER_IMPORT, DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT),
            ): bool,
            vol.Optional(
                CONF_TAKEOUT_DOWNLOAD_LINKS,
                default=defaults.get(CONF_TAKEOUT_DOWNLOAD_LINKS, ""),
            ): _MULTILINE_TEXT,
        }
    )
    if drive_enabled:
        schema = schema.extend(
            {
                vol.Optional(
                    CONF_TAKEOUT_DRIVE_FOLDER_ID,
                    default=defaults.get(CONF_TAKEOUT_DRIVE_FOLDER_ID, DEFAULT_TAKEOUT_DRIVE_FOLDER_ID),
                ): str,
                vol.Optional(
                    CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
                    default=defaults.get(
                        CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC, DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC
                    ),
                ): bool,
                vol.Optional(
                    CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
                    default=defaults.get(
                        CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY, DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY
                    ),
                ): bool,
            }
        )
    return schema


class GooglePhotosBackupFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handles the whole `google_photos_backup` config flow.

    Subclasses AbstractOAuth2FlowHandler (which already is a ConfigFlow)
    unconditionally, even though the rclone branch and the plain-takeout
    branch never touch OAuth - that's fine, those branches just never call
    into the inherited pick_implementation/auth steps.
    """

    VERSION = CONFIG_ENTRY_VERSION
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        # Which scopes to request is the backend's own business - each
        # backend class declares them via BackupBackend.oauth_scopes, so
        # adding an OAuth-using backend doesn't mean editing an if/elif
        # chain here. Falls back to the Photos scopes when the backend
        # isn't known yet (or doesn't declare any): the OAuth steps are
        # only ever reached from a branch that has already set
        # CONF_BACKEND, so this is a belt-and-braces default rather than
        # a real code path.
        scopes = scopes_for_backend(self._data.get(CONF_BACKEND)) or OAUTH2_SCOPES
        return {"scope": " ".join(scopes), "access_type": "offline", "prompt": "consent"}

    # -- step 1: backend selection -------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data[CONF_BACKEND] = user_input[CONF_BACKEND]
            if user_input[CONF_BACKEND] == BACKEND_LIBRARY_API:
                return await self.async_step_pick_implementation()
            if user_input[CONF_BACKEND] == BACKEND_RCLONE:
                return await self.async_step_rclone()
            return await self.async_step_takeout_drive_choice()

        schema = vol.Schema(
            {
                vol.Required(CONF_BACKEND, default=BACKEND_TAKEOUT): selector.selector(
                    {"select": {"options": list(BACKENDS), "translation_key": CONF_BACKEND}}
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # -- reauth: token refresh failed (revoked/expired grant), see
    # coordinator.py's ConfigEntryAuthFailed handling ------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        # Called by HA when a previous async_setup_entry/coordinator update
        # raised ConfigEntryAuthFailed for this entry. `entry_data` is the
        # failing entry's existing `.data` - carry forward CONF_BACKEND (and
        # CONF_TAKEOUT_DRIVE_SYNC, if set) so extra_authorize_data below
        # requests the same scope as originally, then just re-run the normal
        # OAuth dance; async_oauth_create_entry() detects the reauth source
        # and updates the existing entry instead of creating a new one.
        self._data[CONF_BACKEND] = entry_data.get(CONF_BACKEND)
        self._data[CONF_TAKEOUT_DRIVE_SYNC] = entry_data.get(CONF_TAKEOUT_DRIVE_SYNC, False)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation()

    # -- OAuth2 dispatch: library_api options, takeout+Drive options, or
    # reauth completion -------------------------------------------------------

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        # Called by AbstractOAuth2FlowHandler once the OAuth dance succeeds.
        # `data` holds {"auth_implementation": ..., "token": {...}} - stash
        # it and ask for the remaining fields before finalizing. Which
        # fields depends on which backend sent us through OAuth.
        self._data.update(data)

        if self.source == config_entries.SOURCE_REAUTH:
            existing_entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
            if existing_entry is None:
                # The entry was removed while the user was off completing
                # the Google sign-in - nothing left to write the new token
                # to, and dereferencing it here would raise AttributeError
                # mid-flow.
                return self.async_abort(reason="reauth_entry_missing")
            self.hass.config_entries.async_update_entry(existing_entry, data=self._data)
            await self.hass.config_entries.async_reload(existing_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        if self._data.get(CONF_BACKEND) == BACKEND_TAKEOUT:
            return await self.async_step_takeout_drive_options()
        return await self.async_step_library_api_options()

    async def async_step_library_api_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self._async_finalize_entry("Google Photos Backup (Picker/Library API)")
        return self.async_show_form(step_id="library_api_options", data_schema=_common_schema())

    # -- rclone ---------------------------------------------------------------

    async def async_step_rclone(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            return await self._async_finalize_entry("Google Photos Backup (rclone)")

        schema = _common_schema().extend(
            {
                vol.Required(CONF_RCLONE_REMOTE_NAME): str,
                vol.Optional(CONF_RCLONE_BINARY, default=DEFAULT_RCLONE_BINARY): str,
                vol.Optional(CONF_RCLONE_CONFIG_PATH, default=""): str,
                vol.Optional(CONF_RCLONE_SOURCE_PATH, default=DEFAULT_RCLONE_SOURCE_PATH): str,
            }
        )
        return self.async_show_form(step_id="rclone", data_schema=schema, errors=errors)

    # -- takeout ----------------------------------------------------------------

    async def async_step_takeout_drive_choice(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask up front whether to enable Drive sync, since that decides
        whether we route through OAuth at all - can't be decided later
        inside a plain form step."""
        if user_input is not None:
            self._data[CONF_TAKEOUT_DRIVE_SYNC] = user_input[CONF_TAKEOUT_DRIVE_SYNC]
            if user_input[CONF_TAKEOUT_DRIVE_SYNC]:
                return await self.async_step_pick_implementation()
            return await self.async_step_takeout()

        schema = vol.Schema(
            {vol.Required(CONF_TAKEOUT_DRIVE_SYNC, default=DEFAULT_TAKEOUT_DRIVE_SYNC): bool}
        )
        return self.async_show_form(step_id="takeout_drive_choice", data_schema=schema)

    async def async_step_takeout(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Plain-takeout path: no Drive sync, so no OAuth needed."""
        if user_input is not None:
            self._data.update(user_input)
            return await self._async_finalize_entry("Google Photos Backup (Takeout)")
        return self.async_show_form(
            step_id="takeout", data_schema=_takeout_schema(drive_enabled=False)
        )

    async def async_step_takeout_drive_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Takeout-with-Drive-sync path: reached after OAuth succeeds."""
        if user_input is not None:
            self._data.update(user_input)
            return await self._async_finalize_entry("Google Photos Backup (Takeout + Drive sync)")
        return self.async_show_form(
            step_id="takeout_drive_options", data_schema=_takeout_schema(drive_enabled=True)
        )

    async def _async_finalize_entry(self, title: str) -> ConfigFlowResult:
        """Guard against configuring the same backend + target directory
        twice (see issue #4) before actually creating the entry - called
        from every async_create_entry() call site above.

        There's no cheap, scope-free way to get a stable Google-account ID
        here (that would need an extra API call / the openid scope we
        don't otherwise need), so `target_dir` is what we dedupe on: two
        entries writing into the same local directory is the actually
        harmful case (concurrent, uncoordinated writers), not "same Google
        account twice" (a user may legitimately want two entries against
        the same account with different targets/backends).
        """
        unique_id = f"{self._data.get(CONF_BACKEND)}:{self._data.get(CONF_TARGET_DIR)}"
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=self._data)

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> GooglePhotosBackupOptionsFlow:
        return GooglePhotosBackupOptionsFlow()


class GooglePhotosBackupOptionsFlow(config_entries.OptionsFlow):
    """Lets the user change sync interval, bandwidth limit, and (for
    takeout) download links / Drive folder without re-running setup.

    Whether Drive sync itself is enabled is NOT editable here - toggling it
    on needs a fresh OAuth round trip, which an options flow can't do; to
    add Drive sync to an existing takeout instance, remove and re-add the
    integration with the toggle checked during setup instead.

    No custom __init__/self.config_entry assignment - current HA populates
    `self.config_entry` on the instance automatically; doing it manually is
    deprecated.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        def _current(key: str, default: Any) -> Any:
            return self.config_entry.options.get(key, self.config_entry.data.get(key, default))

        schema_dict: dict[Any, Any] = {
            vol.Required(
                CONF_SYNC_INTERVAL_MINUTES,
                default=_current(CONF_SYNC_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SYNC_INTERVAL_MINUTES)),
            vol.Optional(
                CONF_BANDWIDTH_LIMIT_KBPS,
                default=_current(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS),
            ): vol.All(vol.Coerce(int), vol.Range(min=0)),
        }
        if self.config_entry.data.get(CONF_BACKEND) == BACKEND_TAKEOUT:
            schema_dict[
                vol.Optional(
                    CONF_TAKEOUT_DOWNLOAD_LINKS, default=_current(CONF_TAKEOUT_DOWNLOAD_LINKS, "")
                )
            ] = _MULTILINE_TEXT
            if self.config_entry.data.get(CONF_TAKEOUT_DRIVE_SYNC):
                schema_dict[
                    vol.Optional(
                        CONF_TAKEOUT_DRIVE_FOLDER_ID,
                        default=_current(CONF_TAKEOUT_DRIVE_FOLDER_ID, DEFAULT_TAKEOUT_DRIVE_FOLDER_ID),
                    )
                ] = str
                schema_dict[
                    vol.Optional(
                        CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
                        default=_current(
                            CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC, DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC
                        ),
                    )
                ] = bool
                schema_dict[
                    vol.Optional(
                        CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
                        default=_current(
                            CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY, DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY
                        ),
                    )
                ] = bool

        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
