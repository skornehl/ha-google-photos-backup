"""library_api backend: Library API (app-created items only) + Picker API.

IMPORTANT - read before trusting this backend for a real backup:

Since 2025-03-31 Google restricts `mediaItems.list` / `mediaItems.search`
to media created by the *calling app* (scope
`photoslibrary.readonly.appcreateddata`). For virtually every existing
Google Photos library, that means those endpoints return nothing useful -
they cannot see photos the user took with their phone before this
integration ever existed.

The only Google-sanctioned way left to read arbitrary, pre-existing
library content is the **Picker API**: the user is sent a `pickerUri`,
opens it in a browser, manually selects photos/albums in Google's own UI,
and only then can this integration fetch exactly those items. There is no
API to enumerate "everything" or "everything since X" unattended - Google
made that a deliberate, user-consent-gated flow. Practically this means:

  - `async_run_backup()` can only ever finish a picker session the user
    already started via the `google_photos_backup.start_picker_session`
    service (see __init__.py) - it never silently pulls new photos on its
    own timer, unlike the rclone/takeout backends.
  - The scheduled sync interval mostly just re-checks "did the user finish
    picking yet" and does the (usually empty) app-created-only search.

See README.md for why the takeout backend is the recommended default for
backing up an existing library.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from ..const import (
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_PICKER_SESSION_EXPIRES,
    CONF_PICKER_SESSION_ID,
    CONF_PICKER_SESSION_URI,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    LIBRARY_API_BASE,
    PICKER_API_BASE,
)
from .base import BackupBackend, BackupStats, SyncStateStore
from .fsutil import dest_dir_for_date, ensure_target_dir, unique_destination
from .throttle import throttled_read

_LOGGER = logging.getLogger(__name__)


class LibraryApiBackend(BackupBackend):
    """OAuth2-based backend combining the Library API and Picker API."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
    ) -> None:
        super().__init__(hass, entry, state)
        self._oauth = oauth_session

    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)
        await self._oauth.async_ensure_token_valid()

    async def async_start_picker_session(self) -> str:
        """Create a new Picker session, persist it, return the pickerUri."""
        resp = await self._oauth.async_request("POST", f"{PICKER_API_BASE}/sessions", json={})
        resp.raise_for_status()
        payload = await resp.json()
        self.state.set(CONF_PICKER_SESSION_ID, payload["id"])
        self.state.set(CONF_PICKER_SESSION_URI, payload["pickerUri"])
        self.state.set(CONF_PICKER_SESSION_EXPIRES, payload.get("expireTime"))
        _LOGGER.info(
            "Google Photos Picker-Session gestartet - Nutzer muss %s "
            "öffnen und Fotos auswählen",
            payload["pickerUri"],
        )
        return payload["pickerUri"]

    async def async_run_backup(self) -> BackupStats:
        """Downloads run strictly sequentially, one item at a time.

        Deliberate, not an oversight (see issue #20): concurrency here
        would need to share a single bandwidth budget across workers to
        keep the bandwidth_limit_kbps option meaningful (throttle.py's
        pacer is per-download), and would multiply the request rate
        against an API whose rate limits aren't documented in a way we
        can safely tune against. Sequential is slower for very large
        picker selections but predictable, and it keeps the throttle
        semantics honest. Revisit with an explicit semaphore + shared
        pacer if that ever becomes the actual bottleneck.
        """
        stats = BackupStats()
        await self._finish_pending_picker_session(stats)
        await self._sync_app_created_items(stats)
        return stats

    # -- Picker API --------------------------------------------------------

    async def _finish_pending_picker_session(self, stats: BackupStats) -> None:
        session_id = self.state.get(CONF_PICKER_SESSION_ID)
        if not session_id:
            return

        # Listing (session status + paginated mediaItems) is wrapped like
        # _sync_app_created_items() below - a single transient error (429,
        # 5xx, network blip) here must degrade to a logged error for this
        # run, not abort the whole coordinator update the way an unguarded
        # raise_for_status() would (individual item downloads already
        # degrade gracefully via _download_picker_item()'s own try/except;
        # this closes the same gap for the session-management calls around
        # them).
        try:
            resp = await self._oauth.async_request(
                "GET", f"{PICKER_API_BASE}/sessions/{session_id}"
            )
            if resp.status == 404:
                _LOGGER.warning("Picker-Session %s nicht mehr gültig, verwerfe sie", session_id)
                self._clear_picker_session()
                return
            resp.raise_for_status()
            session = await resp.json()
            if not session.get("mediaItemsSet"):
                _LOGGER.debug("Picker-Session %s: Nutzer hat noch nicht abgeschlossen", session_id)
                return

            target_dir = self.entry.data[CONF_TARGET_DIR]
            page_token: str | None = None
            while True:
                params = {"sessionId": session_id, "pageSize": 100}
                if page_token:
                    params["pageToken"] = page_token
                resp = await self._oauth.async_request(
                    "GET", f"{PICKER_API_BASE}/mediaItems", params=params
                )
                resp.raise_for_status()
                payload = await resp.json()
                for item in payload.get("mediaItems", []):
                    await self._download_picker_item(item, target_dir, stats)
                page_token = payload.get("nextPageToken")
                if not page_token:
                    break
        except Exception as err:  # noqa: BLE001
            stats.errors.append(f"Picker-Session-Verarbeitung fehlgeschlagen: {err}")
            return

        try:
            await self._oauth.async_request(
                "DELETE", f"{PICKER_API_BASE}/sessions/{session_id}"
            )
            self._clear_picker_session()
            _LOGGER.info("Picker-Session %s abgeschlossen und aufgeräumt", session_id)
        except Exception as err:  # noqa: BLE001
            # All items in this session are already downloaded and recorded
            # in processed_ids by this point - losing the DELETE just means
            # the next run re-checks the same session (Google may 404 it by
            # then, or return the same mediaItemsSet again, which is
            # harmless: every item gets skipped via processed_ids). Not
            # worth treating as a bigger failure than it is.
            stats.errors.append(
                f"Picker-Session {session_id} konnte nicht aufgeräumt werden: {err}"
            )

    def _clear_picker_session(self) -> None:
        self.state.set(CONF_PICKER_SESSION_ID, None)
        self.state.set(CONF_PICKER_SESSION_URI, None)
        self.state.set(CONF_PICKER_SESSION_EXPIRES, None)

    async def _download_picker_item(
        self, item: dict[str, Any], target_dir: str, stats: BackupStats
    ) -> None:
        item_id = item.get("id")
        if not item_id:
            # No id means we can't record it in processed_ids, so it would
            # be re-downloaded on every single run forever. Skip loudly
            # rather than silently accumulating duplicates.
            stats.errors.append("Picker-Item ohne id in der Antwort - übersprungen")
            return

        processed_ids: list[str] = self.state.get("processed_ids", [])
        if item_id in processed_ids:
            stats.files_skipped += 1
            return

        media_file = item.get("mediaFile", {})
        base_url = media_file.get("baseUrl")
        filename = media_file.get("filename", f"{item_id}.jpg")
        mime_type = media_file.get("mimeType", "")
        if not base_url:
            stats.errors.append(f"{filename}: keine baseUrl in Picker-Antwort")
            return

        suffix = "=dv" if mime_type.startswith("video/") else "=d"
        create_time = item.get("createTime")
        try:
            taken_at = datetime.fromisoformat(create_time.replace("Z", "+00:00")) if create_time else datetime.now(timezone.utc)
        except ValueError:
            taken_at = datetime.now(timezone.utc)

        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        try:
            resp = await self._oauth.async_request("GET", base_url + suffix)
            resp.raise_for_status()
            raw = await throttled_read(resp, limit_kbps)
        except Exception as err:  # noqa: BLE001 - surfaced to the user via sensor
            stats.errors.append(f"{filename}: Download fehlgeschlagen ({err})")
            return

        def _write() -> int:
            dest_dir = dest_dir_for_date(target_dir, taken_at)
            dest = unique_destination(dest_dir, filename)
            dest.write_bytes(raw)
            ts = taken_at.timestamp()
            os.utime(dest, (ts, ts))
            return len(raw)

        size = await self.hass.async_add_executor_job(_write)
        processed_ids.append(item_id)
        self.state.set("processed_ids", processed_ids)
        stats.files_downloaded += 1
        stats.bytes_downloaded += size

    # -- Library API (app-created items only) -------------------------------

    async def _sync_app_created_items(self, stats: BackupStats) -> None:
        """Best-effort sync of items visible under the appcreateddata scope.

        For most users this will simply return an empty list every run -
        see the module docstring. Kept because the spec asked for
        incremental Library API sync and it's harmless / free to run, and
        it does help the rare setup where another integration or script
        uploads into this same OAuth client's app-created album.
        """
        page_token = self.state.get("library_page_token")
        try:
            params = {"pageSize": 100}
            if page_token:
                params["pageToken"] = page_token
            resp = await self._oauth.async_request(
                "GET", f"{LIBRARY_API_BASE}/mediaItems", params=params
            )
            if resp.status == 403:
                _LOGGER.debug(
                    "Library API mediaItems.list: 403 (erwartet, falls keine "
                    "app-eigenen Medien vorhanden sind)"
                )
                return
            resp.raise_for_status()
            payload = await resp.json()
        except Exception as err:  # noqa: BLE001
            stats.errors.append(f"Library API Sync fehlgeschlagen: {err}")
            return

        target_dir = self.entry.data[CONF_TARGET_DIR]
        for item in payload.get("mediaItems", []):
            await self._download_picker_item(
                {
                    "id": item.get("id"),
                    "createTime": item.get("mediaMetadata", {}).get("creationTime"),
                    "mediaFile": {
                        "baseUrl": item.get("baseUrl"),
                        "filename": item.get("filename"),
                        "mimeType": item.get("mimeType"),
                    },
                },
                target_dir,
                stats,
            )
        self.state.set("library_page_token", payload.get("nextPageToken"))
