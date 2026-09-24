"""takeout backend: import Google Takeout export archives.

This is the only backend that can see a user's *entire* existing library
(see README.md) - because it never talks to a restricted photos API at
all. The trade-off is that it's not push-based by default: archive files
need to land in `takeout_watch_dir` somehow. Three ways to get them
there, combinable:

  1. Manual: place archives into takeout_watch_dir yourself (e.g. copied
     from Google Takeout's own "scheduled exports" feature, which can
     auto-generate a new export every 2 months for a year into linked
     Drive/Dropbox/OneDrive/Box storage) via a separate sync step outside
     this integration's scope (rclone, Nextcloud, manual copy, ...).
  2. `_download_via_curl_session` (see curl_session.py): paste a cURL/
     PowerShell command captured from the Download button on Takeout's
     "Manage exports" page. That request is authenticated by the
     browser's own session cookie (unlike a one-time emailed download
     link - confirmed in practice 2026-09-24 that those need a fresh
     interactive sign-in *every single download* and can't be automated
     at all, see the removed `takeout_download_links`/`_download_links`
     history in git log), and the cookie stays valid for repeat use for
     roughly an hour - long enough to fetch every split archive in an
     export by walking Google's numbered filename pattern. Credit:
     adapted from clivewatts/takeout_downloader_script, see
     curl_session.py.
  3. `_sync_drive_folder`: optional continuous alternative to (1) - polls
     Google Drive directly via the Drive API (OAuth, drive.readonly for
     listing/downloading + drive.metadata for the optional cleanup below)
     for new "takeout-*" archives and downloads them in automatically.
     Optionally, once an archive downloaded this way has been *imported*
     (not just downloaded), `_cleanup_drive_file` trashes or permanently
     deletes it from Drive to free up quota for the next scheduled
     export - see CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC.

Known Takeout quirks handled here:
  - Metadata lives in a sidecar `<file>.json` next to each media file, not
    embedded - `photoTakenTime.timestamp` is what we use to file the photo
    under the right `JJJJ/JJJJ-MM/` folder and to set its mtime.
  - For filenames Google considers "too long" the sidecar name gets
    truncated/suffixed inconsistently across Takeout export versions
    (`IMG_20240101.jpg.json`, `IMG_20240101.jpg.suppl.json`,
    `IMG_20240101.jpg.supplemental-metadata.json`, ...) - `_find_sidecar`
    tries several known patterns before falling back to a prefix match.
  - Split exports (`takeout-...-001.zip`, `-002.zip`, ...) are each
    independently valid archives covering part of the library tree; no
    reassembly is needed, they're simply processed one by one.
  - We do NOT rewrite embedded EXIF tags (would need Pillow/piexif, an
    extra dependency) - only the filesystem mtime and the JJJJ/JJJJ-MM
    folder placement are derived from the JSON sidecar. Camera-originated
    files already carry correct EXIF; this mainly matters for
    screenshots/downloads that never had EXIF to begin with.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_TAKEOUT_CURL_MAX_FILES,
    CONF_TAKEOUT_CURL_SESSION,
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    CONF_TAKEOUT_DRIVE_FOLDER_ID,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_TAKEOUT_CURL_MAX_FILES,
    DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    DOMAIN,
    DOWNLOAD_TIMEOUT,
    DRIVE_API_BASE,
    OAUTH2_SCOPES_DRIVE,
    TAKEOUT_ARCHIVE_SUFFIXES,
)
from .base import BackupBackend, BackupStats, SyncStateStore
from .curl_session import parse_curl_session
from .fsutil import dest_dir_for_date, ensure_target_dir, sha256_file, unique_destination
from .throttle import throttled_stream_to_file

_LOGGER = logging.getLogger(__name__)

# Repair issue raised when the captured browser session (see
# _download_via_curl_session below) stops working - expected to happen
# routinely (the cookie is only good for about an hour), not a bug, but
# easy to miss if the only signal is the last_error sensor. See repairs.py
# for the fix flow that lets a fresh cURL/PowerShell command be pasted
# directly from Settings -> System -> Repairs.
def _curl_session_issue_id(entry_id: str) -> str:
    return f"curl_session_expired_{entry_id}"

# Takeout's own non-media files: JSON sidecars (per item, per album
# `metadata.json`, `print-subscriptions.json`, ...) and the
# `archive_browser.html` index at the archive root. Everything else in a
# Google Photos export is user content and gets imported.
#
# Deliberately a blocklist, not a media allowlist: an allowlist silently
# drops every format nobody thought of (RAW like .dng/.cr2/.nef, .mkv,
# .webm, .tif, .avif, .heif, .mts, motion-photo .mp, ...), and because the
# archive is marked processed afterwards, a later run never picks those
# files up again. For a backup, importing one stray file too many is
# harmless; losing a file without a trace is not.
TAKEOUT_METADATA_SUFFIXES = frozenset({".json", ".html", ".htm"})
SIDECAR_PATTERNS = (
    "{name}.json",
    "{name}.suppl.json",
    "{name}.supplemental-metadata.json",
)


class TakeoutBackend(BackupBackend):
    # Only relevant when Drive sync is enabled - the plain-takeout path
    # never goes through OAuth at all (see config_flow's
    # async_step_takeout_drive_choice, which decides that before any
    # authorize URL is built).
    oauth_scopes = OAUTH2_SCOPES_DRIVE

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
        on_progress: Callable[[BackupStats], None] | None = None,
    ) -> None:
        """`oauth_session` is only set when Drive sync is enabled - see
        backends/__init__.py::async_create_backend."""
        super().__init__(hass, entry, state, on_progress)
        self._oauth = oauth_session

    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        watch_dir = self.entry.data[CONF_TAKEOUT_WATCH_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)
        watch_path = Path(watch_dir)
        if not await self.hass.async_add_executor_job(watch_path.is_dir):
            raise ValueError(f"Watch directory does not exist: {watch_dir}")
        if self._oauth is not None:
            await self._oauth.async_ensure_token_valid()

    async def async_run_backup(self) -> BackupStats:
        stats = BackupStats()
        watch_dir = Path(self.entry.data[CONF_TAKEOUT_WATCH_DIR])
        target_dir = self.entry.data[CONF_TARGET_DIR]
        delete_local_after = self.entry.data.get(CONF_TAKEOUT_DELETE_AFTER_IMPORT, False)
        delete_drive_after = self._option(
            CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC, DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC
        )

        # Both of these only ever add files to watch_dir - the archive
        # scan/import below then treats them exactly like anything the
        # user dropped in manually, so there's exactly one import code
        # path regardless of how an archive got here.
        await self._download_via_curl_session(watch_dir, stats)
        await self._sync_drive_folder(watch_dir, stats)

        # name -> Drive file ID, persisted across runs so an archive that
        # downloaded fine but failed to *import* last run still gets
        # cleaned up from Drive once it does import successfully.
        drive_ids_by_name: dict[str, str] = self.state.get("drive_file_id_by_name", {})

        archives = await self.hass.async_add_executor_job(self._list_new_archives, watch_dir)
        for archive in archives:
            _LOGGER.info("Importing Takeout archive: %s", archive)
            try:
                await self.hass.async_add_executor_job(
                    self._import_archive, archive, target_dir, stats
                )
            except Exception as err:  # noqa: BLE001
                stats.errors.append(f"{archive.name}: {err}")
                continue

            processed_archives: list[str] = self.state.get("processed_archives", [])
            processed_archives.append(archive.name)
            self.state.set("processed_archives", processed_archives)
            self._report_progress(stats)

            # Only clean up from Drive *after* a successful import, never
            # right after download - an archive that downloaded fine but
            # failed to extract/import must stay in Drive so it isn't lost.
            drive_file_id = drive_ids_by_name.pop(archive.name, None)
            if drive_file_id is not None:
                self.state.set("drive_file_id_by_name", drive_ids_by_name)
                if delete_drive_after:
                    await self._cleanup_drive_file(drive_file_id, archive.name, stats)

            if delete_local_after:
                await self.hass.async_add_executor_job(archive.unlink, True)

        return stats

    # -- cURL/PowerShell captured session (cookie-authenticated, no OAuth) ---

    async def _download_via_curl_session(self, watch_dir: Path, stats: BackupStats) -> None:
        """Fetch every archive in a Takeout export using a session cookie
        captured from the browser (see curl_session.py). Credit:
        clivewatts/takeout_downloader_script - see the module docstring
        above and curl_session.py."""
        raw = self._option(CONF_TAKEOUT_CURL_SESSION, "") or ""
        if not raw.strip():
            return

        session_info = parse_curl_session(raw)
        if session_info is None:
            stats.errors.append(
                "Could not parse takeout_curl_session - paste the full cURL "
                "or PowerShell command exactly as copied from DevTools "
                "(Network tab, right-click the Download request, "
                "Copy as cURL/Copy as PowerShell). See README."
            )
            return

        max_files = int(
            self._option(CONF_TAKEOUT_CURL_MAX_FILES, DEFAULT_TAKEOUT_CURL_MAX_FILES)
        )
        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        http_session = async_get_clientsession(self.hass)
        headers = {
            "Cookie": session_info.cookie,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            ),
        }

        consecutive_404 = 0
        seq = 1
        while seq <= max_files and consecutive_404 < 3:
            name = session_info.filename(seq)
            dest = watch_dir / name
            if await self.hass.async_add_executor_job(dest.exists):
                # Already downloaded (this run or a previous one) -
                # throttled_stream_to_file only ever leaves a complete
                # file at this path, never a partial one, see throttle.py.
                seq += 1
                consecutive_404 = 0
                continue

            url = session_info.url(seq)
            try:
                async with http_session.get(
                    url, headers=headers, allow_redirects=True, timeout=DOWNLOAD_TIMEOUT
                ) as resp:
                    if resp.status == 404:
                        consecutive_404 += 1
                        seq += 1
                        continue
                    if resp.status in (401, 403) or "accounts.google" in str(resp.url):
                        stats.errors.append(
                            f"Google session expired while downloading {name} - "
                            "capture a fresh cURL/PowerShell command (click "
                            "Download again in Manage exports, DevTools -> "
                            "Network -> Copy as cURL) and paste it into "
                            "takeout_curl_session. Already-downloaded files "
                            "are kept, the next run resumes from here."
                        )
                        self._raise_curl_session_expired_issue()
                        return
                    resp.raise_for_status()
                    if "html" in resp.headers.get("Content-Type", "").lower():
                        stats.errors.append(
                            f"{name}: response is an HTML page rather than an "
                            "archive - the session has likely expired, see "
                            "above."
                        )
                        self._raise_curl_session_expired_issue()
                        return
                    _LOGGER.info(
                        "Downloading Takeout archive via captured session: %s", name
                    )
                    await throttled_stream_to_file(resp, dest, self.hass, limit_kbps)
                    # A chunk just downloaded successfully with this
                    # cookie - any previously-raised "session expired"
                    # repair issue no longer applies. async_delete_issue
                    # is a no-op if there is nothing to delete.
                    ir.async_delete_issue(
                        self.hass, DOMAIN, _curl_session_issue_id(self.entry.entry_id)
                    )
            except Exception as err:  # noqa: BLE001 - surfaced via sensor
                stats.errors.append(f"{name}: {err}")
                return

            consecutive_404 = 0
            seq += 1

    def _raise_curl_session_expired_issue(self) -> None:
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            _curl_session_issue_id(self.entry.entry_id),
            is_fixable=True,
            is_persistent=True,
            severity=ir.IssueSeverity.WARNING,
            translation_key="curl_session_expired",
            translation_placeholders={"title": self.entry.title},
            data={"entry_id": self.entry.entry_id},
        )

    def _list_new_archives(self, watch_dir: Path) -> list[Path]:
        processed = set(self.state.get("processed_archives", []))
        found = [
            p
            for p in sorted(watch_dir.iterdir())
            if p.is_file()
            and p.name not in processed
            and any(p.name.lower().endswith(suf) for suf in TAKEOUT_ARCHIVE_SUFFIXES)
        ]
        return found

    # -- Google Drive folder sync (OAuth, drive.readonly + drive.metadata) ---

    async def _sync_drive_folder(self, watch_dir: Path, stats: BackupStats) -> None:
        if self._oauth is None:
            return
        await self._oauth.async_ensure_token_valid()

        folder_id = self._option(CONF_TAKEOUT_DRIVE_FOLDER_ID, "") or None
        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        downloaded_ids: list[str] = self.state.get("downloaded_drive_file_ids", [])

        query = "name contains 'takeout-' and trashed = false"
        if folder_id:
            query += f" and '{folder_id}' in parents"

        files: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name, size)",
                "pageSize": 100,
                "spaces": "drive",
            }
            if page_token:
                params["pageToken"] = page_token
            try:
                resp = await self._oauth.async_request("GET", f"{DRIVE_API_BASE}/files", params=params)
                resp.raise_for_status()
                payload = await resp.json()
            except Exception as err:  # noqa: BLE001
                stats.errors.append(f"Drive query failed: {err}")
                return
            files.extend(payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        for drive_file in files:
            file_id = drive_file.get("id")
            name = drive_file.get("name", "")
            drive_size = drive_file.get("size")
            if not file_id or file_id in downloaded_ids:
                continue
            if not any(name.lower().endswith(suf) for suf in TAKEOUT_ARCHIVE_SUFFIXES):
                continue

            dest = watch_dir / name
            if await self.hass.async_add_executor_job(dest.exists):
                if await self.hass.async_add_executor_job(
                    self._local_file_matches_drive_size, dest, drive_size
                ):
                    # Already on disk from a previous run that crashed/
                    # restarted before recording state, and the size on
                    # disk matches what Drive reports - trust the existing
                    # file over re-downloading a potentially huge archive.
                    downloaded_ids.append(file_id)
                    self.state.set("downloaded_drive_file_ids", downloaded_ids)
                    self._remember_drive_file(name, file_id)
                    continue
                # Exists but doesn't match Drive's reported size - most
                # likely a truncated download from a crash mid-write.
                # Trusting it would silently and permanently skip backing
                # up this archive (see issue #2): discard and re-download.
                _LOGGER.warning(
                    "Existing file %s does not match the file size reported by "
                    "Drive (most likely an aborted download) - downloading it again.",
                    name,
                )
                await self.hass.async_add_executor_job(dest.unlink)

            _LOGGER.info("Downloading Takeout archive from Google Drive: %s", name)
            try:
                resp = await self._oauth.async_request(
                    "GET",
                    f"{DRIVE_API_BASE}/files/{file_id}",
                    params={"alt": "media"},
                    timeout=DOWNLOAD_TIMEOUT,
                )
                resp.raise_for_status()
                await throttled_stream_to_file(resp, dest, self.hass, limit_kbps)
            except Exception as err:  # noqa: BLE001
                stats.errors.append(f"Drive download of {name} failed: {err}")
                await self.hass.async_add_executor_job(dest.unlink, True)
                continue

            downloaded_ids.append(file_id)
            self.state.set("downloaded_drive_file_ids", downloaded_ids)
            self._remember_drive_file(name, file_id)

    @staticmethod
    def _local_file_matches_drive_size(dest: Path, drive_size: str | int | None) -> bool:
        """Compare an existing local file's size against what Drive
        reported for it, to distinguish "fully downloaded, only the state
        write was missed" from "crashed mid-download, file is truncated".

        `drive_size` is whatever `files.list`'s `size` field returned -
        normally a numeric string, but treated defensively since it's
        external API data. If Drive didn't report a size at all, falls
        back to trusting existence (the pre-fix behavior), rather than
        being stricter than the API itself.
        """
        if drive_size is None:
            return True
        try:
            return dest.stat().st_size == int(drive_size)
        except (OSError, TypeError, ValueError):
            return False

    def _remember_drive_file(self, name: str, file_id: str) -> None:
        """Record which Drive file a watch_dir archive came from, so
        _cleanup_drive_file can find it again once the archive has been
        successfully imported (see async_run_backup)."""
        name_map: dict[str, str] = self.state.get("drive_file_id_by_name", {})
        name_map[name] = file_id
        self.state.set("drive_file_id_by_name", name_map)

    async def _cleanup_drive_file(self, file_id: str, name: str, stats: BackupStats) -> None:
        """Trash (default) or permanently delete an archive from Drive
        after it has been successfully imported. Only called when
        CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC is on - see async_run_backup.
        """
        if self._oauth is None:
            # Only reachable if an archive was recorded in
            # drive_file_id_by_name while Drive sync was on and the entry
            # was later reconfigured without it. Nothing to clean up
            # against, and definitely not worth raising over.
            return

        permanently = self._option(
            CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY, DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY
        )
        try:
            if permanently:
                resp = await self._oauth.async_request("DELETE", f"{DRIVE_API_BASE}/files/{file_id}")
            else:
                resp = await self._oauth.async_request(
                    "PATCH", f"{DRIVE_API_BASE}/files/{file_id}", json={"trashed": True}
                )
            if resp.status != 404:  # 404 = already gone, nothing to do
                resp.raise_for_status()
            _LOGGER.info(
                "Takeout archive in Google Drive %s: %s",
                "permanently deleted" if permanently else "moved to trash",
                name,
            )
        except Exception as err:  # noqa: BLE001 - surfaced via sensor, file just stays in Drive
            stats.errors.append(f"Cleaning up {name} in Drive failed: {err}")

    # -- archive import (blocking, runs in executor) -------------------------

    def _import_archive(self, archive: Path, target_dir: str, stats: BackupStats) -> None:
        # dir=target_dir deliberately, NOT the OS default tmp location:
        # Home Assistant OS mounts /tmp as tmpfs (RAM-backed), which is
        # nowhere near big enough for a 50 GB+ Takeout archive regardless
        # of how much space the actual target disk has (confirmed
        # 2026-09-24: extraction failed with "only 3918 MiB available"
        # while the target disk had 1.6+ TB free). Extracting directly
        # on target_dir's filesystem also means the final shutil.move()
        # below is a same-filesystem rename instead of a slow
        # cross-filesystem copy when target_dir is a network mount.
        with tempfile.TemporaryDirectory(prefix="gpb_takeout_", dir=target_dir) as tmp:
            tmp_path = Path(tmp)
            self._check_free_space(archive, tmp_path)
            self._extract(archive, tmp_path)
            media_files = [
                p for p in tmp_path.rglob("*") if p.is_file() and _is_takeout_content(p)
            ]
            for media_file in media_files:
                self._import_media_file(media_file, target_dir, stats)

    @staticmethod
    def _check_free_space(archive: Path, extract_dir: Path) -> None:
        """Fail fast with a clear message if the extraction target is
        obviously too small for this archive.

        Without this the extraction still fails safely (the
        TemporaryDirectory context manager cleans up, and the archive
        isn't marked processed, so it's retried next run) - but only
        with a bare OSError: [Errno 28] surfaced on the last_error
        sensor, which reads like a bug rather than "your disk is full".

        Uses the compressed archive size as the estimate, times a
        modest safety factor. Takeout archives are overwhelmingly
        already-compressed JPEG/MP4 payloads, so uncompressed size is
        close to compressed size - deliberately not trying to read the
        real uncompressed size from the archive headers, which would
        mean opening every archive twice. This is a cheap sanity check
        for the obvious case, not a guarantee.
        """
        try:
            archive_size = archive.stat().st_size
            free = shutil.disk_usage(extract_dir).free
        except OSError:
            return  # Can't tell - let the extraction itself decide.

        required = int(archive_size * 1.2)
        if free < required:
            raise ValueError(
                f"Not enough free disk space to extract: {archive.name} needs "
                f"about {required // (1024 * 1024)} MiB, but only "
                f"{free // (1024 * 1024)} MiB are available under {extract_dir}. The "
                "archive is left in place and retried on the next run."
            )

    @staticmethod
    def _extract(archive: Path, dest: Path) -> None:
        name = archive.name.lower()
        if name.endswith(".zip"):
            # zipfile has sanitized member paths (strips '..'/absolute
            # components) in the stdlib for a long time - no extra check
            # needed here, unlike tarfile below.
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        elif name.endswith(".tgz") or name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tf:
                _safe_tar_extractall(tf, dest)
        else:
            raise ValueError(f"Unknown archive format: {archive.name}")

    def _import_media_file(self, media_file: Path, target_dir: str, stats: BackupStats) -> None:
        digest = sha256_file(media_file)
        if digest in self.state.processed_hashes:
            stats.files_skipped += 1
            return

        taken_at = self._resolve_taken_at(media_file)
        dest_dir = dest_dir_for_date(target_dir, taken_at)
        dest = unique_destination(dest_dir, media_file.name)
        shutil.move(str(media_file), str(dest))
        ts = taken_at.timestamp()
        os.utime(dest, (ts, ts))

        self.state.add_processed_hash(digest)
        stats.files_downloaded += 1
        stats.bytes_downloaded += dest.stat().st_size

    def _resolve_taken_at(self, media_file: Path) -> datetime:
        sidecar = self._find_sidecar(media_file)
        if sidecar is not None:
            try:
                payload = json.loads(sidecar.read_text(encoding="utf-8"))
                ts = int(payload["photoTakenTime"]["timestamp"])
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (KeyError, ValueError, json.JSONDecodeError):
                _LOGGER.debug("Sidecar %s has no usable timestamp", sidecar)

        # Fall back to whatever mtime the archive gave the extracted file
        # (usually the archive creation time, not the photo date - better
        # than nothing but logged so it's visible in the sensor error list).
        stats_note = f"{media_file.name}: no sidecar timestamp found, falling back to file mtime"
        _LOGGER.warning(stats_note)
        return datetime.fromtimestamp(media_file.stat().st_mtime, tz=timezone.utc)

    @staticmethod
    def _find_sidecar(media_file: Path) -> Path | None:
        directory = media_file.parent
        for pattern in SIDECAR_PATTERNS:
            candidate = directory / pattern.format(name=media_file.name)
            if candidate.is_file():
                return candidate

        # Truncated-filename fallback: Takeout sometimes shortens the
        # sidecar's stem so it no longer matches the media filename
        # exactly. Pick the *.json in the same directory whose name shares
        # the longest prefix with the media filename, if any share at
        # least 8 characters (avoids matching an unrelated sidecar).
        best: Path | None = None
        best_len = 7
        for candidate in directory.glob("*.json"):
            prefix_len = _common_prefix_len(candidate.stem, media_file.name)
            if prefix_len > best_len:
                best_len = prefix_len
                best = candidate
        return best


def _is_takeout_content(path: Path) -> bool:
    """True for anything in an extracted archive that should be backed up -
    i.e. everything except Takeout's own metadata files, see
    TAKEOUT_METADATA_SUFFIXES."""
    return path.suffix.lower() not in TAKEOUT_METADATA_SUFFIXES


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n


def _safe_tar_extractall(tf: tarfile.TarFile, dest: Path) -> None:
    """Extract a tar archive, rejecting any member that would land outside
    `dest` or that is a symlink/hardlink (path traversal / "Zip Slip" for
    tar, CVE-2007-4559).

    Unlike zipfile, tarfile.extractall() only defends against this by
    default starting with Python 3.14 (PEP 706's `filter="data"` becoming
    the default). Takeout .tgz archives can reach this code via Drive
    sync, not just manually placed files, so this can't rely on "Google
    is trusted" - and Home Assistant can run on Python versions well
    before 3.14.

    Strategy: prefer the real `filter="data"` where available (Python
    3.12+, does more than just path-traversal checking - also drops
    dangerous permission bits etc.); on older Python where `extractall()`
    doesn't accept `filter` at all, fall back to a manual check that
    covers at least the path-traversal and symlink/hardlink cases.
    """
    try:
        tf.extractall(dest, filter="data")
        return
    except TypeError:
        pass  # Python < 3.12: extractall() has no `filter` parameter yet.

    dest_resolved = dest.resolve()
    for member in tf.getmembers():
        if member.issym() or member.islnk():
            raise ValueError(
                f"Takeout archive contains a symlink/hardlink, rejecting it: {member.name}"
            )
        member_path = (dest / member.name).resolve()
        try:
            member_path.relative_to(dest_resolved)
        except ValueError:
            raise ValueError(
                "Takeout archive contains a path outside the target directory "
                f"(possible path traversal attempt): {member.name}"
            ) from None
    tf.extractall(dest)
