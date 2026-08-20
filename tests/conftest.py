"""Shared pytest fixtures for the google_photos_backup test suite.

Uses pytest-homeassistant-custom-component, the standard way to unit-test a
HACS custom_component against a real (not hand-mocked) Home Assistant
runtime - it provides the `hass` fixture, MockConfigEntry, and patches HA's
custom_components loader so `custom_components.google_photos_backup` (this
repo, checked out at the repo root) is importable exactly like it would be
once installed via HACS.
"""
from __future__ import annotations

import pytest

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Make custom_components/ discoverable for every test automatically -
    without this, HA's component loader ignores custom_components/ during
    tests and every `hass.config_entries.async_setup(...)` call for this
    domain would fail with "integration not found"."""
    yield
