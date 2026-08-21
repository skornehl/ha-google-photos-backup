"""Shared interface every backup backend implements."""
from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import Callable
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

    #: Set while a run is in progress, cleared when it finishes. Lets the
    #: sensors say "still working" instead of looking stalled during a long
    #: initial import (issue #21).
    in_progress: bool = False

    def merge(self, other: BackupStats) -> None:
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
    def processed_hashes(self) -> builtins.set[str]:
        # builtins.set, not set: this class defines a method named `set`,
        # which shadows the builtin inside the class body - a bare
        # `set[str]` annotation here resolves to SyncStateStore.set.
        return builtins.set(self.data.setdefault("processed_hashes", []))

    def add_processed_hash(self, digest: str) -> None:
        hashes: list[str] = self.data.setdefault("processed_hashes", [])
        if digest not in hashes:
            hashes.append(digest)


class BackupBackend(ABC):
    """Common interface for library_api / rclone / takeout backends."""

    #: OAuth2 scopes this backend needs, or None if it doesn't use OAuth
    #: at all. Declared here (rather than as an `if backend == ...` chain
    #: inside config_flow's extra_authorize_data) so a new OAuth-using
    #: backend only has to state its own scopes on its own class - see
    #: config_flow.GooglePhotosBackupFlowHandler.extra_authorize_data,
    #: which reads this via `scopes_for_backend()` below.
    oauth_scopes: list[str] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        state: SyncStateStore,
        on_progress: Callable[[BackupStats], None] | None = None,
    ) -> None:
        self.hass = hass
        self.entry = entry
        self.state = state
        #: Invoked by a backend to publish intermediate BackupStats while a
        #: run is still going. The coordinator passes a callback that pushes
        #: them to the sensors; when it's None (tests, direct use) reporting
        #: is simply a no-op, so backends never have to check.
        self._on_progress = on_progress

    def _report_progress(self, stats: BackupStats) -> None:
        """Publish intermediate stats. Cheap and safe to call often.

        Deliberately one-directional: the backend hands data outward and
        never reads coordinator state, so the dependency arrow this
        architecture avoids stays pointing the right way.
        """
        if self._on_progress is not None:
            self._on_progress(stats)

    def _option(self, key: str, default: Any = None) -> Any:
        """Read a config value, preferring an options-flow override (set
        after initial setup, e.g. via the "..." menu on the integration)
        over the value collected during the original config flow."""
        return self.entry.options.get(key, self.entry.data.get(key, default))

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

    async def async_terminate(self) -> None:
        """Stop whatever this backend might currently have in flight
        (e.g. a subprocess) - called on unload/reload so nothing is left
        running detached from HA. No-op by default; only RcloneBackend
        currently has something to actually terminate."""
