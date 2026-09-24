"""Tests for TakeoutBackend._download_via_curl_session (issue: replacement
for the removed, nonfunctional takeout_download_links feature).

Mocks aiohttp at the `session.get(...)` level (an async context manager),
not the whole HTTP stack, since that's the actual seam this method calls
through (`async_get_clientsession(self.hass)` then `.get()`).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import custom_components.google_photos_backup.backends.takeout_backend as takeout_module
from custom_components.google_photos_backup.backends.base import BackupStats, SyncStateStore
from custom_components.google_photos_backup.backends.takeout_backend import TakeoutBackend
from custom_components.google_photos_backup.const import (
    CONF_TAKEOUT_CURL_SESSION,
)


@pytest.fixture(autouse=True)
def _mock_issue_registry(monkeypatch):
    """The real issue_registry helpers need a real hass.data/storage - not
    worth setting up for tests that aren't about the repair issue itself.
    Callers that *are* about it (see the curl_session_expired_issue tests
    below) grab these mocks directly off takeout_module.ir instead of
    relying on this fixture's return value, so a plain autouse is enough."""
    monkeypatch.setattr(takeout_module.ir, "async_create_issue", MagicMock())
    monkeypatch.setattr(takeout_module.ir, "async_delete_issue", MagicMock())

_CURL = (
    "curl 'https://takeout-download.usercontent.google.com/download/"
    "takeout-20260923T121036Z-1-001.zip?j=abc&i=0' "
    "-H 'cookie: SID=abc.def; __Secure-1PSID=ccc'"
)


def _fake_response(status: int, content_type: str = "application/zip", body: bytes = b"x", url: str = ""):
    resp = MagicMock()
    resp.status = status
    resp.headers = {"Content-Type": content_type}
    resp.url = url or "https://takeout-download.usercontent.google.com/download/x"

    def _raise():
        if status >= 400:
            raise Exception(f"HTTP {status}")

    resp.raise_for_status = _raise

    async def _iter_chunked(size):
        yield body

    resp.content = SimpleNamespace(iter_chunked=_iter_chunked)

    class _Ctx:
        async def __aenter__(self):
            return resp

        async def __aexit__(self, *exc):
            return False

    return _Ctx()


def _make_backend(tmp_path: Path, curl_session: str) -> TakeoutBackend:
    entry = SimpleNamespace(
        entry_id="test_entry_id",
        title="Test entry",
        data={},
        options={CONF_TAKEOUT_CURL_SESSION: curl_session},
    )
    return TakeoutBackend(MagicMock(), entry, SyncStateStore({}))


async def test_downloads_until_three_consecutive_404s(monkeypatch, tmp_path: Path):
    responses = {
        1: _fake_response(200),
        2: _fake_response(200),
        3: _fake_response(404),
        4: _fake_response(404),
        5: _fake_response(404),
    }
    calls: list[str] = []

    fake_session = MagicMock()

    def _get(url, headers=None, **kwargs):
        seq = int(url.rsplit("-", 1)[-1][:3])
        calls.append(url)
        assert "cookie" in {k.lower() for k in headers}
        assert "SID=abc.def" in headers["Cookie"]
        return responses[seq]

    fake_session.get = _get
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    assert len(calls) == 5  # 001, 002 succeed; 003-005 are the 3 consecutive 404s that stop it
    assert (tmp_path / "takeout-20260923T121036Z-1-001.zip").exists()
    assert (tmp_path / "takeout-20260923T121036Z-1-002.zip").exists()
    assert not (tmp_path / "takeout-20260923T121036Z-1-003.zip").exists()
    assert stats.errors == []


async def test_stops_and_reports_clearly_on_expired_session(monkeypatch, tmp_path: Path):
    fake_session = MagicMock()
    fake_session.get = lambda url, headers=None, **kwargs: _fake_response(403)
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    assert len(stats.errors) == 1
    assert "session expired" in stats.errors[0]
    assert "takeout_curl_session" in stats.errors[0]


async def test_html_response_treated_as_expired_session(monkeypatch, tmp_path: Path):
    fake_session = MagicMock()
    fake_session.get = lambda url, headers=None, **kwargs: _fake_response(
        200, content_type="text/html; charset=utf-8"
    )
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    assert len(stats.errors) == 1
    assert "HTML page" in stats.errors[0]


async def test_already_downloaded_files_are_skipped_not_refetched(monkeypatch, tmp_path: Path):
    (tmp_path / "takeout-20260923T121036Z-1-001.zip").write_bytes(b"already here")

    fake_session = MagicMock()
    calls: list[str] = []

    def _get(url, headers=None, **kwargs):
        calls.append(url)
        return _fake_response(404)

    fake_session.get = _get
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    # 001 was never requested (already on disk); 002-004 hit as 3 consecutive 404s.
    assert not any("-001.zip" in c for c in calls)
    assert len(calls) == 3


async def test_empty_config_is_a_silent_noop(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        takeout_module, "async_get_clientsession", lambda hass: (_ for _ in ()).throw(AssertionError("should not be called"))
    )
    backend = _make_backend(tmp_path, "")
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    assert stats.errors == []


async def test_unparseable_session_reports_clear_error(tmp_path: Path):
    backend = _make_backend(tmp_path, "this is not a curl command")
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    assert len(stats.errors) == 1
    assert "Could not parse takeout_curl_session" in stats.errors[0]


async def test_expired_session_raises_a_repair_issue(monkeypatch, tmp_path: Path):
    fake_session = MagicMock()
    fake_session.get = lambda url, headers=None, **kwargs: _fake_response(403)
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    takeout_module.ir.async_create_issue.assert_called_once()
    _, kwargs = takeout_module.ir.async_create_issue.call_args
    assert kwargs["translation_key"] == "curl_session_expired"
    assert kwargs["is_fixable"] is True
    assert kwargs["data"] == {"entry_id": "test_entry_id"}
    takeout_module.ir.async_delete_issue.assert_not_called()


async def test_html_response_also_raises_a_repair_issue(monkeypatch, tmp_path: Path):
    fake_session = MagicMock()
    fake_session.get = lambda url, headers=None, **kwargs: _fake_response(
        200, content_type="text/html; charset=utf-8"
    )
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    takeout_module.ir.async_create_issue.assert_called_once()


async def test_successful_download_clears_any_expired_session_issue(monkeypatch, tmp_path: Path):
    fake_session = MagicMock()
    fake_session.get = lambda url, headers=None, **kwargs: _fake_response(200)
    monkeypatch.setattr(takeout_module, "async_get_clientsession", lambda hass: fake_session)

    backend = _make_backend(tmp_path, _CURL)
    backend.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
    stats = BackupStats()

    await backend._download_via_curl_session(tmp_path, stats)

    takeout_module.ir.async_create_issue.assert_not_called()
    takeout_module.ir.async_delete_issue.assert_called_with(
        backend.hass, takeout_module.DOMAIN, "curl_session_expired_test_entry_id"
    )
