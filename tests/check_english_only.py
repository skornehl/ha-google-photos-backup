#!/usr/bin/env python3
"""Fail CI if German text creeps back into the source.

This project was originally written with German comments, log messages and
error strings. Everything except `translations/de.json` is English now, and
`translations/de.json` is the one file that is *supposed* to be German -
it is the German translation.

The check is deliberately narrow: it looks for German-specific letters
(umlauts and sharp s) rather than trying to detect German by vocabulary.
A word list would either miss plenty ("Backup-Lauf fehlgeschlagen" shares
no word with English) or fire on legitimate English text ("die" is a verb,
"war" is a noun, "list" is both). Umlauts have a near-zero false-positive
rate in an English codebase and catch the majority of German that would
realistically be added by someone typing prose.

That means German written without umlauts slips through. It is a tripwire,
not a language detector - review still has to do the rest.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Written as escapes on purpose: spelling the characters out literally here
# would make this file its own first finding.
GERMAN_LETTERS = re.compile(
    "[" + "".join(chr(c) for c in (0xE4, 0xF6, 0xFC, 0xC4, 0xD6, 0xDC, 0xDF)) + "]"
)

SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml"}

# The German translation file, and only that one.
ALLOWED = {ROOT / "custom_components" / "google_photos_backup" / "translations" / "de.json"}

SKIP_DIRS = {".git", ".github/workflows/node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache"}


def iter_files():
    for path in ROOT.rglob("*"):
        if path.suffix not in SUFFIXES or not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path in ALLOWED:
            continue
        yield path


def main() -> int:
    findings: list[str] = []
    for path in iter_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if GERMAN_LETTERS.search(line):
                rel = path.relative_to(ROOT)
                findings.append(f"{rel}:{lineno}: {line.strip()}")

    if findings:
        print("German text found outside translations/de.json:\n")
        for finding in findings:
            print(f"  {finding}")
        print(
            "\nEverything except the German translation file must be English "
            "(comments, log messages, error strings, docs)."
        )
        return 1

    print("English-only check passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
