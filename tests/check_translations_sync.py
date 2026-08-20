#!/usr/bin/env python3
"""Fail CI if strings.json and translations/*.json drift apart key-for-key.

strings.json is the source of truth HA falls back to; every translation
file must define exactly the same set of keys (values differ, that's the
whole point of translating them - but a missing/extra key means the
config flow or an entity will silently fall back to the raw key or to
English, which is easy to miss in review otherwise).
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
COMPONENT_DIR = ROOT / "custom_components" / "google_photos_backup"


def flatten_keys(data, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    if isinstance(data, dict):
        for key, value in data.items():
            keys |= flatten_keys(value, f"{prefix}.{key}" if prefix else key)
    else:
        keys.add(prefix)
    return keys


def main() -> int:
    strings_path = COMPONENT_DIR / "strings.json"
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    reference_keys = flatten_keys(strings)

    failed = False
    translations_dir = COMPONENT_DIR / "translations"
    for translation_path in sorted(translations_dir.glob("*.json")):
        translation = json.loads(translation_path.read_text(encoding="utf-8"))
        keys = flatten_keys(translation)

        missing = reference_keys - keys
        extra = keys - reference_keys
        if missing:
            print(f"{translation_path.relative_to(ROOT)}: missing keys: {sorted(missing)}")
            failed = True
        if extra:
            print(f"{translation_path.relative_to(ROOT)}: extra keys not in strings.json: {sorted(extra)}")
            failed = True
        if not missing and not extra:
            print(f"{translation_path.relative_to(ROOT)}: OK ({len(keys)} keys)")

    if failed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
