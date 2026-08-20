"""Download throttling for backends that stream HTTP responses themselves.

rclone has its own native `--bwlimit` flag (see rclone_backend.py), so it
doesn't need this. This is only for library_api, which reads media bytes
via aiohttp directly.
"""
from __future__ import annotations

import asyncio

from aiohttp import ClientResponse

from ..const import DOWNLOAD_CHUNK_SIZE


async def throttled_read(
    resp: ClientResponse, limit_kbps: int, chunk_size: int = DOWNLOAD_CHUNK_SIZE
) -> bytes:
    """Read the full response body, pacing chunk reads to stay near
    `limit_kbps` (KiB/s).

    Rate is measured from the start of this download rather than
    chunk-to-chunk, so a slow first chunk (e.g. TLS handshake latency)
    doesn't get "made up" by bursting later chunks - it just evens out.
    `limit_kbps <= 0` means unlimited: reads the whole body in one shot,
    same as a plain `await resp.read()`.
    """
    if limit_kbps <= 0:
        return await resp.read()

    bytes_per_second = limit_kbps * 1024
    loop = asyncio.get_running_loop()
    started = loop.time()
    sent = 0
    chunks: list[bytes] = []
    async for chunk in resp.content.iter_chunked(chunk_size):
        chunks.append(chunk)
        sent += len(chunk)
        expected_elapsed = sent / bytes_per_second
        actual_elapsed = loop.time() - started
        if expected_elapsed > actual_elapsed:
            await asyncio.sleep(expected_elapsed - actual_elapsed)
    return b"".join(chunks)
