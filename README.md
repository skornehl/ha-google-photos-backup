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
├── application_credentials.py  # Google OAuth2 endpoints (library_api + takeout's Drive sync)
├── config_flow.py           # backend selection + backend-specific options
├── const.py
├── coordinator.py           # DataUpdateCoordinator, persisted sync state
├── sensor.py                # last_sync, files_backed_up, last_error, free_space
├── services.yaml
├── strings.json / translations/{en,de}.json
└── backends/
    ├── base.py               # BackupBackend ABC, BackupStats, SyncStateStore
    ├── fsutil.py             # shared file/folder/hash logic
    ├── throttle.py            # shared bandwidth-limited HTTP reads (library_api, takeout)
    ├── library_api.py        # Library API (app-owned) + Picker API
    ├── rclone_backend.py     # subprocess wrapper around `rclone`
    └── takeout_backend.py    # Takeout archive import, optional download-link fetch + Drive sync
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

### Bandwidth limiting

Every backend offers a **Bandwidth limit (KiB/s)** field during setup, and
it can be changed afterwards via Settings → Devices & Services → Google
Photos Backup → Configure - no need to re-run the whole setup. `0` (the
default) means unlimited. Useful for a large initial backup (easily a TB+
for a big photo library) so it doesn't saturate the rest of your
connection for hours.

- `library_api`: throttled in Python while streaming each photo/video
  download.
- `rclone`: passed straight through to rclone's own `--bwlimit` flag.
- `takeout`: applies to its own network transfers - downloading archives
  from a pasted email link, and/or from Google Drive if Drive sync is
  enabled (see below). It's a no-op if you only ever drop archives into
  `takeout_watch_dir` manually, since that path never touches the network
  at all.

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
3. Get finished archives into the configured `takeout_watch_dir` - three
   ways, combinable, all optional except the first:
   - Manually (or via your own separate sync step from the cloud
     destination in step 2 - rclone, Nextcloud, whatever you already run).
   - Paste one or more **Takeout email download links** into the
     `takeout_download_links` field (setup or later via Configure) - see
     "Download links" below.
   - Enable **Drive sync** during setup - see "Google Drive sync" below.
4. Config flow: backend `takeout`, target directory, watch directory,
   interval, optionally "delete archive after import".
5. The integration unpacks every new archive, files media
   chronologically into `YYYY/YYYY-MM/` based on the `<file>.json`
   sidecar (`photoTakenTime`), and skips already-imported files
   (SHA-256 hash comparison, consistent across backends).

#### Download links

If you request a Takeout export with delivery method **"Send download
link via email"**, paste the link(s) from that email (one per line) into
`takeout_download_links` - the integration fetches them directly into
`takeout_watch_dir` on the next sync, no manual download needed.

