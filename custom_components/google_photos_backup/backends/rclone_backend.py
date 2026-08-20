"""rclone backend: shells out to `rclone` and syncs its Google Photos remote.

IMPORTANT: rclone's `googlephotos` backend hits the exact same Google API
restriction as the library_api backend. Since rclone v1.70 / 2025-03-31,
rclone's own docs state: "rclone can only download photos it uploaded."
(https://rclone.org/googlephotos/). `media/all`, `media/by-year`,
`media/by-month` and `media/by-day` still exist as paths, but for an
rclone remote that was never used to *upload* the user's existing photos,
they will enumerate close to nothing. This backend is implemented per
spec and works correctly for whatever the configured remote *can* see -
but it is not a way around the API restriction, and README.md is explicit
about that.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path

from ..const import (
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_RCLONE_BINARY,
    CONF_RCLONE_CONFIG_PATH,
    CONF_RCLONE_REMOTE_NAME,
    CONF_RCLONE_SOURCE_PATH,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_RCLONE_BINARY,
)
from .base import BackupBackend, BackupStats
from .fsutil import ensure_target_dir

_LOGGER = logging.getLogger(__name__)


class RcloneBackend(BackupBackend):
    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)

        binary = self.entry.data.get(CONF_RCLONE_BINARY, DEFAULT_RCLONE_BINARY)
        found = await self.hass.async_add_executor_job(shutil.which, binary)
        if not found:
            raise ValueError(
                f"rclone-Binary '{binary}' wurde nicht gefunden (PATH). "
                "rclone ist in Home Assistant OS/Container nicht enthalten - "
                "es muss z. B. über ein eigenes Add-on/Image oder einen "
                "gemounteten Pfad bereitgestellt werden."
            )

        config_path = self.entry.data.get(CONF_RCLONE_CONFIG_PATH)
        if config_path and not await self.hass.async_add_executor_job(
            Path(config_path).is_file
        ):
            raise ValueError(f"rclone.conf nicht gefunden unter: {config_path}")

        remote = self.entry.data.get(CONF_RCLONE_REMOTE_NAME)
        if not remote:
            raise ValueError("Kein rclone-Remote-Name konfiguriert")

    async def async_run_backup(self) -> BackupStats:
        stats = BackupStats()
        binary = self.entry.data.get(CONF_RCLONE_BINARY, DEFAULT_RCLONE_BINARY)
        config_path = self.entry.data.get(CONF_RCLONE_CONFIG_PATH)
        remote = self.entry.data[CONF_RCLONE_REMOTE_NAME]
        source_path = self.entry.data.get(CONF_RCLONE_SOURCE_PATH, "media/by-month")
        target_dir = self.entry.data[CONF_TARGET_DIR]

        args = [binary]
        if config_path:
            args += ["--config", config_path]
        args += [
            "copy",
            f"{remote}:{source_path}",
            target_dir,
            "--use-json-log",
            "--create-empty-src-dirs=false",
            "--stats=10s",
            "--stats-one-line",
        ]
        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        if limit_kbps > 0:
            # rclone's `k` suffix is KiByte/s, matching our own KiB/s unit.
            args += ["--bwlimit", f"{limit_kbps}k"]

        _LOGGER.debug("rclone Aufruf: %s", " ".join(args))
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        self._parse_json_log(stderr.decode(errors="replace"), stats)
        self._parse_json_log(stdout.decode(errors="replace"), stats)

        if proc.returncode != 0:
            stats.errors.append(
                f"rclone beendete sich mit Exit-Code {proc.returncode}"
            )
            tail = stderr.decode(errors="replace").strip().splitlines()[-5:]
            if tail:
                stats.errors.append(" | ".join(tail))

        return stats

    @staticmethod
    def _parse_json_log(text: str, stats: BackupStats) -> None:
        """rclone --use-json-log emits one JSON object per line."""
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            level = entry.get("level")
            msg = entry.get("msg", "")
            if level == "error":
                stats.errors.append(msg)
                continue

            # rclone tags per-file completion messages with object/size once
            # a transfer finishes; count those as downloaded files.
            if entry.get("object") and "Copied" in msg:
                stats.files_downloaded += 1
                size = entry.get("size")
                if isinstance(size, (int, float)):
                    stats.bytes_downloaded += int(size)
            elif entry.get("object") and ("skipped" in msg.lower() or "unchanged" in msg.lower()):
                stats.files_skipped += 1
