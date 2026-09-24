"""Parse a pasted cURL/PowerShell "copy as" command into a reusable Google
Takeout download session.

Why this exists: `takeout_download_links` (the "send download link via
email" feature, removed 2026-09-24) turned out to be fundamentally
unautomatable - Google requires an interactive, re-confirmed-per-download
sign-in for those one-time email links, with no session to reuse.

The "Manage exports" page on takeout.google.com is different: clicking
its Download button issues a request authenticated by the *browser's own
session cookie*, not a single-use link token. That cookie stays valid for
about an hour and works for any number of requests in that window - so a
user can capture ONE such request (DevTools -> Network -> right-click the
Download request -> Copy as cURL, or Copy as PowerShell), paste it in,
and this module extracts the cookie plus the archive's filename pattern
(Google Takeout splits large exports into `..._001.zip`, `..._002.zip`,
...) well enough to fetch every file in the export with that one cookie.

Credit: this two-step approach (capture a real session via a pasted
cURL, then walk the numbered filename pattern) is not something we
figured out ourselves - it's adapted from clivewatts/takeout_downloader_
script (https://github.com/clivewatts/takeout_downloader_script, MIT
licensed), a standalone downloader for exactly this problem. This module
reimplements the same idea natively for Home Assistant's async/aiohttp
stack (reusing this integration's own bandwidth-limited streaming and
dedup machinery) rather than vendoring that project's synchronous
`requests`-based code, but the parsing patterns and the "3 consecutive
404s means the export is done" heuristic below are directly modeled on
it. If you need a full-featured, non-integration downloader for a
Takeout export (parallel downloads, a TUI/web UI, resumable
byte-range downloads), that project does substantially more than this
module needs to and is worth using directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_POWERSHELL_INDICATORS = (
    "Invoke-WebRequest",
    "New-Object System.Net.Cookie",
    "$session",
    "WebRequestSession",
    "-WebSession",
)

# Google Takeout's split-archive naming: `takeout-<timestamp>-<batch>-<seq>.zip`
# (e.g. `takeout-20260923T121036Z-1-003.zip`), optionally followed by a query
# string. `<seq>` is what changes from file to file within one export.
_FILENAME_PATTERN = re.compile(r"(.*takeout-[^/?]+?-)(\d{3})(\.\w+)$")


@dataclass(frozen=True)
class CurlSession:
    """A parsed, reusable download session: the captured session cookie,
    plus enough of the URL to reconstruct every file in the export."""

    cookie: str
    url_prefix: str  # everything up to (not including) the 3-digit sequence
    first_seq: int  # the sequence number of the captured request itself
    extension: str  # e.g. ".zip", including the leading dot
    query_string: str  # without the leading "?", "" if none

    def filename(self, seq: int) -> str:
        return f"{self.url_prefix.rstrip('/').rsplit('/', 1)[-1]}{seq:03d}{self.extension}"

    def url(self, seq: int) -> str:
        url = f"{self.url_prefix}{seq:03d}{self.extension}"
        if self.query_string:
            # Google's query string carries a 0-based `i` index alongside
            # the 1-based, 3-digit sequence in the path - keep both in
            # sync rather than reusing the captured request's own index
            # for every file.
            query = re.sub(r"(?<=[?&])i=\d+", f"i={seq - 1}", self.query_string)
            url = f"{url}?{query}"
        return url


def _is_powershell(text: str) -> bool:
    return any(marker in text for marker in _POWERSHELL_INDICATORS)


def _cookie_from_powershell(text: str) -> str:
    # $session.Cookies.Add((New-Object System.Net.Cookie("NAME", "VALUE", "/", "domain")))
    pairs = re.findall(
        r'New-Object\s+System\.Net\.Cookie\s*\(\s*["\']([^"\']+)["\']\s*,\s*["\']([^"\']*)["\']',
        text,
    )
    return "; ".join(f"{name}={value}" for name, value in pairs)


def _url_from_powershell(text: str) -> str | None:
    match = re.search(r"-Uri\s+[\"']?(https?://[^\"'\s`]+)[\"']?", text, re.IGNORECASE)
    return match.group(1) if match else None


def _cookie_from_curl(text: str) -> str:
    match = re.search(r"-H\s+['\"]Cookie:\s*([^'\"]+)['\"]", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    match = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", text)
    if match:
        return match.group(1).strip()
    return ""


def _url_from_curl(text: str) -> str | None:
    match = re.search(r"curl\s+['\"]?(https?://[^'\"\s]+)['\"]?", text, re.IGNORECASE)
    if match:
        return match.group(1)
    for url in re.findall(r"https?://[^'\"\s]+", text):
        if "takeout" in url.lower():
            return url
    return None


def parse_curl_session(text: str) -> CurlSession | None:
    """Parse a pasted cURL or PowerShell "copy as" command.

    Returns None if the text doesn't look like a usable Takeout download
    request (missing cookie, missing URL, or a URL that doesn't match
    Google's split-archive filename pattern) - callers should surface
    that as a clear, actionable error rather than a bare parse failure.
    """
    text = text.strip()
    if not text:
        return None

    powershell = _is_powershell(text)
    cookie = _cookie_from_powershell(text) if powershell else _cookie_from_curl(text)
    url = _url_from_powershell(text) if powershell else _url_from_curl(text)
    if not cookie or not url:
        return None

    url_path, _, query_string = url.partition("?")
    match = _FILENAME_PATTERN.match(url_path)
    if not match:
        return None
    prefix, seq_str, extension = match.groups()

    return CurlSession(
        cookie=cookie,
        url_prefix=prefix,
        first_seq=int(seq_str),
        extension=extension,
        query_string=query_string,
    )
