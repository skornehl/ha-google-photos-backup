"""Blocking filesystem helpers, always called via hass.async_add_executor_job.

Kept in one place because both the library_api backend (fresh downloads) and
the takeout backend (extracted archive members) need the exact same
date-folder layout, hashing and collision handling so a file backed up by
one backend is recognized as a duplicate by the other.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from datetime import datetime
from pathlib import Path

CHUNK_SIZE = 1024 * 1024


def ensure_target_dir(target_dir: str) -> None:
    """Validate the configured backup directory exists and is writable.

    Raises ValueError with a user-facing message on failure - never creates
    the directory itself, since a typo'd path silently auto-created would
    hide a misconfigured mount from the user.
    """
    path = Path(target_dir)
    if not path.is_dir():
        raise ValueError(f"Zielverzeichnis existiert nicht oder ist kein Ordner: {target_dir}")
    probe = path / ".google_photos_backup_write_test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as err:
        raise ValueError(
            f"Zielverzeichnis ist nicht beschreibbar: {target_dir} ({err})"
        ) from err


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dest_dir_for_date(target_dir: str, taken_at: datetime) -> Path:
    """JJJJ/JJJJ-MM/ layout, e.g. 2026/2026-08/."""
    return Path(target_dir) / f"{taken_at.year:04d}" / f"{taken_at.year:04d}-{taken_at.month:02d}"


def unique_destination(dest_dir: Path, filename: str) -> Path:
    """Return a free path for `filename` inside dest_dir.

    Original filenames are preserved where possible; only on an actual name
    collision (different file, same name - Google reuses names like
    IMG_0001.jpg constantly) do we append a numeric suffix.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    candidate = dest_dir / filename
    if not candidate.exists():
        return candidate
    stem, suffix = os.path.splitext(filename)
    n = 1
    while True:
        candidate = dest_dir / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def move_into_place(src: Path, dest: Path, *, set_mtime: datetime | None = None) -> int:
    """Move src to dest (cross-filesystem safe), return byte size."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    size = dest.stat().st_size
    if set_mtime is not None:
        ts = set_mtime.timestamp()
        os.utime(dest, (ts, ts))
    return size


def free_bytes(target_dir: str) -> int:
    return shutil.disk_usage(target_dir).free
