"""Download throttling for backends that stream HTTP responses themselves.

rclone has its own native `--bwlimit` flag (see rclone_backend.py), so it
doesn't need this. Used by every backend that streams bytes itself (library_api items,
Takeout/Drive archives). Everything streams straight to disk - both
paths can see multi-GB payloads (4K video, split Takeout archives), so
nothing may be buffered into memory as a whole.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from aiohttp import ClientResponse
from homeassistant.core import HomeAssistant

from ..const import DOWNLOAD_CHUNK_SIZE, DRIVE_DOWNLOAD_FLUSH_SIZE


class BandwidthPacer:
    """Sleeps as needed so cumulative bytes accounted for stay near
    limit_kbps (KiB/s), measured from the pacer's creation rather than
    chunk-to-chunk - a slow first chunk doesn't get "made up" by bursting
    later ones, it just evens out.

    Shareable on purpose. With concurrent downloads (issue #20), one pacer
    per download would let N workers each run at the full configured limit,
    silently multiplying the user's bandwidth cap by N. Passing a single
    instance to every worker keeps the limit meaning what it says: the
    total across the run.
    """

    def __init__(self, limit_kbps: int) -> None:
        self._bytes_per_second = limit_kbps * 1024 if limit_kbps > 0 else 0
        self._loop = asyncio.get_running_loop()
        self._started = self._loop.time()
        self._sent = 0
        # Serialises the read-modify-write of _sent so two workers can't
        # both compute their delay from the same stale total.
        self._lock = asyncio.Lock()

    @property
    def unlimited(self) -> bool:
        return not self._bytes_per_second

    async def account(self, n: int) -> None:
        if not self._bytes_per_second:
            return
        async with self._lock:
            self._sent += n
            expected_elapsed = self._sent / self._bytes_per_second
            delay = expected_elapsed - (self._loop.time() - self._started)
        # Sleep outside the lock: holding it while sleeping would serialise
        # the workers into a single-file queue, undoing the concurrency.
        if delay > 0:
            await asyncio.sleep(delay)


#: Backwards-compatible alias - older call sites constructed _Pacer.
_Pacer = BandwidthPacer


async def throttled_stream_to_file(
    resp: ClientResponse,
    dest_path: Path,
    hass: HomeAssistant,
    limit_kbps: int,
    chunk_size: int = DOWNLOAD_CHUNK_SIZE,
    flush_size: int = DRIVE_DOWNLOAD_FLUSH_SIZE,
    pacer: BandwidthPacer | None = None,
) -> int:
    """Stream resp's body to dest_path, paced to `limit_kbps` (KiB/s,
    <=0 = unlimited). Buffers up to `flush_size` in memory before each
    blocking write, so a multi-GB archive stays bounded to `flush_size` of
    RAM instead of being held in full, while still avoiding a write (and
    executor round-trip) per single network chunk. Returns the total
    number of bytes written.

    Writes to a `<dest_path>.part` sibling and only `os.replace()`s it
    onto `dest_path` after the transfer completes fully - so a process
    kill/restart mid-download (network exceptions are already handled by
    the caller) can never leave a truncated, corrupt file sitting at the
    filename callers treat as "this archive is done" (see issue #9). On
    any exception, the partial `.part` file is removed rather than left
    behind for a future run to trip over.
    """
    tmp_path = dest_path.with_name(dest_path.name + ".part")
    # A caller running downloads concurrently passes one shared pacer so
    # the limit applies across all of them, not per download.
    if pacer is None:
        pacer = BandwidthPacer(limit_kbps) if limit_kbps > 0 else None
    buffer = bytearray()
    total = 0
    wrote_anything = False

    def _flush(data: bytes, mode: str) -> None:
        with open(tmp_path, mode) as fh:
            fh.write(data)

    try:
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

        await hass.async_add_executor_job(os.replace, tmp_path, dest_path)
    except BaseException:
        await hass.async_add_executor_job(tmp_path.unlink, True)
        raise

    return total
