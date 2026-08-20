"""Tests for download-link URL redaction (issue #18).

Takeout "download link" URLs carry Google-issued auth material in their
query string. They were previously logged in full at INFO level (and
truncated only by character count in the error path, which cuts
mid-token rather than removing it). Both now go through _redact_url().
"""
from __future__ import annotations

import pytest

from custom_components.google_photos_backup.backends.takeout_backend import _redact_url

_SECRET = "SUPER_SECRET_TOKEN_VALUE"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            f"https://takeout.google.com/takeout/download?j=abc123&token={_SECRET}",
            "https://takeout.google.com/takeout/download?<redacted>",
        ),
        (
            "https://example.com/files/takeout-001.zip",
            "https://example.com/files/takeout-001.zip",
        ),
        ("not a url at all", "<nicht parsebare URL>"),
        ("", "<nicht parsebare URL>"),
    ],
)
def test_redact_url(url: str, expected: str):
    assert _redact_url(url) == expected


def test_secret_never_survives_redaction():
    """The actual point of the fix - stated as its own assertion so a
    future 'improvement' to the format can't quietly reintroduce the
    leak while still matching a reformatted expected-string above."""
    url = f"https://takeout.google.com/download?token={_SECRET}&j=1"
    assert _SECRET not in _redact_url(url)


def test_redaction_keeps_enough_to_identify_the_link():
    """Redaction shouldn't make the log useless - host and path stay, so
    it's still clear *which* download failed."""
    redacted = _redact_url(f"https://takeout.google.com/takeout/download/abc?token={_SECRET}")
    assert "takeout.google.com" in redacted
    assert "/takeout/download/abc" in redacted
