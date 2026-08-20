"""Config flow: pick a backend, then collect backend-specific options.

library_api is the only branch that needs OAuth2 (via
config_entry_oauth2_flow.AbstractOAuth2FlowHandler / Application
Credentials) - rclone and takeout create their config entry directly from
a plain form, no cloud auth involved.
"""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_entry_oauth2_flow, selector

from .const import (
    BACKEND_LIBRARY_API,
    BACKEND_RCLONE,
    BACKEND_TAKEOUT,
    BACKENDS,
    CONF_BACKEND,
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_RCLONE_BINARY,
    CONF_RCLONE_CONFIG_PATH,
    CONF_RCLONE_REMOTE_NAME,
    CONF_RCLONE_SOURCE_PATH,
    CONF_SYNC_INTERVAL_MINUTES,
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_RCLONE_BINARY,
    DEFAULT_RCLONE_SOURCE_PATH,
    DEFAULT_SYNC_INTERVAL_MINUTES,
    DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT,
    DOMAIN,
    MIN_SYNC_INTERVAL_MINUTES,
    OAUTH2_SCOPES,
)

_LOGGER = logging.getLogger(__name__)


def _common_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_TARGET_DIR, default=defaults.get(CONF_TARGET_DIR, "/media/google_photos")): str,
            vol.Required(
                CONF_SYNC_INTERVAL_MINUTES,
                default=defaults.get(CONF_SYNC_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES),
            ): vol.All(vol.Coerce(int), vol.Range(min=MIN_SYNC_INTERVAL_MINUTES)),
        }
    )


def _bandwidth_schema(defaults: dict[str, Any] | None = None) -> dict[Any, Any]:
    """Bandwidth limit field, added to backends that transfer bytes
    themselves (library_api, rclone). Not part of _common_schema() because
    the takeout backend never does network I/O - see const.py."""
    defaults = defaults or {}
    return {
        vol.Optional(
            CONF_BANDWIDTH_LIMIT_KBPS,
            default=defaults.get(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS),
        ): vol.All(vol.Coerce(int), vol.Range(min=0))
    }


class GooglePhotosBackupFlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handles the whole `google_photos_backup` config flow.

    Subclasses AbstractOAuth2FlowHandler (which already is a ConfigFlow)
    unconditionally, even though the rclone/takeout branches never touch
    OAuth - that's fine, those branches just never call into the
    inherited pick_implementation/auth steps.
    """

    VERSION = 1
    DOMAIN = DOMAIN

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(OAUTH2_SCOPES), "access_type": "offline", "prompt": "consent"}

    # -- step 1: backend selection -------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._data[CONF_BACKEND] = user_input[CONF_BACKEND]
            if user_input[CONF_BACKEND] == BACKEND_LIBRARY_API:
                return await self.async_step_pick_implementation()
            if user_input[CONF_BACKEND] == BACKEND_RCLONE:
                return await self.async_step_rclone()
            return await self.async_step_takeout()

        schema = vol.Schema(
            {
                vol.Required(CONF_BACKEND, default=BACKEND_TAKEOUT): selector.selector(
                    {"select": {"options": list(BACKENDS), "translation_key": CONF_BACKEND}}
                )
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema)

    # -- library_api: OAuth2, then target_dir/interval -----------------------

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> FlowResult:
        # Called by AbstractOAuth2FlowHandler once the OAuth dance succeeds.
        # `data` holds {"auth_implementation": ..., "token": {...}} - stash
        # it and ask for the remaining common fields before finalizing.
        self._data.update(data)
        return await self.async_step_library_api_options()

    async def async_step_library_api_options(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(
                title="Google Photos Backup (Picker/Library API)", data=self._data
            )
        return self.async_show_form(
            step_id="library_api_options",
            data_schema=_common_schema().extend(_bandwidth_schema()),
        )

    # -- rclone ---------------------------------------------------------------

    async def async_step_rclone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Google Photos Backup (rclone)", data=self._data)

        schema = _common_schema().extend(
            {
                vol.Required(CONF_RCLONE_REMOTE_NAME): str,
                vol.Optional(CONF_RCLONE_BINARY, default=DEFAULT_RCLONE_BINARY): str,
                vol.Optional(CONF_RCLONE_CONFIG_PATH, default=""): str,
                vol.Optional(CONF_RCLONE_SOURCE_PATH, default=DEFAULT_RCLONE_SOURCE_PATH): str,
                **_bandwidth_schema(),
            }
        )
        return self.async_show_form(step_id="rclone", data_schema=schema, errors=errors)

    # -- takeout ----------------------------------------------------------------

    async def async_step_takeout(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="Google Photos Backup (Takeout)", data=self._data)

        schema = _common_schema().extend(
            {
                vol.Required(CONF_TAKEOUT_WATCH_DIR, default="/media/google_takeout_incoming"): str,
                vol.Optional(
                    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
                    default=DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT,
                ): bool,
            }
        )
        return self.async_show_form(step_id="takeout", data_schema=schema)

    @staticmethod
    @config_entries.callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "GooglePhotosBackupOptionsFlow":
        return GooglePhotosBackupOptionsFlow()


class GooglePhotosBackupOptionsFlow(config_entries.OptionsFlow):
    """Lets the user change the sync interval and bandwidth limit without
    re-running setup.

    No custom __init__/self.config_entry assignment - current HA populates
    `self.config_entry` on the instance automatically; doing it manually is
    deprecated.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_SYNC_INTERVAL_MINUTES,
            self.config_entry.data.get(CONF_SYNC_INTERVAL_MINUTES, DEFAULT_SYNC_INTERVAL_MINUTES),
        )
        schema_dict: dict[Any, Any] = {
            vol.Required(CONF_SYNC_INTERVAL_MINUTES, default=current_interval): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SYNC_INTERVAL_MINUTES)
            )
        }
        # takeout never transfers bytes itself (see const.py) - no
        # bandwidth field to offer for that backend.
        if self.config_entry.data.get(CONF_BACKEND) != BACKEND_TAKEOUT:
            schema_dict.update(
                _bandwidth_schema(
                    {
                        CONF_BANDWIDTH_LIMIT_KBPS: self.config_entry.options.get(
                            CONF_BANDWIDTH_LIMIT_KBPS,
                            self.config_entry.data.get(
                                CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS
                            ),
                        )
                    }
                )
            )
        return self.async_show_form(step_id="init", data_schema=vol.Schema(schema_dict))