These are meant to be pre-authorized, time-limited URLs (valid ~7 days,
max. 5 downloads each), fetched with a plain HTTPS request - no Google
sign-in performed by this integration. **If Google actually requires an
authenticated browser session for a given link** (this integration
deliberately does not attempt to script a Google login - see "Known
limitations"), the download fails with a clear error on the sensor
instead of silently saving the resulting HTML login page as if it were an
archive; fall back to downloading it yourself and dropping it into
`takeout_watch_dir` in that case. A successfully downloaded link is
remembered (won't be re-fetched); failed ones are retried on the next
sync automatically since links commonly used up mid-way just need one
more attempt.

#### Google Drive sync

Continuous alternative to manually placing archives: enable **"Enable
Google Drive sync"** when choosing the `takeout` backend. This adds a
Google sign-in step (separate from `library_api`'s, but can reuse the
same Application Credentials/Google Cloud project - see step 0 below) and
then polls Google Drive on every sync for files named `takeout-*`
(Takeout's own naming, e.g. `takeout-20250801T000000Z-001.zip`),
optionally restricted to one Drive folder ID, and downloads new ones
straight into `takeout_watch_dir`.

0. In the same Google Cloud project as step 1 above, enable the **Google
   Drive API**, and add both the `drive.readonly` and `drive.metadata`
   scopes to the OAuth consent screen's scope list (Data access) - same
   place you added the Photos scopes for `library_api`, if you've set
   that up. If you're using `takeout` standalone, you still need a Google
   Cloud project + OAuth client (Application Credentials) for this,
   exactly like `library_api` step 1-4, just with these two Drive scopes
   instead of the Photos scopes. Both scopes are requested together as
   soon as Drive sync is enabled (regardless of whether you turn on the
   delete option below) - `drive.metadata` grants managing metadata
   (rename/trash/delete) for **every** file in your Drive, not just
   Takeout archives; this integration only ever touches files it
   downloaded itself, but the OAuth grant itself isn't scoped that
   narrowly (Google doesn't offer a "delete only files matching a
   pattern" scope). `drive.readonly` similarly grants read access to your
   whole Drive, needed to actually download archive contents.
1. Config flow: backend `takeout` → "Enable Google Drive sync" → sign in
   with your Google account → target directory, watch directory,
   interval, delete-after-import, download links, Drive folder ID
   (optional, empty = all of My Drive), and the two Drive-cleanup options
   below.
2. Set up "Scheduled exports" (see step 2 above) with **Drive** as the
   destination - this backend then picks up every new export
   automatically, no external sync tool needed at all.

Drive sync is a setup-time choice - to add it to an existing plain
`takeout` instance, remove and re-add the integration with the toggle
checked (same limitation as changing backend type).

##### Cleaning up archives in Drive after import

"Scheduled exports" keep piling up in Drive every 2 months forever unless
something removes the old ones - which matters if you don't want a
recurring export saturating your Drive quota (or your connection, see
"Bandwidth limiting" above, re-downloading multi-GB archives on every
sync isn't what happens here since already-downloaded files are
remembered, but the *quota* problem is separate from that).

Two independent options, both off by default, only shown when Drive sync
is enabled:

- **Delete archive from Drive after successful import**: once an archive
  downloaded via Drive sync has been *successfully imported* (not just
  downloaded - a failed extraction leaves it in Drive so nothing is
  lost), it gets moved to Drive's trash.
- **Delete permanently instead of moving to trash**: skips the trash
  entirely. Trash still counts against your Drive quota until it's
  emptied (manually, or automatically after ~30 days) - if the whole
  point is freeing up quota immediately for the next scheduled export,
  you need this option on too. **This is not recoverable** - only enable
  it once you've confirmed a few sync runs actually produced correct,
  complete local backups.

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
2. Paste the resulting email link(s) into `takeout_download_links` (see
   "Download links" above) - or download them yourself within the 7 days
   and place them into `takeout_watch_dir` if a link needs a browser
   session this integration can't provide.
3. **Only then** switch to "Scheduled exports" (Drive), optionally with
   Drive sync enabled (see above) - according to Google, that then only
   transfers *new/changed* data since the last export, so for most
   libraries just a few GB per run instead of the full size.

Google Takeout doesn't allow selecting by album or time range for
Photos - a one-time export is always the complete library.

**Limitation:** no real-time sync - depends on how often exports are
generated (Drive sync/download links still just react to what already
exists; they don't make Google generate exports faster). No automatic
rewriting of EXIF tags (only filesystem mtime/folder structure are
derived from the JSON).

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
- No automated *triggering* of a Takeout export via browser automation -
  deliberately not implemented (fragile, potentially violates Google's
  terms of service for automated access to the web UI). Download links
  and Drive sync only *fetch* exports that already exist (created by you
  manually, or by Takeout's own "Scheduled exports" feature) - neither
  scripts a Google login or the Takeout web UI itself.
- Download links only work if Google serves the archive to a plain,
  unauthenticated HTTPS request; if a given link actually requires a
  logged-in browser session, the fetch fails with a clear error rather
  than silently misbehaving - see "Download links" above.

## Development

```bash
pip install -r requirements_test.txt
pytest tests/ -v
```

CI (`.github/workflows/ci.yml`) runs on every pull request: Python syntax +
JSON validity, `strings.json`/translations key-sync check,
[hassfest](https://developers.home-assistant.io/docs/creating_integration_manifest/#hassfest)
(the official HA integration validator), `ruff` lint, and the pytest suite
against Python 3.12 and 3.13.
