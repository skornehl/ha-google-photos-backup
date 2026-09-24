"""Repair flows for the Google Photos Backup integration.

Currently one issue: the captured browser session used by
backends/takeout_backend.py's cURL/PowerShell download expires after
about an hour (it's a real Google session cookie, not a token this
integration controls). That used to only show up as a line on the
last_error sensor - easy to miss on an unattended schedule. The fix flow
below lets a fresh cURL/PowerShell command be pasted directly from
Settings -> System -> Repairs, the same field the options flow offers.
"""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.repairs import RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .config_flow import _MULTILINE_TEXT
from .const import CONF_TAKEOUT_CURL_SESSION


class CurlSessionExpiredRepairFlow(RepairsFlow):
    """Single-step flow: paste a new session, save it to the entry's
    options exactly like the options flow would, done."""

    def __init__(self, entry: config_entries.ConfigEntry) -> None:
        self._entry = entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            new_options = dict(self._entry.options)
            new_options[CONF_TAKEOUT_CURL_SESSION] = user_input[CONF_TAKEOUT_CURL_SESSION]
            self.hass.config_entries.async_update_entry(self._entry, options=new_options)
            # The options-update listener (see __init__._async_update_listener)
            # reloads the entry, which is enough to pick up the new session
            # on the next run - no separate "verify now" step needed here.
            return self.async_create_entry(data={})

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({vol.Required(CONF_TAKEOUT_CURL_SESSION): _MULTILINE_TEXT}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, Any] | None
) -> RepairsFlow:
    entry_id = (data or {}).get("entry_id")
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None:
        # Config entry was removed after the issue was raised - nothing
        # sensible to fix any more. Home Assistant's repairs UI already
        # guards against this in practice (the issue only shows while its
        # entry exists), but async_create_fix_flow still has to return
        # *something* rather than raise into the repairs platform.
        raise ValueError(f"No config entry for repair issue {issue_id}")
    return CurlSessionExpiredRepairFlow(entry)
