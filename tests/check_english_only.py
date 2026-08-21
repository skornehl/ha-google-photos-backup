#!/usr/bin/env python3
"""Fail CI if German text creeps back into the source.

This project was originally written with German comments, log messages and
error strings. Everything except `translations/de.json` is English now, and
that file is the one that is *supposed* to be German - it is the German
translation.

Two signals, because either one alone has a blind spot:

1. German-specific letters (umlauts, sharp s). Near-zero false positives in
   an English codebase, but blind to German written without them. In this
   repo that gap was real, not theoretical: `"lauf_aktiv"`, `"rclone
   Aufruf: %s"`, `"Unbekanntes Backend"` and `"Sidecar %s ohne verwertbaren
   Zeitstempel"` all sailed past an umlaut-only check.

2. A curated list of German function words. Every entry was picked because
   it is *not* also an English word - that rules out the obvious traps
   ("die", "war", "man", "hat", "list" are all English too) and keeps the
   false-positive rate at zero for this codebase. Matched case-insensitively
   on word boundaries, so `und` does not fire inside `refund`.

Neither signal is a language detector. German content words with no umlaut
and no function word nearby ("Backup Sync Status") still get through, and
that is accepted: this is a tripwire for prose, not a proof of absence.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Built from code points rather than spelled out: writing the characters
# literally would make this file its own first finding.
GERMAN_LETTERS = re.compile(
    "[" + "".join(chr(c) for c in (0xE4, 0xF6, 0xFC, 0xC4, 0xD6, 0xDC, 0xDF)) + "]"
)

# German function words that are not also English words. Deliberately short:
# a longer list buys little and every added entry is a false-positive risk.
GERMAN_WORDS = (
    "nicht", "kein", "keine", "keinen", "keiner",
    "wird", "werden", "wurde", "wurden",
    "muss", "kann", "koennen", "soll", "sollen",
    "damit", "weil", "wenn", "dass", "denn",
    "oder", "aber", "ohne", "auch", "noch", "schon", "sowie",
    "sind", "eine", "einen", "einer", "eines",
    "nach", "sich", "durch", "beim", "vom", "zum", "zur",
    "fuer", "ueber", "unter", "gegen",
    "unbekannt", "unbekannte", "unbekanntes",
    "datei", "dateien", "fehler", "ordner", "verzeichnis",
    "aufruf", "zeitstempel", "lauf", "laeuft",
    "erneut", "bereits", "vorhanden", "vorhandene", "gefunden", "abgeschlossen",
    "loeschen", "geloescht", "pruefen", "auswahl", "groesse",
    # Past participles and prose words that got past an earlier version of
    # this list. Extended whenever something slips through in review - the
    # list is empirical, not a grammar.
    "fehlgeschlagen", "abgebrochen", "gedrosselt", "gemeldet", "freigegeben",
    "gestartet", "beendet", "uebersprungen", "heruntergeladen", "laufenden",
    "statt", "wenig", "bekam", "denselben", "zweiter", "verwertbaren",
    "rohliste", "kennzahl",
)
GERMAN_WORD_RE = re.compile(r"\b(" + "|".join(GERMAN_WORDS) + r")\b", re.IGNORECASE)

SUFFIXES = {".py", ".md", ".json", ".yaml", ".yml", ".toml"}

# The German translation file, and this checker - which necessarily contains
# German, since the word list and the docstring examples *are* German.
ALLOWED = {
    ROOT / "custom_components" / "google_photos_backup" / "translations" / "de.json",
    pathlib.Path(__file__).resolve(),
}

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache"}


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
            hit = None
            if GERMAN_LETTERS.search(line):
                hit = "german letter"
            else:
                match = GERMAN_WORD_RE.search(line)
                if match:
                    hit = f"german word {match.group(0)!r}"
            if hit:
                rel = path.relative_to(ROOT)
                findings.append(f"{rel}:{lineno}: [{hit}] {line.strip()}")

    if findings:
        print("German text found outside translations/de.json:\n")
        for finding in findings:
            print(f"  {finding}")
        print(
            "\nEverything except the German translation file must be English "
            "(comments, log messages, error strings, docs).\n"
            "If a match is a genuine English false positive, adjust GERMAN_WORDS "
            "rather than working around the check."
        )
        return 1

    print(f"English-only check passed ({len(list(iter_files()))} files scanned).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
