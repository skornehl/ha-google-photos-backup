"""takeout backend: import Google Takeout export archives.

This is the only backend that can see a user's *entire* existing library
(see README.md) - because it never talks to a restricted photos API at
all. The trade-off is that it's not push-based by default: archive files
need to land in `takeout_watch_dir` somehow. Three ways to get them there,
all optional and combinable:

  1. Manual: place archives into takeout_watch_dir yourself (e.g. copied
     from Google Takeout's own "scheduled exports" feature, which can
     auto-generate a new export every 2 months for a year into linked
     Drive/Dropbox/OneDrive/Box storage) via a separate sync step outside
     this integration's scope (rclone, Nextcloud, manual copy, ...).
  2. `_download_links`: paste one-time "download link" URLs from a
     Takeout export email (delivery method "Send download link via
     email") - see README "Large libraries" section for why that delivery
     method matters (it doesn't count against Drive storage quota).
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
import re
import shutil
import tarfile
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_DOWNLOAD_LINKS,
    CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    CONF_TAKEOUT_DRIVE_FOLDER_ID,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC,
    DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY,
    DRIVE_API_BASE,
    OAUTH2_SCOPES_DRIVE,
    TAKEOUT_ARCHIVE_SUFFIXES,
)
from .base import BackupBackend, BackupStats
from .fsutil import dest_dir_for_date, ensure_target_dir, sha256_file, unique_destination
from .throttle import throttled_stream_to_file

_LOGGER = logging.getLogger(__name__)

MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp",
    ".mp4", ".mov", ".m4v", ".avi", ".3gp",
}
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
        hass,
        entry,
        state,
        oauth_session: config_entry_oauth2_flow.OAuth2Session | None = None,
    ) -> None:
        """`oauth_session` is only set when Drive sync is enabled - see
        backends/__init__.py::async_create_backend. Download links never
        use it; they're plain, unauthenticated HTTPS fetches."""
        super().__init__(hass, entry, state)
        self._oauth = oauth_session

    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        watch_dir = self.entry.data[CONF_TAKEOUT_WATCH_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)
        watch_path = Path(watch_dir)
        if not await self.hass.async_add_executor_job(watch_path.is_dir):
            raise ValueError(f"Watch-Verzeichnis existiert nicht: {watch_dir}")
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
        await self._download_links(watch_dir, stats)
        await self._sync_drive_folder(watch_dir, stats)

        # name -> Drive file ID, persisted across runs so an archive that
        # downloaded fine but failed to *import* last run still gets
        # cleaned up from Drive once it does import successfully.
        drive_ids_by_name: dict[str, str] = self.state.get("drive_file_id_by_name", {})

        archives = await self.hass.async_add_executor_job(self._list_new_archives, watch_dir)
        for archive in archives:
            _LOGGER.info("Importiere Takeout-Archiv: %s", archive)
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

            # Only clean up from Drive *after* a successful import, never
            # right after download - an archive that downloaded fine but
            # failed to extract/import must stay in Drive so it isn't lost.
            drive_file_id = drive_ids_by_name.pop(archive.name, None)
            if drive_file_id is not None:
                self.state.set("drive_file_id_by_name", drive_ids_by_name)
                if delete_drive_after:
                    await self._cleanup_drive_file(drive_file_id, archive.name, stats)

            if delete_local_after:
                archive.unlink(missing_ok=True)

        return stats

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

    # -- download links (plain HTTPS, no OAuth) ------------------------------

    async def _download_links(self, watch_dir: Path, stats: BackupStats) -> None:
        raw = self._option(CONF_TAKEOUT_DOWNLOAD_LINKS, "") or ""
        urls = [line.strip() for line in raw.splitlines() if line.strip()]
        if not urls:
            return

        downloaded: list[str] = self.state.get("downloaded_takeout_links", [])
        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        session = async_get_clientsession(self.hass)

        for url in urls:
            if url in downloaded:
                continue
            _LOGGER.info("Lade Takeout-Archiv von manuellem Link herunter: %s", url)
            dest: Path | None = None
            try:
                async with session.get(url, allow_redirects=True) as resp:
                    resp.raise_for_status()
                    if "html" in resp.headers.get("Content-Type", "").lower():
                        # Most likely a Google sign-in/error page rather than
                        # the archive - this link needs an authenticated
                        # browser session we don't have here. Fail loudly
                        # instead of silently saving the HTML as a "zip".
                        raise ValueError(
                            "Antwort ist eine HTML-Seite statt eines Archivs - "
                            "dieser Link verlangt vermutlich eine angemeldete "
                            "Google-Browser-Session. Archiv stattdessen manuell "
                            "herunterladen und in takeout_watch_dir legen."
                        )
                    dest = watch_dir / self._filename_for_link(resp, url, watch_dir)
                    await throttled_stream_to_file(resp, dest, self.hass, limit_kbps)
            except Exception as err:  # noqa: BLE001 - surfaced via sensor
                stats.errors.append(f"Download-Link fehlgeschlagen ({url[:80]}): {err}")
                if dest is not None:
                    dest.unlink(missing_ok=True)
                continue

            downloaded.append(url)
            self.state.set("downloaded_takeout_links", downloaded)

    @staticmethod
    def _filename_for_link(resp, url: str, watch_dir: Path) -> str:
        disposition = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition)
        if match:
            name = Path(match.group(1)).name
        else:
            name = Path(urlsplit(url).path).name
        if not name or not any(name.lower().endswith(suf) for suf in TAKEOUT_ARCHIVE_SUFFIXES):
            name = f"takeout_link_{abs(hash(url)) % 10_000_000}.zip"
        # Two different email links could coincidentally suggest the same
        # filename (Google reuses "takeout-...-001.zip" numbering per
        # export) - never overwrite an existing file.
        candidate = watch_dir / name
        if not candidate.exists():
            return name
        stem, suffix = os.path.splitext(name)
        n = 1
        while (watch_dir / f"{stem}_{n}{suffix}").exists():
            n += 1
        return f"{stem}_{n}{suffix}"

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

        files: list[dict] = []
        page_token: str | None = None
        while True:
            params = {
                "q": query,
                "fields": "nextPageToken, files(id, name)",
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
                stats.errors.append(f"Drive-Abfrage fehlgeschlagen: {err}")
                return
            files.extend(payload.get("files", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        for entry in files:
            file_id = entry.get("id")
            name = entry.get("name", "")
            if not file_id or file_id in downloaded_ids:
                continue
            if not any(name.lower().endswith(suf) for suf in TAKEOUT_ARCHIVE_SUFFIXES):
                continue

            dest = watch_dir / name
            if dest.exists():
                # Already on disk from a previous run that crashed/restarted
                # before recording state - trust the existing file over
                # re-downloading a potentially huge archive.
                downloaded_ids.append(file_id)
                self.state.set("downloaded_drive_file_ids", downloaded_ids)
                self._remember_drive_file(name, file_id)
                continue

            _LOGGER.info("Lade Takeout-Archiv aus Google Drive: %s", name)
            try:
                resp = await self._oauth.async_request(
                    "GET", f"{DRIVE_API_BASE}/files/{file_id}", params={"alt": "media"}
                )
                resp.raise_for_status()
                await throttled_stream_to_file(resp, dest, self.hass, limit_kbps)
            except Exception as err:  # noqa: BLE001
                stats.errors.append(f"Drive-Download {name} fehlgeschlagen: {err}")
                dest.unlink(missing_ok=True)
                continue

            downloaded_ids.append(file_id)
            self.state.set("downloaded_drive_file_ids", downloaded_ids)
            self._remember_drive_file(name, file_id)

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
                "Takeout-Archiv in Google Drive %s: %s",
                "gelöscht" if permanently else "in den Papierkorb verschoben",
                name,
            )
        except Exception as err:  # noqa: BLE001 - surfaced via sensor, file just stays in Drive
            stats.errors.append(f"Aufräumen von {name} in Drive fehlgeschlagen: {err}")

    # -- archive import (blocking, runs in executor) -------------------------

    def _import_archive(self, archive: Path, target_dir: str, stats: BackupStats) -> None:
        with tempfile.TemporaryDirectory(prefix="gpb_takeout_") as tmp:
            tmp_path = Path(tmp)
            self._extract(archive, tmp_path)
            media_files = [
                p
                for p in tmp_path.rglob("*")
                if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
            ]
            for media_file in media_files:
                self._import_media_file(media_file, target_dir, stats)

    @staticmethod
    def _extract(archive: Path, dest: Path) -> None:
        name = archive.name.lower()
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(dest)
        elif name.endswith(".tgz") or name.endswith(".tar.gz"):
            with tarfile.open(archive, "r:gz") as tf:
                tf.extractall(dest)
        else:
            raise ValueError(f"Unbekanntes Archivformat: {archive.name}")

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
                _LOGGER.debug("Sidecar %s ohne verwertbaren Zeitstempel", sidecar)

        # Fall back to whatever mtime the archive gave the extracted file
        # (usually the archive creation time, not the photo date - better
        # than nothing but logged so it's visible in the sensor error list).
        stats_note = f"{media_file.name}: kein Sidecar-Zeitstempel gefunden, nutze Datei-mtime"
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


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for ca, cb in zip(a, b):
        if ca != cb:
            break
        n += 1
    return n
