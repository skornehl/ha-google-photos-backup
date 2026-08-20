"""Shared interface every backup backend implements."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


@dataclass
class BackupStats:
    """Result of a single backup run, surfaced on the sensors."""

    files_downloaded: int = 0
    files_skipped: int = 0
    bytes_downloaded: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: "BackupStats") -> None:
        self.files_downloaded += other.files_downloaded
        self.files_skipped += other.files_skipped
        self.bytes_downloaded += other.bytes_downloaded
        self.errors.extend(other.errors)


class SyncStateStore:
    """Thin wrapper around the persisted per-entry sync state.

    Kept intentionally dict-based (rather than a strict schema) because each
    backend persists different things (processed media IDs vs. file hashes
    vs. a pending picker session). The coordinator owns the actual
    homeassistant.helpers.storage.Store instance and calls async_save()
    after every run; backends only ever touch the in-memory dict returned by
    `data`.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    @property
    def processed_hashes(self) -> set[str]:
        return set(self.data.setdefault("processed_hashes", []))

    def add_processed_hash(self, digest: str) -> None:
        hashes: list[str] = self.data.setdefault("processed_hashes", [])
        if digest not in hashes:
            hashes.append(digest)


class BackupBackend(ABC):
    """Common interface for library_api / rclone / takeout backends."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.state = state

    @abstractmethod
    async def async_validate(self) -> None:
        """Raise homeassistant.exceptions.ConfigEntryNotReady / ValueError.

        Called once during async_setup_entry. Must check everything needed
        for the backend to function (binary present, directories writable,
        credentials valid, ...) and raise a clear, translatable error if
        not.
        """

    @abstractmethod
    async def async_run_backup(self) -> BackupStats:
        """Perform one backup pass and return stats for the sensors."""
