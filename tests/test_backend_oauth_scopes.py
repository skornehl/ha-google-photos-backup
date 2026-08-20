"""Tests for backend-declared OAuth scopes (issue #16).

Which scopes get requested is now each backend class's own declaration
(BackupBackend.oauth_scopes) rather than an if/elif chain inside the
config flow. These tests pin both halves of that contract: the lookup
itself, and that the config flow actually routes through it.
"""
from __future__ import annotations

from custom_components.google_photos_backup.backends import scopes_for_backend
from custom_components.google_photos_backup.backends.library_api import LibraryApiBackend
from custom_components.google_photos_backup.backends.rclone_backend import RcloneBackend
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.config_flow import (
    GooglePhotosBackupFlowHandler,
)
from custom_components.google_photos_backup.const import (
    BACKEND_LIBRARY_API,
    BACKEND_RCLONE,
    BACKEND_TAKEOUT,
    CONF_BACKEND,
    OAUTH2_SCOPES,
    OAUTH2_SCOPES_DRIVE,
)


def test_library_api_declares_photos_scopes():
    assert LibraryApiBackend.oauth_scopes == OAUTH2_SCOPES
    assert scopes_for_backend(BACKEND_LIBRARY_API) == OAUTH2_SCOPES


def test_takeout_declares_drive_scopes():
    assert TakeoutBackend.oauth_scopes == OAUTH2_SCOPES_DRIVE
    assert scopes_for_backend(BACKEND_TAKEOUT) == OAUTH2_SCOPES_DRIVE


def test_rclone_declares_no_scopes():
    """rclone does its own OAuth entirely outside this integration."""
    assert RcloneBackend.oauth_scopes is None
    assert scopes_for_backend(BACKEND_RCLONE) is None


def test_unknown_backend_returns_none():
    assert scopes_for_backend("not-a-backend") is None
    assert scopes_for_backend(None) is None


def test_config_flow_requests_drive_scopes_for_takeout():
    flow = GooglePhotosBackupFlowHandler()
    flow._data = {CONF_BACKEND: BACKEND_TAKEOUT}

    assert flow.extra_authorize_data["scope"] == " ".join(OAUTH2_SCOPES_DRIVE)


def test_config_flow_requests_photos_scopes_for_library_api():
    flow = GooglePhotosBackupFlowHandler()
    flow._data = {CONF_BACKEND: BACKEND_LIBRARY_API}

    assert flow.extra_authorize_data["scope"] == " ".join(OAUTH2_SCOPES)


def test_config_flow_falls_back_to_photos_scopes_when_backend_unknown():
    flow = GooglePhotosBackupFlowHandler()
    flow._data = {}

    assert flow.extra_authorize_data["scope"] == " ".join(OAUTH2_SCOPES)


def test_config_flow_always_requests_offline_access():
    """Regression guard: without access_type=offline + prompt=consent,
    Google doesn't return a refresh token and every entry would break
    an hour after setup."""
    flow = GooglePhotosBackupFlowHandler()
    flow._data = {CONF_BACKEND: BACKEND_LIBRARY_API}

    data = flow.extra_authorize_data
    assert data["access_type"] == "offline"
    assert data["prompt"] == "consent"
