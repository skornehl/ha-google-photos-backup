"""Constants for the Google Photos Backup integration."""
from __future__ import annotations

from datetime import timedelta
from typing import Final

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

# --- library_api backend ------------------------------------------------------
# Removed 2025-03-31 by Google: photoslibrary, photoslibrary.readonly,
# photoslibrary.sharing. Only these three scopes still exist:
OAUTH2_SCOPE_APPCREATED_READONLY: Final = (
    "https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata"
)
OAUTH2_SCOPE_APPENDONLY: Final = (
    "https://www.googleapis.com/auth/photoslibrary.appendonly"
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
DEFAULT_PICKER_POLL_INTERVAL: Final = timedelta(seconds=5)

# --- rclone backend ------------------------------------------------------------
CONF_RCLONE_BINARY: Final = "rclone_binary"
DEFAULT_RCLONE_BINARY: Final = "rclone"
CONF_RCLONE_CONFIG_PATH: Final = "rclone_config_path"
CONF_RCLONE_REMOTE_NAME: Final = "rclone_remote_name"
CONF_RCLONE_SOURCE_PATH: Final = "rclone_source_path"
DEFAULT_RCLONE_SOURCE_PATH: Final = "media/by-month"

# --- takeout backend -------------------------------------------------------
CONF_TAKEOUT_WATCH_DIR: Final = "takeout_watch_dir"
CONF_TAKEOUT_DELETE_AFTER_IMPORT: Final = "takeout_delete_after_import"
DEFAULT_TAKEOUT_DELETE_AFTER_IMPORT: Final = False
TAKEOUT_ARCHIVE_SUFFIXES: Final = (".zip", ".tgz", ".tar.gz")

# --- persisted sync state (Store) ------------------------------------------
STORAGE_VERSION: Final = 1
STORAGE_KEY_TEMPLATE: Final = f"{DOMAIN}_{{entry_id}}"

# --- misc --------------------------------------------------------------------
PLATFORMS: Final = ["sensor"]
SERVICE_BACKUP_NOW: Final = "backup_now"
SERVICE_START_PICKER_SESSION: Final = "start_picker_session"
UPDATE_INTERVAL_FALLBACK: Final = timedelta(minutes=DEFAULT_SYNC_INTERVAL_MINUTES)

ATTR_LAST_SYNC: Final = "last_sync"
ATTR_FILES_BACKED_UP: Final = "files_backed_up"
ATTR_LAST_ERROR: Final = "last_error"

MAX_DOWNLOAD_RETRIES: Final = 3
RETRY_BACKOFF_BASE_SECONDS: Final = 5
