"""Small behavior pins for the shared entity base and the last_error sensor."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.google_photos_backup.sensor import LastErrorSensor
from custom_components.google_photos_backup.switch import PauseDownloadSwitch


def _entry() -> SimpleNamespace:
    return SimpleNamespace(entry_id="abc", title="Backup", data={"backend": "takeout"})


def test_last_error_is_none_rather_than_the_string_none_when_clean():
    coordinator = MagicMock()
    coordinator.data = SimpleNamespace(last_run_errors=[])

    assert LastErrorSensor(coordinator, _entry()).native_value is None


def test_pause_switch_keeps_its_unique_id_and_device_after_moving_to_the_base():
    """unique_id must not change - that would orphan the existing entity."""
    switch = PauseDownloadSwitch(MagicMock(), _entry())

    assert switch.unique_id == "abc_pause_download"
    assert switch.translation_key == "pause_download"
    assert switch.device_info["model"] == "takeout"
