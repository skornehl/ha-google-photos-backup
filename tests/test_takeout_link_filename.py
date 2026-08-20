"""Regression tests for the filename-resolution split (issue #5).

_filename_for_link() used to do string parsing AND blocking filesystem
stat() calls in one function called directly from a coroutine. Split into
_proposed_filename_for_link() (pure) and _resolve_unique_filename()
(blocking, meant to run via async_add_executor_job) - these tests just
confirm the split didn't change behavior.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from custom_components.google_photos_backup.backends.takeout_backend import (
    TakeoutBackend,
)


def _fake_response(headers: dict[str, str]):
    return SimpleNamespace(headers=headers)


def test_uses_content_disposition_filename_when_present():
    resp = _fake_response({"Content-Disposition": 'attachment; filename="takeout-20260801-001.zip"'})
    name = TakeoutBackend._proposed_filename_for_link(resp, "https://example.com/download?x=1")
    assert name == "takeout-20260801-001.zip"


def test_falls_back_to_url_path_when_no_content_disposition():
    resp = _fake_response({})
    name = TakeoutBackend._proposed_filename_for_link(
        resp, "https://example.com/files/takeout-002.zip"
    )
    assert name == "takeout-002.zip"


def test_falls_back_to_generated_name_when_nothing_usable():
    resp = _fake_response({})
    name = TakeoutBackend._proposed_filename_for_link(resp, "https://example.com/download?token=abc")
    assert name.startswith("takeout_link_")
    assert name.endswith(".zip")


def test_resolve_unique_filename_returns_original_when_free(tmp_path: Path):
    assert TakeoutBackend._resolve_unique_filename(tmp_path, "archive.zip") == "archive.zip"


def test_resolve_unique_filename_appends_suffix_on_collision(tmp_path: Path):
    (tmp_path / "archive.zip").write_bytes(b"x")
    assert TakeoutBackend._resolve_unique_filename(tmp_path, "archive.zip") == "archive_1.zip"


def test_resolve_unique_filename_keeps_incrementing(tmp_path: Path):
    (tmp_path / "archive.zip").write_bytes(b"x")
    (tmp_path / "archive_1.zip").write_bytes(b"x")
    assert TakeoutBackend._resolve_unique_filename(tmp_path, "archive.zip") == "archive_2.zip"
