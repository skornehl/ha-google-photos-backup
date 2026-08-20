"""Regression tests for the tar-extraction path-traversal fix (issue #1).

Verifies _safe_tar_extractall() rejects a "Zip Slip"-style malicious tar
member (path escaping the destination directory) and a symlink/hardlink
member, while still extracting an ordinary, well-formed archive correctly.
The zip path isn't re-tested here - it relies on zipfile's own long-standing
stdlib sanitization, not on anything this integration added.
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from custom_components.google_photos_backup.backends.takeout_backend import (
    _safe_tar_extractall,
)


def _make_tar(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))


def _make_symlink_tar(path: Path, link_name: str, link_target: str) -> None:
    with tarfile.open(path, "w") as tf:
        info = tarfile.TarInfo(name=link_name)
        info.type = tarfile.SYMTYPE
        info.linkname = link_target
        tf.addfile(info)


def test_rejects_path_traversal_member(tmp_path: Path):
    evil_tar = tmp_path / "evil.tar"
    escape_target = tmp_path / "escaped.txt"
    _make_tar(evil_tar, {f"../{escape_target.name}": b"pwned"})

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises((ValueError, tarfile.TarError)):
        with tarfile.open(evil_tar) as tf:
            _safe_tar_extractall(tf, dest)

    assert not escape_target.exists(), "malicious member must never be written outside dest"


def test_rejects_deeply_nested_path_traversal_member(tmp_path: Path):
    evil_tar = tmp_path / "evil.tar"
    _make_tar(evil_tar, {"../../../../../../etc/passwd-lookalike": b"pwned"})

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises((ValueError, tarfile.TarError)):
        with tarfile.open(evil_tar) as tf:
            _safe_tar_extractall(tf, dest)


def test_rejects_symlink_member(tmp_path: Path):
    evil_tar = tmp_path / "evil_symlink.tar"
    _make_symlink_tar(evil_tar, "innocuous.jpg", "/etc/passwd")

    dest = tmp_path / "dest"
    dest.mkdir()

    with pytest.raises((ValueError, tarfile.TarError)):
        with tarfile.open(evil_tar) as tf:
            _safe_tar_extractall(tf, dest)

    assert not (dest / "innocuous.jpg").exists()


def test_extracts_well_formed_archive_correctly(tmp_path: Path):
    good_tar = tmp_path / "good.tar"
    _make_tar(
        good_tar,
        {
            "photos/2026/IMG_0001.jpg": b"fake jpeg bytes",
            "photos/2026/IMG_0001.jpg.json": b'{"photoTakenTime": {"timestamp": "1"}}',
        },
    )

    dest = tmp_path / "dest"
    dest.mkdir()
    with tarfile.open(good_tar) as tf:
        _safe_tar_extractall(tf, dest)

    extracted = dest / "photos" / "2026" / "IMG_0001.jpg"
    assert extracted.read_bytes() == b"fake jpeg bytes"
