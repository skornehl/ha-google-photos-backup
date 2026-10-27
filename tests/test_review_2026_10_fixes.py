"""Regression tests for the findings of the full code review (2026-10).

  - A file that failed to import was only logged: the archive was marked
    processed anyway (and deleted with takeout_delete_after_import), so the
    file was never retried.
  - Unload/reload during an import left the executor thread running next to
    the reloaded entry's own run.
  - The "session expired" notification was raised on every run and never
    withdrawn by the repair or options flow.
  - The options flow could not clear the captured session.
  - Date folders were derived from UTC instead of local time.
  - The redirect URL placeholder was empty on HA without
    async_get_redirect_uri.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import data_entry_flow
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.google_photos_backup import application_credentials
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.fsutil import dest_dir_for_date
from custom_components.google_photos_backup.backends.takeout_backend import (
    ImportStopped,
    IncompleteImport,
    TakeoutBackend,
    _curl_session_issue_id,
)
from custom_components.google_photos_backup.const import (
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    CONF_TAKEOUT_CURL_SESSION,
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DOMAIN,
)
from custom_components.google_photos_backup.repairs import async_create_fix_flow

SESSION = (
    "curl 'https://takeout-download.usercontent.google.com/download/"
    "takeout-x-1-001.zip' -H 'cookie: SID=fresh'"
)


def _make_backend(tmp_path: Path, options: dict | None = None) -> tuple[TakeoutBackend, Path]:
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    entry = SimpleNamespace(
        entry_id="e",
        title="t",
        data={CONF_TAKEOUT_WATCH_DIR: str(watch_dir), CONF_TARGET_DIR: str(target_dir)},
        options=options or {},
    )
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    hass.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *a: fn(*a))
    return TakeoutBackend(hass, entry, SyncStateStore({})), watch_dir


def _write_archive(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)


# -- failed files keep the archive -------------------------------------------


async def test_failed_file_keeps_the_archive_unprocessed_and_on_disk(tmp_path: Path):
    backend, watch_dir = _make_backend(tmp_path, {CONF_TAKEOUT_DELETE_AFTER_IMPORT: True})
    archive = watch_dir / "takeout-x-1-001.zip"
    _write_archive(archive, {"T/a.jpg": "a", "T/b.jpg": "b"})

    real_import = TakeoutBackend._import_media_file

    def flaky(self, media_file, *args, **kwargs):
        if media_file.name == "a.jpg":
            raise OSError(28, "No space left on device")
        return real_import(self, media_file, *args, **kwargs)

    with patch.object(TakeoutBackend, "_import_media_file", flaky):
        stats = await backend.async_run_backup()

    assert backend.state.get("processed_archives", []) == []
    assert archive.exists(), "a partly imported archive must not be deleted"
    assert stats.archives_done == 0
    assert any("a.jpg: import failed" in e for e in stats.errors)
    assert any("1 of 2 files could not be imported" in e for e in stats.errors)
    # The file that did make it is recorded, so the retry skips it.
    assert len(backend.state.processed_hashes) == 1


def test_import_archive_raises_incomplete_import(tmp_path: Path):
    backend, watch_dir = _make_backend(tmp_path)
    archive = watch_dir / "takeout-x-1-001.zip"
    _write_archive(archive, {"T/a.jpg": "a"})

    with patch.object(TakeoutBackend, "_import_media_file", side_effect=RuntimeError("boom")):
        with pytest.raises(IncompleteImport):
            backend._import_archive(archive, str(tmp_path / "target"), BackupStats())


# -- unload stops the import ---------------------------------------------------


def test_import_refuses_to_start_after_terminate(tmp_path: Path):
    backend, watch_dir = _make_backend(tmp_path)
    archive = watch_dir / "takeout-x-1-001.zip"
    _write_archive(archive, {"T/a.jpg": "a"})
    backend._stop_requested.set()

    with pytest.raises(ImportStopped):
        backend._import_archive(archive, str(tmp_path / "target"), BackupStats())
    assert not any((tmp_path / "target").rglob("a.jpg"))
    assert backend._import_idle.is_set()


def test_running_import_stops_between_files(tmp_path: Path):
    backend, watch_dir = _make_backend(tmp_path)
    archive = watch_dir / "takeout-x-1-001.zip"
    _write_archive(archive, {"T/a.jpg": "a", "T/b.jpg": "b", "T/c.jpg": "c"})
    imported: list[str] = []

    def stop_after_first(self, media_file, *args, **kwargs):
        imported.append(media_file.name)
        self._stop_requested.set()

    with patch.object(TakeoutBackend, "_import_media_file", stop_after_first):
        with pytest.raises(ImportStopped):
            backend._import_archive(archive, str(tmp_path / "target"), BackupStats())
    assert len(imported) == 1
    assert backend._import_idle.is_set()


async def test_terminate_returns_when_no_import_is_running(tmp_path: Path):
    backend, _ = _make_backend(tmp_path)
    await backend.async_terminate()
    assert backend._stop_requested.is_set()


# -- session-expired notification ---------------------------------------------


def _takeout_entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Google Photos Backup (Takeout)",
        data={CONF_BACKEND: BACKEND_TAKEOUT, CONF_TAKEOUT_CURL_SESSION: SESSION},
        options={},
    )
    entry.add_to_hass(hass)
    return entry


async def test_notification_is_raised_once_per_expiry(hass):
    entry = _takeout_entry(hass)
    backend = TakeoutBackend(hass, entry, SyncStateStore({}))

    with patch("homeassistant.components.persistent_notification.async_create") as create:
        backend._raise_curl_session_expired_issue()
        backend._raise_curl_session_expired_issue()

    create.assert_called_once()


async def test_fix_flow_dismisses_the_notification(hass):
    entry = _takeout_entry(hass)
    flow = await async_create_fix_flow(
        hass, _curl_session_issue_id(entry.entry_id), {"entry_id": entry.entry_id}
    )
    flow.hass = hass

    with patch("homeassistant.components.persistent_notification.async_dismiss") as dismiss:
        result = await flow.async_step_confirm({CONF_TAKEOUT_CURL_SESSION: SESSION})

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    dismiss.assert_called_once_with(hass, f"{DOMAIN}_curl_{entry.entry_id}")


# -- options flow ---------------------------------------------------------------


async def test_options_flow_can_clear_the_session(hass):
    entry = _takeout_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    # A cleared optional field is not sent at all.
    with patch("homeassistant.components.persistent_notification.async_dismiss") as dismiss:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"sync_interval_minutes": 60}
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TAKEOUT_CURL_SESSION] == ""
    dismiss.assert_called_once()


async def test_options_flow_keeps_an_unchanged_session(hass):
    entry = _takeout_entry(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch("homeassistant.components.persistent_notification.async_dismiss") as dismiss:
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"sync_interval_minutes": 60, CONF_TAKEOUT_CURL_SESSION: SESSION},
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_TAKEOUT_CURL_SESSION] == SESSION
    dismiss.assert_not_called()


# -- date folders in local time ------------------------------------------------


async def test_date_folder_uses_local_time(hass):
    await hass.config.async_set_time_zone("Europe/Berlin")
    # 00:30 on 1 March in Berlin is still 28 February in UTC.
    taken_at = datetime(2026, 2, 28, 23, 30, tzinfo=timezone.utc)
    assert dest_dir_for_date("/media/photos", taken_at) == Path("/media/photos/2026/2026-03")


# -- redirect URL fallback -------------------------------------------------------


async def test_redirect_url_falls_back_to_my_home_assistant(hass):
    with patch.object(
        application_credentials.config_entry_oauth2_flow,
        "async_get_redirect_uri",
        create=True,
        new=None,
    ):
        placeholders = await application_credentials.async_get_description_placeholders(hass)
    assert placeholders["redirect_url"] == "https://my.home-assistant.io/redirect/oauth"
