"""Download throttling for backends that stream HTTP responses themselves.

rclone has its own native `--bwlimit` flag (see rclone_backend.py), so it
doesn't need this. Used by library_api (small in-memory reads, one photo/
video at a time) and takeout (large archives, streamed straight to disk -
Takeout/Drive archives can be tens of GB, so those must never be buffered
into memory as a whole).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from aiohttp import ClientResponse
from homeassistant.core import HomeAssistant

from ..const import DOWNLOAD_CHUNK_SIZE, DRIVE_DOWNLOAD_FLUSH_SIZE


class _Pacer:
    """Sleeps as needed so cumulative bytes accounted for stay near
    limit_kbps (KiB/s), measured from the pacer's creation rather than
    chunk-to-chunk - a slow first chunk doesn't get "made up" by bursting
    later ones, it just evens out over the whole download."""

    def __init__(self, limit_kbps: int) -> None:
        self._bytes_per_second = limit_kbps * 1024 if limit_kbps > 0 else 0
        self._loop = asyncio.get_running_loop()
        self._started = self._loop.time()
        self._sent = 0

    async def account(self, n: int) -> None:
        if not self._bytes_per_second:
            return
        self._sent += n
        expected_elapsed = self._sent / self._bytes_per_second
        actual_elapsed = self._loop.time() - self._started
        if expected_elapsed > actual_elapsed:
            await asyncio.sleep(expected_elapsed - actual_elapsed)


async def throttled_read(
    resp: ClientResponse, limit_kbps: int, chunk_size: int = DOWNLOAD_CHUNK_SIZE
) -> bytes:
    """Read the full response body into memory, paced to `limit_kbps`
    (KiB/s). `limit_kbps <= 0` means unlimited - reads the whole body in
    one shot, same as a plain `await resp.read()`. Only for backends that
    know the payload is small (single photos/videos) - see
    `throttled_stream_to_file` for anything that could be large.
    """
    if limit_kbps <= 0:
        return await resp.read()

    pacer = _Pacer(limit_kbps)
    chunks: list[bytes] = []
    async for chunk in resp.content.iter_chunked(chunk_size):
        chunks.append(chunk)
        await pacer.account(len(chunk))
    return b"".join(chunks)


async def throttled_stream_to_file(
    resp: ClientResponse,
    dest_path: Path,
    hass: HomeAssistant,
    limit_kbps: int,
    chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    flush_size: int = DRIVE_DOWNLOAD_FLUSH_SIZE,
) -> int:
    """Stream resp's body straight to dest_path, paced to `limit_kbps`
    (KiB/s, <=0 = unlimited). Buffers up to `flush_size` in memory before
    each blocking write, so a multi-GB archive stays bounded to
    `flush_size` of RAM instead of being held in full, while still
    avoiding a write (and executor round-trip) per single network chunk.
    Returns the total number of bytes written.
    """
    pacer = _Pacer(limit_kbps) if limit_kbps > 0 else None
    buffer = bytearray()
    total = 0
    wrote_anything = False

    def _flush(data: bytes, mode: str) -> None:
        with open(dest_path, mode) as fh:
            fh.write(data)

    async for chunk in resp.content.iter_chunked(chunk_size):
        buffer.extend(chunk)
        total += len(chunk)
        if pacer is not None:
            await pacer.account(len(chunk))
        if len(buffer) >= flush_size:
            await hass.async_add_executor_job(
                _flush, bytes(buffer), "ab" if wrote_anything else "wb"
            )
            wrote_anything = True
            buffer.clear()

    if buffer or not wrote_anything:
        await hass.async_add_executor_job(
            _flush, bytes(buffer), "ab" if wrote_anything else "wb"
        )
    return total
