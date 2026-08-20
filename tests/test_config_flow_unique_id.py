"""Regression tests for duplicate-entry prevention (issue #4).

Exercises the rclone branch specifically since it needs no OAuth/
Application Credentials setup, keeping the test focused on the
unique_id/_abort_if_unique_id_configured() logic itself rather than the
OAuth machinery (already covered by test_coordinator_auth_failure.py and
will be covered by dedicated config-flow OAuth tests separately).
"""
from __future__ import annotations

from homeassistant import config_entries, data_entry_flow

from custom_components.google_photos_backup.const import (
    BACKEND_RCLONE,
    CONF_BACKEND,
    CONF_RCLONE_REMOTE_NAME,
    CONF_TARGET_DIR,
    DOMAIN,
)


async def _start_rclone_flow(hass, *, target_dir: str, remote_name: str = "gphotos"):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BACKEND: BACKEND_RCLONE}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TARGET_DIR: target_dir,
            "sync_interval_minutes": 60,
            "bandwidth_limit_kbps": 0,
            CONF_RCLONE_REMOTE_NAME: remote_name,
        },
    )


async def test_first_entry_is_created_successfully(hass):
    result = await _start_rclone_flow(hass, target_dir="/media/google_photos")
    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY


async def test_second_entry_with_same_backend_and_target_dir_is_aborted(hass):
    first = await _start_rclone_flow(hass, target_dir="/media/google_photos")
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    second = await _start_rclone_flow(
        hass, target_dir="/media/google_photos", remote_name="a-different-remote"
    )
    assert second["type"] == data_entry_flow.FlowResultType.ABORT
    assert second["reason"] == "already_configured"


async def test_same_backend_different_target_dir_is_allowed(hass):
    """Two entries for the *same* backend are fine as long as they don't
    write into the same directory - that's the actually harmful case, not
    "configured twice" in the abstract."""
    first = await _start_rclone_flow(hass, target_dir="/media/google_photos")
    assert first["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

    second = await _start_rclone_flow(hass, target_dir="/media/google_photos_2")
    assert second["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
