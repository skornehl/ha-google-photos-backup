"""Tests for the curl_session_expired repair issue and its fix flow
(repairs.py) against a real Home Assistant issue registry/config entry -
not hand-mocked, since issue_registry.async_get(hass) is the actual thing
worth verifying (see test_curl_session_download.py for the mocked-issue-
registry tests that focus on *when* the issue is raised/cleared instead).
"""
from __future__ import annotations

from homeassistant import data_entry_flow
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.base import SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import (
    TakeoutBackend,
    _curl_session_issue_id,
)
from custom_components.google_photos_backup.const import CONF_TAKEOUT_CURL_SESSION, DOMAIN
from custom_components.google_photos_backup.repairs import (
    CurlSessionExpiredRepairFlow,
    async_create_fix_flow,
)


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, title="Google Photos Backup (Takeout)", data={}, options={}
    )
    entry.add_to_hass(hass)
    return entry


async def test_raise_issue_creates_a_real_registry_entry(hass):
    entry = _entry(hass)
    backend = TakeoutBackend(hass, entry, SyncStateStore({}))

    backend._raise_curl_session_expired_issue()

    issue = ir.async_get(hass).async_get_issue(DOMAIN, _curl_session_issue_id(entry.entry_id))
    assert issue is not None
    assert issue.is_fixable is True
    assert issue.translation_key == "curl_session_expired"
    assert issue.data == {"entry_id": entry.entry_id}


async def test_fix_flow_saves_the_new_session_and_returns_it(hass):
    entry = _entry(hass)
    flow = await async_create_fix_flow(
        hass, _curl_session_issue_id(entry.entry_id), {"entry_id": entry.entry_id}
    )
    assert isinstance(flow, CurlSessionExpiredRepairFlow)
    flow.hass = hass

    form = await flow.async_step_init()
    assert form["step_id"] == "confirm"

    new_session = "curl 'https://example/download/takeout-x-1-001.zip' -H 'cookie: SID=fresh'"
    result = await flow.async_step_confirm({CONF_TAKEOUT_CURL_SESSION: new_session})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TAKEOUT_CURL_SESSION] == new_session


async def test_fix_flow_has_nothing_to_fix_if_entry_is_gone(hass):
    try:
        await async_create_fix_flow(hass, "curl_session_expired_missing", {"entry_id": "missing"})
    except ValueError as err:
        assert "missing" in str(err) or "No config entry" in str(err)
    else:
        raise AssertionError("expected ValueError for a nonexistent entry_id")
