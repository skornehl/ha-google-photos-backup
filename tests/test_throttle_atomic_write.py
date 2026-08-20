"""Regression tests for atomic downloads via temp-file + rename (issue #9).

throttled_stream_to_file() now writes to a `<dest>.part` sibling and only
os.replace()s it onto the final name once the transfer completes fully -
a process kill mid-download (not a caught exception, which already
cleaned up before this fix too) can then never leave a truncated file
sitting at the name the rest of the code treats as "this archive is
done". These tests exercise the function against small fake response
objects, without needing real aiohttp/HA - just an async iterator with
the same `.content.iter_chunked()` shape and a `hass` stand-in whose
async_add_executor_job() just calls the function synchronously.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from custom_components.google_photos_backup.backends.throttle import (
    throttled_stream_to_file,
)


class _FakeHass:
    async def async_add_executor_job(self, func, *args):
        return func(*args)


class _FakeContent:
    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self._chunks = chunks
        self._fail_after = fail_after

    async def iter_chunked(self, size: int):
        for i, chunk in enumerate(self._chunks):
            if self._fail_after is not None and i == self._fail_after:
                raise ConnectionError("simulated network drop")
            yield chunk


class _FakeResponse:
    def __init__(self, chunks: list[bytes], fail_after: int | None = None) -> None:
        self.content = _FakeContent(chunks, fail_after)


async def test_successful_download_produces_correct_file_with_no_leftover_part(tmp_path: Path):
    dest = tmp_path / "archive.zip"
    resp = _FakeResponse([b"hello ", b"world", b"!" * 20])

    total = await throttled_stream_to_file(resp, dest, _FakeHass(), limit_kbps=0, flush_size=8)

    expected = b"hello world" + b"!" * 20
    assert dest.exists()
    assert dest.read_bytes() == expected
    assert total == len(expected)
    assert not dest.with_name(dest.name + ".part").exists()


async def test_interrupted_download_leaves_no_trace(tmp_path: Path):
    """The core regression test: a failure partway through must leave
    NEITHER a truncated file at the final name NOR an orphaned .part
    file behind."""
    dest = tmp_path / "archive.zip"
    resp = _FakeResponse([b"a" * 20, b"b" * 20, b"c" * 20], fail_after=2)

    with pytest.raises(ConnectionError):
        await throttled_stream_to_file(resp, dest, _FakeHass(), limit_kbps=0, flush_size=8)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


async def test_interrupted_download_does_not_touch_a_preexisting_file_at_the_same_name(
    tmp_path: Path,
):
    """If dest already existed (e.g. from a legitimate prior run) and a
    *new* download to the same path fails, the old file must survive
    untouched - only the .part sibling is affected until the final
    os.replace()."""
    dest = tmp_path / "archive.zip"
    dest.write_bytes(b"previous complete content")
    resp = _FakeResponse([b"new", b"data"], fail_after=1)

    with pytest.raises(ConnectionError):
        await throttled_stream_to_file(resp, dest, _FakeHass(), limit_kbps=0, flush_size=1)

    assert dest.read_bytes() == b"previous complete content"
    assert not dest.with_name(dest.name + ".part").exists()


async def test_download_larger_than_flush_size_spans_multiple_flushes(tmp_path: Path):
    dest = tmp_path / "big.bin"
    chunks = [bytes([i % 256]) * 5 for i in range(10)]  # 10 chunks x 5 bytes = 50 bytes
    resp = _FakeResponse(chunks)

    total = await throttled_stream_to_file(resp, dest, _FakeHass(), limit_kbps=0, flush_size=12)

    assert total == 50
    assert dest.read_bytes() == b"".join(chunks)
