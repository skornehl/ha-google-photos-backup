# Google Photos Backup (Home Assistant Custom Component)

Automatic backup of Google Photos/videos to a disk mounted locally in Home
Assistant. Installable via HACS (custom repository).

> **Important, read first:** On March 31, 2025, Google significantly
> restricted the Google Photos Library API. `mediaItems.list`/
> `mediaItems.search` now only return **media that was created/uploaded by
> the calling app itself** - no longer a user's complete, pre-existing
> library. This affects *all* third-party tools that go through this API,
> including `rclone`. This integration was deliberately built with
> **multiple layers** so you can still get a complete backup - make sure to
> read the comparison table below before choosing a backend.

## Architecture

```
custom_components/google_photos_backup/
├── __init__.py              # async_setup_entry/async_unload_entry, services
├── application_credentials.py  # Google OAuth2 endpoints (library_api only)
├── config_flow.py           # backend selection + backend-specific options
├── const.py
├── coordinator.py           # DataUpdateCoordinator, persisted sync state
├── sensor.py                # last_sync, files_backed_up, last_error, free_space
├── services.yaml
├── strings.json / translations/{en,de}.json
└── backends/
    ├── base.py               # BackupBackend ABC, BackupStats, SyncStateStore
    ├── fsutil.py             # shared file/folder/hash logic
    ├── library_api.py        # Library API (app-owned) + Picker API
    ├── rclone_backend.py     # subprocess wrapper around `rclone`
    └── takeout_backend.py    # Takeout archive import
```

Each backend implements the same `BackupBackend` interface
(`async_validate()`, `async_run_backup() -> BackupStats`). The
`GooglePhotosBackupCoordinator` (a `DataUpdateCoordinator`) doesn't know
about backend details - it only loads/saves the persisted sync state
(`homeassistant.helpers.storage.Store`) and calls
`backend.async_run_backup()` on a fixed interval. The config flow decides
at setup time which backend class gets instantiated
(`backends/__init__.py: async_create_backend`). New backends can be added
without touching the coordinator/sensors/services.

## Backend comparison

