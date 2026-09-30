"""Regression test for issue #12: sensor keys sourced from const.py.

Pins the actual string values, since these double as translation_key /
unique_id suffixes and must keep matching strings.json's
`entity.sensor.*` keys - a future edit to one of these constants without
updating strings.json would silently break translations, and this test
is the tripwire for that.
"""
from __future__ import annotations

from custom_components.google_photos_backup.const import (
    ATTR_DOWNLOAD_ACTIVITY,
    ATTR_DOWNLOAD_PROGRESS_PERCENT,
    ATTR_EXTRACT_ACTIVITY,
    ATTR_EXTRACT_PROGRESS_PERCENT,
    ATTR_FILES_BACKED_UP,
    ATTR_FREE_SPACE,
    ATTR_LAST_ERROR,
    ATTR_LAST_SYNC,
)


def test_sensor_key_constants_match_strings_json_entity_keys():
    assert ATTR_LAST_SYNC == "last_sync"
    assert ATTR_FILES_BACKED_UP == "files_backed_up"
    assert ATTR_LAST_ERROR == "last_error"
    assert ATTR_FREE_SPACE == "free_space"
    assert ATTR_DOWNLOAD_ACTIVITY == "download_activity"
    assert ATTR_DOWNLOAD_PROGRESS_PERCENT == "download_progress_percent"
    assert ATTR_EXTRACT_ACTIVITY == "extract_activity"
    assert ATTR_EXTRACT_PROGRESS_PERCENT == "extract_progress_percent"
