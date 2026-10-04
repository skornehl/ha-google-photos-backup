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
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from ..const import (
    CONF_BANDWIDTH_LIMIT_KBPS,
    CONF_RCLONE_BINARY,
    CONF_RCLONE_CONFIG_PATH,
    CONF_RCLONE_REMOTE_NAME,
    CONF_RCLONE_SOURCE_PATH,
    CONF_TARGET_DIR,
    DEFAULT_BANDWIDTH_LIMIT_KBPS,
    DEFAULT_RCLONE_BINARY,
    RCLONE_TIMEOUT_SECONDS,
)
from .base import BackupBackend, BackupStats, SyncStateStore
from .fsutil import ensure_target_dir

_LOGGER = logging.getLogger(__name__)


class RcloneBackend(BackupBackend):
    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
        on_progress: Callable[[BackupStats], None] | None = None,
    ) -> None:
        super().__init__(hass, entry, state, on_progress)
        self._proc: asyncio.subprocess.Process | None = None

    async def async_terminate(self) -> None:
        """Kill an in-flight rclone subprocess, if any - called on
        unload/reload so a sync in progress doesn't keep running detached
        from HA (see issue #6)."""
        if self._proc is not None and self._proc.returncode is None:
            _LOGGER.warning("Terminating running rclone process (unload/reload)")
            self._proc.kill()
            await self._proc.wait()

    async def async_validate(self) -> None:
        target_dir = self.entry.data[CONF_TARGET_DIR]
        await self.hass.async_add_executor_job(ensure_target_dir, target_dir)

        binary = self.entry.data.get(CONF_RCLONE_BINARY, DEFAULT_RCLONE_BINARY)
        found = await self.hass.async_add_executor_job(shutil.which, binary)
        if not found:
            raise ValueError(
                f"rclone binary '{binary}' was not found on PATH. "
                "rclone is not bundled with Home Assistant OS/Container - "
                "it has to be provided separately, e.g. through a custom "
                "add-on/image or a mounted path."
            )

        config_path = self.entry.data.get(CONF_RCLONE_CONFIG_PATH)
        if config_path and not await self.hass.async_add_executor_job(
            Path(config_path).is_file
        ):
            raise ValueError(f"rclone.conf not found at: {config_path}")

        remote = self.entry.data.get(CONF_RCLONE_REMOTE_NAME)
        if not remote:
            raise ValueError("No rclone remote name configured")

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
            # Per-file "Copied (new)" lines are INFO level and never appear
            # at rclone's default NOTICE level - counting those always gave
            # 0. The periodic stats lines carry cumulative totals instead
            # (see _parse_json_log); they default to INFO too, so raise them.
            "--stats-log-level",
            "NOTICE",
        ]
        limit_kbps = self._option(CONF_BANDWIDTH_LIMIT_KBPS, DEFAULT_BANDWIDTH_LIMIT_KBPS)
        if limit_kbps > 0:
            # rclone's `k` suffix is KiByte/s, matching our own KiB/s unit.
            args += ["--bwlimit", f"{limit_kbps}k"]

        _LOGGER.debug("rclone invocation: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        proc = self._proc
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=RCLONE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            stats.errors.append(
                f"rclone process killed after {RCLONE_TIMEOUT_SECONDS}s without "
                "finishing (most likely a hung process - a normal throttled "
                "bulk transfer should not come close to this generous limit)."
            )
            return stats
        finally:
            self._proc = None

        # One pass over both streams (rclone logs to stderr by default), so
        # "last stats line wins" in _parse_json_log sees all of them.
        self._parse_json_log(
            stderr.decode(errors="replace") + "\n" + stdout.decode(errors="replace"), stats
        )

        if proc.returncode != 0:
            stats.errors.append(
                f"rclone exited with exit code {proc.returncode}"
            )
            tail = stderr.decode(errors="replace").strip().splitlines()[-5:]
            if tail:
                stats.errors.append(" | ".join(tail))

        return stats

    @staticmethod
    def _parse_json_log(text: str, stats: BackupStats) -> None:
        """rclone --use-json-log emits one JSON object per line.

        Stats lines carry a `stats` object (same shape as the rc call
        core/stats) with cumulative totals - the last one seen wins:
        `transfers` = files copied, `bytes` = bytes copied, `checks` =
        files compared against an existing copy (counted as skipped; a
        changed file would count as both, which for photos practically
        doesn't happen). Per-file "Copied" lines only exist at INFO level
        (`-v`), and are only used when no stats line was logged at all.
        """
        final: dict[str, Any] | None = None
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue

            if isinstance(entry.get("stats"), dict):
                final = entry["stats"]
                continue

            level = entry.get("level")
            msg = entry.get("msg", "")
            if level == "error":
                stats.errors.append(msg)
                continue

            if entry.get("object") and "Copied" in msg:
                stats.files_downloaded += 1
                size = entry.get("size")
                if isinstance(size, (int, float)):
                    stats.bytes_downloaded += int(size)
            elif entry.get("object") and ("skipped" in msg.lower() or "unchanged" in msg.lower()):
                stats.files_skipped += 1

        if final is not None:
            stats.files_downloaded = _int(final.get("transfers"))
            stats.bytes_downloaded = _int(final.get("bytes"))
            stats.files_skipped = _int(final.get("checks"))


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) else 0
