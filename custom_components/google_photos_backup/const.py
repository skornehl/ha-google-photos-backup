"""Constants for the Google Photos Backup integration."""
from __future__ import annotations

from typing import Final

from aiohttp import ClientTimeout

DOMAIN: Final = "google_photos_backup"

# --- Backend selection -----------------------------------------------------
CONF_BACKEND: Final = "backend"
BACKEND_LIBRARY_API: Final = "library_api"
BACKEND_RCLONE: Final = "rclone"
BACKEND_TAKEOUT: Final = "takeout"
BACKENDS: Final = (BACKEND_LIBRARY_API, BACKEND_RCLONE, BACKEND_TAKEOUT)

# --- Common options ----------------------------------------------------------
CONF_TARGET_DIR: Final = "target_dir"
CONF_SYNC_INTERVAL_MINUTES: Final = "sync_interval_minutes"
DEFAULT_SYNC_INTERVAL_MINUTES: Final = 60
MIN_SYNC_INTERVAL_MINUTES: Final = 5

# --- Bandwidth throttling ------------------------------------------------------
# Applies to every backend that can transfer bytes itself: library_api and
# takeout always can (takeout via download links / Drive sync, see below);
# rclone passes it straight through as its own --bwlimit flag.
CONF_BANDWIDTH_LIMIT_KBPS: Final = "bandwidth_limit_kbps"
DEFAULT_BANDWIDTH_LIMIT_KBPS: Final = 0  # 0 = unlimited
DOWNLOAD_CHUNK_SIZE: Final = 65536  # 64 KiB per network read while streaming
DRIVE_DOWNLOAD_FLUSH_SIZE: Final = 8 * 1024 * 1024  # buffered writes for large archives

# Explicit per-request timeout for the actual byte-content downloads
# (library_api items, Drive archive downloads, download-link fetches) -
# NOT applied to small JSON/listing calls, which are fine with whatever
# default the underlying aiohttp session already has.
#
# total=None deliberately leaves the *overall* duration unbounded: with a
# low bandwidth_limit_kbps, a single large archive can legitimately take
# many hours, and a finite `total` here would silently defeat that
# feature by aborting the download partway through every time. sock_read
# instead catches a genuinely stalled connection (no bytes arriving at
# all for this long) - it does not fire because of our own deliberate
# pacing sleeps between chunk reads (those don't block a socket read;
# the server keeps sending into aiohttp's buffer in the background
# regardless of when we choose to consume it).
DOWNLOAD_TIMEOUT: Final = ClientTimeout(total=None, sock_connect=30, sock_read=300)

# --- library_api backend ------------------------------------------------------
# Removed 2025-03-31 by Google: photoslibrary, photoslibrary.readonly,
# photoslibrary.sharing. Only these three scopes still exist:
OAUTH2_SCOPE_APPCREATED_READONLY: Final = (
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
)
# Picker API scope - the only way left to let a user select from their
# *entire*, pre-existing library. Verify against
# https://developers.google.com/photos/picker at setup time; Google has
# changed scope names before.
OAUTH2_SCOPE_PICKER: Final = (
    "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
)
OAUTH2_SCOPES: Final = [
    OAUTH2_SCOPE_APPCREATED_READONLY,
    OAUTH2_SCOPE_PICKER,
]

OAUTH2_AUTHORIZE: Final = "https://accounts.google.com/o/oauth2/v2/auth"
OAUTH2_TOKEN: Final = "https://oauth2.googleapis.com/token"

LIBRARY_API_BASE: Final = "https://photoslibrary.googleapis.com/v1"
PICKER_API_BASE: Final = "https://photospicker.googleapis.com/v1"

CONF_PICKER_SESSION_ID: Final = "picker_session_id"
CONF_PICKER_SESSION_URI: Final = "picker_session_uri"
CONF_PICKER_SESSION_EXPIRES: Final = "picker_session_expires"

# --- rclone backend ------------------------------------------------------------
CONF_RCLONE_BINARY: Final = "rclone_binary"
DEFAULT_RCLONE_BINARY: Final = "rclone"
CONF_RCLONE_CONFIG_PATH: Final = "rclone_config_path"
CONF_RCLONE_REMOTE_NAME: Final = "rclone_remote_name"
CONF_RCLONE_SOURCE_PATH: Final = "rclone_source_path"
DEFAULT_RCLONE_SOURCE_PATH: Final = "media/by-month"
# Deliberately generous rather than short: with a low bandwidth_limit_kbps
# (see throttle.py) a single large, legitimate sync can take many hours -
# this is only meant to catch a genuinely hung rclone process (stuck auth
# prompt, dead connection that never errors out), not to cap normal
# large/slow transfers.
RCLONE_TIMEOUT_SECONDS: Final = 24 * 60 * 60

