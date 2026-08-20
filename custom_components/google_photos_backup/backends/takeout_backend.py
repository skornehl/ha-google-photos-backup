"""takeout backend: import Google Takeout export archives.

This is the only backend that can see a user's *entire* existing library
(see README.md) - because it never talks to a restricted API at all. The
trade-off is that it's not push-based: the user (or Google Takeout's own
"scheduled exports" feature, which can auto-generate a new Google Photos
export every 2 months for a year and drop it into linked Drive / Dropbox /
OneDrive / Box storage) has to get archive files into `takeout_watch_dir`.
Getting them from that cloud destination into the local watch dir is a
separate sync step outside this integration's scope (e.g. another rclone
remote, Nextcloud, or a manual copy).

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
from datetime import datetime, timezone
from pathlib import Path

from ..const import (
    CONF_TAKEOUT_DELETE_AFTER_IMPORT,
    CONF_TAKEOUT_WATCH_DIR,
    CONF_TARGET_DIR,
    TAKEOUT_ARCHIVE_SUFFIXES,
)
from .base import BackupBackend, BackupStats
from .fsutil import dest_dir_for_date, ensure_target_dir, sha256_file, unique_destination

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
    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        watch_dir = self.entry.data[CONF_TAKEOUT_WATCH_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)
        watch_path = Path(watch_dir)
        if not await self.hass.async_add_executor_job(watch_path.is_dir):
            raise ValueError(f"Watch-Verzeichnis existiert nicht: {watch_dir}")

    async def async_run_backup(self) -> BackupStats:
        stats = BackupStats()
        watch_dir = Path(self.entry.data[CONF_TAKEOUT_WATCH_DIR])
        target_dir = self.entry.data[CONF_TARGET_DIR]
        delete_after = self.entry.data.get(CONF_TAKEOUT_DELETE_AFTER_IMPORT, False)

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

            if delete_after:
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
