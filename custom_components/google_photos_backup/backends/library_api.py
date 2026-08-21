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

import asyncio
import logging
import os
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from ..const import (
    BASEURL_MAX_AGE_SECONDS,
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_DOWNLOAD_CONCURRENCY,
    CONF_PICKER_SESSION_EXPIRES,
    CONF_PICKER_SESSION_ID,
    CONF_PICKER_SESSION_URI,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_DOWNLOAD_CONCURRENCY,
    DOWNLOAD_TIMEOUT,
    LIBRARY_API_BASE,
    MAX_DOWNLOAD_CONCURRENCY,
    OAUTH2_SCOPES,
    PICKER_API_BASE,
)
from .base import BackupBackend, BackupStats, SyncStateStore
from .fsutil import dest_dir_for_date, ensure_target_dir, unique_destination
from .throttle import BandwidthPacer, throttled_stream_to_file

_LOGGER = logging.getLogger(__name__)


class LibraryApiBackend(BackupBackend):
    """OAuth2-based backend combining the Library API and Picker API."""

    oauth_scopes = OAUTH2_SCOPES

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
        oauth_session: config_entry_oauth2_flow.OAuth2Session,
        on_progress: Callable[[BackupStats], None] | None = None,
    ) -> None:
        super().__init__(hass, entry, state, on_progress)
        self._oauth = oauth_session
        #: Destinations claimed by an in-flight download. See
        #: fsutil.unique_destination() for why existence alone isn't
        #: enough while downloads overlap.
        self._reserved_destinations: set[Path] = set()
        self._reserve_lock = asyncio.Lock()

    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)
        await self._oauth.async_ensure_token_valid()

    async def async_start_picker_session(self) -> str:
        """Create a new Picker session, persist it, return the pickerUri."""
        resp = await self._oauth.async_request("POST", f"{PICKER_API_BASE}/sessions", json={})
        resp.raise_for_status()
        payload = await resp.json()
        # resp.json() is Any; pin the two fields we promise to callers so
        # the declared -> str return type actually means something.
        session_id = str(payload["id"])
        picker_uri = str(payload["pickerUri"])
        self.state.set(CONF_PICKER_SESSION_ID, session_id)
        self.state.set(CONF_PICKER_SESSION_URI, picker_uri)
        self.state.set(CONF_PICKER_SESSION_EXPIRES, payload.get("expireTime"))
        _LOGGER.info(
            "Google Photos Picker-Session gestartet - Nutzer muss %s "
            "öffnen und Fotos auswählen",
            picker_uri,
        )
        return picker_uri

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

        # Purely local check, deliberately outside the try below: its
        # entire point is to short-circuit a session we already know is
        # dead *before* spending a network round-trip on it.
        if self._is_picker_session_expired():
            _LOGGER.info(
                "Picker-Session %s laut gespeichertem expireTime abgelaufen - "
                "verwerfe sie ohne Google-Anfrage. Neue Auswahl über den "
                "Service start_picker_session starten.",
                session_id,
            )
            self._clear_picker_session()
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
            loop = asyncio.get_running_loop()
            concurrency = self._download_concurrency()
            sem = asyncio.Semaphore(concurrency)
            limit_kbps = self._option(
                CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS
            )
            pacer = BandwidthPacer(limit_kbps) if limit_kbps > 0 else None
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
                items = payload.get("mediaItems", [])
                fetched_at = loop.time()

                # A page's baseUrls all expire ~60 min after Google issued
                # them (issue #46). Listing and downloading are already
                # interleaved per page, so a *run* of any length is fine -
                # but one page is 100 items, and with a low
                # bandwidth_limit_kbps and large videos those 100 can take
                # longer than the URLs live. Re-request the same page
                # (same pageToken) once the URLs get close to expiry, then
                # carry on at the same index: identical query, identical
                # order, only the baseUrls are fresh.
                # Downloads run concurrently within a page (issue #20),
                # bounded by a semaphore and sharing one BandwidthPacer so
                # bandwidth_limit_kbps stays a total rather than a
                # per-worker allowance. Batched rather than one big gather
                # so the baseUrl age check below still gets a chance to run
                # between batches instead of after the whole page.
                index = 0
                while index < len(items):
                    if loop.time() - fetched_at > BASEURL_MAX_AGE_SECONDS:
                        _LOGGER.info(
                            "baseUrls dieser Seite sind bald abgelaufen - fordere "
                            "Seite erneut an und setze bei Item %s/%s fort",
                            index + 1,
                            len(items),
                        )
                        resp = await self._oauth.async_request(
                            "GET", f"{PICKER_API_BASE}/mediaItems", params=params
                        )
                        resp.raise_for_status()
                        payload = await resp.json()
                        items = payload.get("mediaItems", [])
                        fetched_at = loop.time()
                        if index >= len(items):
                            # Shorter page than before (user changed the
                            # selection mid-run?) - nothing left here.
                            break

                    batch = items[index : index + concurrency]
                    await asyncio.gather(
                        *(
                            self._download_with_limit(sem, item, target_dir, stats, pacer)
                            for item in batch
                        )
                    )
                    index += len(batch)

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

    def _download_concurrency(self) -> int:
        """How many item downloads may run at once. Clamped to
        MAX_DOWNLOAD_CONCURRENCY so a typo in the options can't fire
        hundreds of parallel requests at Google."""
        raw = self._option(CONF_DOWNLOAD_CONCURRENCY, DEFAULT_DOWNLOAD_CONCURRENCY)
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return DEFAULT_DOWNLOAD_CONCURRENCY
        return max(1, min(value, MAX_DOWNLOAD_CONCURRENCY))

    async def _download_with_limit(
        self,
        sem: asyncio.Semaphore,
        item: dict[str, Any],
        target_dir: str,
        stats: BackupStats,
        pacer: BandwidthPacer | None,
    ) -> None:
        """Semaphore-guarded wrapper. _download_picker_item() already turns
        every per-item failure into a stats.errors entry, so one bad item
        can't take the whole gather() down with it."""
        async with sem:
            await self._download_picker_item(item, target_dir, stats, pacer=pacer)

    def _clear_picker_session(self) -> None:
        self.state.set(CONF_PICKER_SESSION_ID, None)
        self.state.set(CONF_PICKER_SESSION_URI, None)
        self.state.set(CONF_PICKER_SESSION_EXPIRES, None)

    def _is_picker_session_expired(self) -> bool:
        """Client-side check against the expireTime Google returned when
        the session was created (see async_start_picker_session) - lets
        us skip a pointless API round-trip for a session we already know
        is dead, instead of only finding out via a 404 from Google.
        Conservative: any missing/unparseable value is treated as "not
        expired" (falls through to the real status check against the
        API), since a client-side clock/parsing issue shouldn't be able
        to discard a session that's actually still fine.
        """
        expires_raw = self.state.get(CONF_PICKER_SESSION_EXPIRES)
        if not expires_raw:
            return False
        try:
            expires_at = datetime.fromisoformat(expires_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return False
        return datetime.now(timezone.utc) >= expires_at

    async def _download_picker_item(
        self,
        item: dict[str, Any],
        target_dir: str,
        stats: BackupStats,
        pacer: BandwidthPacer | None = None,
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

        # Reserve the destination name *before* downloading: the response
        # is streamed straight to disk rather than buffered in memory
        # (see issue #45 - Google Photos serves multi-GB 4K videos through
        # this same path, and HA commonly runs on 2-4 GB hardware where
        # buffering one would OOM the whole process, not just this
        # integration).
        def _reserve_destination() -> Path:
            dest_dir = dest_dir_for_date(target_dir, taken_at)
            return unique_destination(dest_dir, filename, self._reserved_destinations)

        # Reserve under a lock so two concurrent workers can't be handed the
        # same path: the file itself only appears at the final rename, so
        # without this the second worker's existence check would still say
        # "free" and one download would clobber the other.
        async with self._reserve_lock:
            dest = await self.hass.async_add_executor_job(_reserve_destination)
            self._reserved_destinations.add(dest)

        try:
            resp = await self._oauth.async_request(
                "GET", base_url + suffix, timeout=DOWNLOAD_TIMEOUT
            )
            resp.raise_for_status()
            # Handles the .part-file + atomic rename itself (issue #9).
            size = await throttled_stream_to_file(
                resp, dest, self.hass, limit_kbps, pacer=pacer
            )
        except Exception as err:  # noqa: BLE001 - surfaced to the user via sensor
            stats.errors.append(f"{filename}: Download fehlgeschlagen ({err})")
            # unique_destination() created the date folder and reserved the
            # name; throttled_stream_to_file() already removed its own
            # .part file, so nothing is left behind but the (possibly
            # empty) directory, which the next item will reuse.
            return

        finally:
            async with self._reserve_lock:
                self._reserved_destinations.discard(dest)

        def _set_mtime() -> None:
            ts = taken_at.timestamp()
            os.utime(dest, (ts, ts))

        await self.hass.async_add_executor_job(_set_mtime)

        processed_ids.append(item_id)
        self.state.set("processed_ids", processed_ids)
        stats.files_downloaded += 1
        stats.bytes_downloaded += size
        self._report_progress(stats)

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