# --- takeout backend -------------------------------------------------------
CONF_TAKEOUT_WATCH_DIR: Final = "takeout_watch_dir"
CONF_TAKEOUT_DELETE_AFTER_IMPORT: Final = "takeout_delete_after_import"
DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT: Final = False
TAKEOUT_ARCHIVE_SUFFIXES: Final = (".zip", ".tgz", ".tar.gz")

# Optional: one-time archives from Takeout's "send download link via email"
# delivery (see README "Large libraries" section) - newline-separated URLs,
# downloaded straight into takeout_watch_dir. Plain HTTPS, no OAuth: these
# are meant to be pre-authorized, time-limited URLs, not something that
# needs a logged-in browser session. If Google *does* require one for a
# given link, the download fails with a clear error instead of silently
# saving an HTML login page as if it were an archive.
CONF_TAKEOUT_DOWNLOAD_LINKS: Final = "takeout_download_links"

# Optional: continuous alternative to manually placing archives in
# takeout_watch_dir - polls a Google Drive location (My Drive root, or one
# folder) for new "takeout-*" archives (as delivered by Takeout's own
# "Scheduled exports" feature) and downloads them in automatically. Needs
# its own OAuth consent (Drive API enabled + the scopes below added to the
# same Google Cloud project/consent screen used for library_api) -
# requested only when this is enabled, see config_flow.py.
CONF_TAKEOUT_DRIVE_SYNC: Final = "takeout_drive_sync"
DEFAULT_TAKEOUT_DRIVE_SYNC: Final = False
CONF_TAKEOUT_DRIVE_FOLDER_ID: Final = "takeout_drive_folder_id"
DEFAULT_TAKEOUT_DRIVE_FOLDER_ID: Final = ""  # empty = search all of My Drive
OAUTH2_SCOPE_DRIVE_READONLY: Final = "https://www.googleapis.com/auth/drive.readonly"
# .../drive.metadata (not .readonly) covers listing *and* mutating metadata
# (trashing/deleting a file is a metadata-level change, no content access
# needed for it) - narrower than the full .../auth/drive scope, which would
# also grant editing/replacing file *content* anywhere in the user's Drive.
# Only requested/used when "delete after sync" is enabled below.
OAUTH2_SCOPE_DRIVE_METADATA: Final = "https://www.googleapis.com/auth/drive.metadata"
OAUTH2_SCOPES_DRIVE: Final = [OAUTH2_SCOPE_DRIVE_READONLY, OAUTH2_SCOPE_DRIVE_METADATA]
DRIVE_API_BASE: Final = "https://www.googleapis.com/drive/v3"

# Optional: clean up a Drive archive once it has been successfully
# downloaded AND imported (not just downloaded - see takeout_backend.py).
# Off by default - trash, not full scope, is what's actually requested
# above regardless of this setting, so enabling it later needs no reauth.
CONF_TAKEOUT_DRIVE_DELETE_AFTER_SYNC: Final = "takeout_drive_delete_after_sync"
DEFAULT_TAKEOUT_DRIVE_DELETE_AFTER_SYNC: Final = False
# False (default) = move to trash (recoverable for ~30 days, but keeps
# counting against Drive quota until emptied). True = permanently delete
# immediately (frees quota right away, NOT recoverable) - see README.
CONF_TAKEOUT_DRIVE_DELETE_PERMANENTLY: Final = "takeout_drive_delete_permanently"
DEFAULT_TAKEOUT_DRIVE_DELETE_PERMANENTLY: Final = False

# --- persisted sync state (Store) ------------------------------------------
STORAGE_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = f"{DOMAIN}_{{entry_id}}"

# --- misc --------------------------------------------------------------------
PLATFORMS: Final = ["sensor"]
SERVICE_BACKUP_NOW: Final = "backup_now"
SERVICE_START_PICKER_SESSION: Final = "start_picker_session"

# Sensor keys - also used as translation_key / unique_id suffix, see
# sensor.py. Kept as constants rather than repeated string literals so a
# typo in one place can't silently desync from the other.
ATTR_LAST_SYNC: Final = "last_sync"
ATTR_FILES_BACKED_UP: Final = "files_backed_up"
ATTR_LAST_ERROR: Final = "last_error"
ATTR_FREE_SPACE: Final = "free_space"
