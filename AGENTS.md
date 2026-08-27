# AGENTS.md

Working notes for this repository. Read before changing anything.

## What this is

A Home Assistant **custom integration** (HACS) that copies a Google Photos
library onto a local disk. Not a general-purpose library — it runs inside a
live Home Assistant instance, so a bad release breaks someone's backups
silently.

## Conventions

- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`,
  `ci:`, `release:`). Subject in the imperative, no trailing period.
- **Branches:** `<type>/<slug>`, e.g. `docs/google-cloud-console-2026-ui`,
  `feat/20-concurrent-downloads`, `ci/mypy-stricter`.
- **Never push to `master`.** Always a branch plus a pull request; PRs are
  squash-merged with `(#NN)` appended by GitHub.
- **Authorship:** commits are authored by Sebastian Kornehl
  `<skornehl@users.noreply.github.com>`, with
  `Co-Authored-By: Claude <noreply@anthropic.com>` in the body when an agent
  did the work.
- **Language:** the entire source, comments and docs are **English**
  (see #69). The only German is `translations/de.json`.

## The one thing to get right: Google's API reality

Google removed the `photoslibrary`, `photoslibrary.readonly` and
`photoslibrary.sharing` scopes on **2025-03-31**. Since then **no** app can
read a user's pre-existing library through the Photos API. This is not a bug
to work around, it is the constraint the whole architecture follows:

| Backend | Sees | OAuth |
|---|---|---|
| `library_api` | only what this integration uploaded, plus manual Picker selections | yes — two Photos scopes |
| `rclone` | same limitation, via rclone's own remote | none from us |
| `takeout` | **the complete library** | only if Drive sync is enabled |

`takeout` is the recommended backend and the only one that delivers a real
backup. Do not "improve" the others into something they cannot be.

**Takeout is incremental since June 2026** for Google Photos: the first
export is the full library, later scheduled exports contain only new or
changed items. Up to six exports, one every two months, then the schedule
must be recreated. Requires Google Photos to be the *only* product selected.

**`async_step_takeout_drive_choice` decides whether OAuth happens at all** —
it deliberately asks about Drive sync *before* the sign-in step, because that
routing cannot be decided later inside a plain form. Plain Takeout needs no
Google Cloud project whatsoever. Keep that property; it is the difference
between a ten-minute setup and a Cloud Console session.

## Docs rot faster than code here

Google renames Cloud Console UI elements regularly. The former *"OAuth consent
screen"* is now **Google Auth Platform** (Branding / Audience / Data access /
Clients / Verification center). When touching setup instructions:

- verify against the **live console**, not from memory — this has already
  produced one wrong claim that a user caught
- record the **date of verification** in the text
- name the scope **categories** (non-sensitive / sensitive / restricted),
  because that is what decides whether Google demands a review;
  `drive.readonly` is restricted, the strictest tier
- state that publishing status **Testing** avoids verification entirely
  (100 test-user cap), since that is what keeps a private setup out of a
  review process

## When changing dialogs

`strings.json`, `translations/en.json` and `translations/de.json` must stay
**key-identical**. Check with a set comparison of flattened key paths after
every edit — a missing key shows up as a raw identifier in the user's UI.
`en.json` is normally a copy of `strings.json`.

## Safety-relevant defaults

Anything that deletes data is **off by default** and must stay that way:
`takeout_delete_after_import`, `takeout_drive_delete_after_sync`,
`takeout_drive_delete_permanently`. Permanent Drive deletion is not
recoverable — the README says so explicitly and that warning must survive
edits.

## Development

`pyproject.toml` holds ruff/mypy config; `requirements_test.txt` the test
deps. CI runs ruff, mypy (moving towards `--strict`, see #67) and pytest.
Run them before opening a PR.