| | **library_api** | **rclone** | **takeout** |
|---|---|---|---|
| Coverage of an existing library | ❌ only what this integration itself uploads + manual per-session Picker selection | ❌ since March 2025, only what rclone itself has uploaded | ✅ complete (that's the whole point of Takeout) |
| Automation level | Medium (Picker requires manual user interaction per selection session) | High (unattended, but empty of content for existing photos) | Medium (Google auto-generates exports every 2 months, but delivery into the watch directory is a separate step) |
| EXIF/metadata fidelity | Original file straight from Google (`=d`/`=dv`) | Original file straight from Google | File + JSON sidecar; capture date/mtime is taken from the JSON, embedded EXIF is not rewritten |
| Setup effort | Google Cloud project + OAuth consent screen + application credentials | rclone binary + `rclone.conf` + OAuth in the rclone context | Just a watch directory; manual Google Takeout export or "scheduled exports" |
| Dependency on external binaries | None | `rclone` must be provided separately (not included in HA OS) | None |
| Resilience to future Google API changes | Low (API surface can be restricted further at any time) | Low (same API under the hood) | High (Takeout is a user right under GDPR/data portability, not an API product Google can arbitrarily cut off) |

### Recommendation

**For a complete, durable backup of an existing library, the `takeout`
backend is the only option that can actually deliver that under the
current API reality.** `library_api` and `rclone` are fully implemented
and work correctly for what they can see - but structurally that's not
"the whole library", only what was uploaded through the respective OAuth
client itself or manually selected via a Picker session.

For the most automated flow possible anyway: Google Takeout offers
*"Scheduled exports"*, which can automatically generate a new Google
Photos export every 2 months for a year and deliver it directly to Google
Drive/Dropbox/OneDrive/Box. A second sync step, independent of this
integration (e.g. a dedicated rclone Drive remote or Nextcloud), fetches
the archives from there into `takeout_watch_dir` - from that point on,
this integration fully automatically handles unpacking, matching, and
filing. `library_api` (via the Picker service) works well as a complement,
to quickly back up recent photos on demand without waiting for the next
2-month export.

## Setup

### Common: install via HACS

1. HACS → Custom repositories → add `https://github.com/skornehl/ha-google-photos-backup`, category "Integration".
2. Install `Google Photos Backup`, restart Home Assistant.
3. Settings → Devices & Services → Add Integration → "Google Photos
   Backup" → choose backend.

### Backend 1: library_api (Google Cloud OAuth)

1. Create a project in the [Google Cloud Console](https://console.cloud.google.com/),
   enable the **Photos Library API** and **Photos Picker API**.
2. Configure the OAuth consent screen (External, test user = your Google
   account, as long as the app isn't verified).
3. Create an OAuth client ID (type "Web Application"), enter the redirect
   URI Home Assistant suggests under Settings → Application Credentials as
   the redirect URI (`https://YOUR_HA/auth/external/callback`).
4. In HA: Settings → Application Credentials → Add → domain
   `google_photos_backup`, enter client ID/secret.
5. Start the config flow, choose backend `library_api`, authorize your
   Google account, set the target directory + interval.
6. To actually get photos from your **existing** library: call the
   `google_photos_backup.start_picker_session` service, open the link from
   the notification, select photos/albums. The next sync (or
   `backup_now`) downloads the selection.

**Limitation:** without a manual Picker selection, practically nothing
happens here - see comparison table.

### Backend 2: rclone

1. `rclone` must be available inside the HA container/host - it is **not**
   part of Home Assistant OS. Options: a custom add-on that provides
   `rclone` and exposes the binary path via a bind mount, or a customized
   core image. Without it, `async_validate()` fails during setup with a
   clear error message.
2. On a machine with a browser: run `rclone config`, choose the
   `google photos` backend, go through the OAuth flow. Copy the resulting
   `rclone.conf` to the HA host (specify the path in the config flow, or
   use the default path).
3. Config flow: backend `rclone`, remote name (as in `rclone.conf`),
   source path (default `media/by-month`), destination, interval.

**Limitation:** since March 2025, only returns files that were uploaded
through this exact rclone remote - see rclone docs
(`rclone can only download photos it uploaded`).

### Backend 3: Takeout (recommended)

1. Open [Google Takeout](https://takeout.google.com/), select only "Google
   Photos", export format `.zip`, limit size as needed (smaller files are
   easier to handle).
2. Optional but recommended: set up a recurring backup under "Scheduled
   exports" (every 2 months, 1 year) and choose Drive/Dropbox/OneDrive/Box
   as the destination.
3. Place finished archives into the configured `takeout_watch_dir`
   (manually, or automated via a separate sync from the cloud destination
   in step 2 - that is deliberately not part of this integration).
4. Config flow: backend `takeout`, target directory, watch directory,
   interval, optionally "delete archive after import".
5. The integration unpacks every new archive, files media
   chronologically into `YYYY/YYYY-MM/` based on the `<file>.json`
   sidecar (`photoTakenTime`), and skips already-imported files
   (SHA-256 hash comparison, consistent across backends).

#### Large libraries: first full export without Drive storage

"Scheduled exports" *always* deliver to Drive/Dropbox/OneDrive/Box - which
requires enough free space there for the **entire** library (e.g. ~1TB
free for 1TB of photos), since the first run exports everything. If your
quota isn't enough:

1. For the **first** export, choose a **one-time** Takeout export with
   delivery method **"Send download link via email"** instead (not "Add
   to Drive") - according to Google, this does **not** count against your
   storage quota, since the archives are only made available for direct
   download for a limited time (~7 days, max. 5 downloads per archive).
   Choose a 50GB archive size to keep the number of files small.
2. Download all archives within the 7 days and place them into
   `takeout_watch_dir`.
3. **Only then** switch to "Scheduled exports" (Drive) - according to
   Google, that then only transfers *new/changed* data since the last
   export, so for most libraries just a few GB per run instead of the
   full size.

Google Takeout doesn't allow selecting by album or time range for
Photos - a one-time export is always the complete library.

**Limitation:** no real-time sync - depends on how often exports are
generated and delivered into the watch directory. No automatic rewriting
of EXIF tags (only filesystem mtime/folder structure are derived from the
JSON).

## Services

- `google_photos_backup.backup_now(config_entry_id)` - trigger an
  immediate sync run.
- `google_photos_backup.start_picker_session(config_entry_id)` -
  `library_api` only; starts a Picker session and notifies with the
  selection link.

## Known limitations / open items

- No restore functionality, backup direction only.
- `library_api`/`rclone` are deliberately fully implemented (the spec
  calls for them), even though their practical value for a full backup is
  low under Google's current API policy - this could change if Google
  relaxes the restrictions again.
- Takeout doesn't rewrite EXIF back into the image file (only
  folder/mtime); usually irrelevant for camera originals, since the
  camera already sets EXIF correctly.
- No automated Takeout triggering via browser automation - deliberately
  not implemented (fragile, potentially violates Google's terms of
  service for automated access to the web UI).
