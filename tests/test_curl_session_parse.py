"""Tests for parsing a pasted cURL/PowerShell command into a CurlSession.

Fixtures are shaped like what Chrome/Firefox's "Copy as cURL"/"Copy as
PowerShell" DevTools actions actually produce, not idealized minimal
examples - real cURL commands carry a dozen unrelated headers and the
cookie value itself contains semicolons/special characters.
"""
from __future__ import annotations

from custom_components.google_photos_backup.backends.curl_session import (
    parse_curl_session,
)

_CURL_BASH = """\
curl 'https://takeout-download.usercontent.google.com/download/takeout-20260923T121036Z-1-003.zip?j=abc123&i=2&user=0' \\
  -H 'authority: takeout-download.usercontent.google.com' \\
  -H 'accept: text/html,application/xhtml+xml' \\
  -H 'accept-language: en-US,en;q=0.9' \\
  -H 'cookie: SID=abc.def; HSID=xyz; SSID=123; APISID=aaa; SAPISID=bbb; __Secure-1PSID=ccc' \\
  -H 'referer: https://takeout.google.com/' \\
  -H 'sec-fetch-mode: navigate' \\
  --compressed
"""

_POWERSHELL = """\
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$session.Cookies.Add((New-Object System.Net.Cookie("SID", "abc.def", "/", "google.com")))
$session.Cookies.Add((New-Object System.Net.Cookie("HSID", "xyz", "/", "google.com")))
$session.Cookies.Add((New-Object System.Net.Cookie("__Secure-1PSID", "ccc", "/", "google.com")))
$response = Invoke-WebRequest -UseBasicParsing -Uri "https://takeout-download.usercontent.google.com/download/takeout-20260923T121036Z-1-003.zip?j=abc123&i=2&user=0" `
-WebSession $session `
-Method "GET"
"""


def test_parses_bash_curl():
    result = parse_curl_session(_CURL_BASH)
    assert result is not None
    assert "SID=abc.def" in result.cookie
    assert "__Secure-1PSID=ccc" in result.cookie
    assert result.url_prefix == (
        "https://takeout-download.usercontent.google.com/download/"
        "takeout-20260923T121036Z-1-"
    )
    assert result.first_seq == 3
    assert result.extension == ".zip"
    assert result.query_string == "j=abc123&i=2&user=0"


def test_parses_powershell():
    result = parse_curl_session(_POWERSHELL)
    assert result is not None
    assert "SID=abc.def" in result.cookie
    assert "HSID=xyz" in result.cookie
    assert "__Secure-1PSID=ccc" in result.cookie
    assert result.first_seq == 3
    assert result.extension == ".zip"


def test_filename_and_url_walk_the_sequence():
    result = parse_curl_session(_CURL_BASH)
    assert result is not None

    assert result.filename(1) == "takeout-20260923T121036Z-1-001.zip"
    assert result.filename(12) == "takeout-20260923T121036Z-1-012.zip"

    url_1 = result.url(1)
    assert url_1.startswith(
        "https://takeout-download.usercontent.google.com/download/"
        "takeout-20260923T121036Z-1-001.zip?"
    )
    # The captured request's own i=2 must not leak into other files' URLs -
    # each file needs its own 0-based index alongside the 1-based, 3-digit
    # sequence number in the path.
    assert "i=0" in url_1
    assert "i=2" not in url_1
    assert "i=11" in result.url(12)


def test_returns_none_for_unrelated_text():
    assert parse_curl_session("") is None
    assert parse_curl_session("this is not a curl command") is None


def test_returns_none_without_cookie():
    no_cookie = _CURL_BASH.replace(
        "-H 'cookie: SID=abc.def; HSID=xyz; SSID=123; APISID=aaa; SAPISID=bbb; __Secure-1PSID=ccc' \\\n",
        "",
    )
    assert parse_curl_session(no_cookie) is None


def test_returns_none_for_url_not_matching_takeout_pattern():
    curl = (
        "curl 'https://example.com/some/file.zip' "
        "-H 'cookie: SID=abc.def'"
    )
    assert parse_curl_session(curl) is None
