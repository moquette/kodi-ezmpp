# Master Plan: one universal UI/feedback library (`ui.py`)

Status: history as of 2026-09-26. Stages A, B and C are on `main` since 2026-07-01 (the
branch `ui-consistency-stage-a` no longer exists) and Stage D shipped 2026-07-13 in
`aef9bea`: `buildInstaller`, `BUILDS` and `install_build` are gone from the source (grep
finds none). Nothing below is open. The original status line follows as written.

Status: **BUILT. Stages A, B, and C are implemented on branch `ui-consistency-stage-a`
(135 tests green). Stage D (buildInstaller/BUILDS removal) is deferred to a separate PR as
planned. The "v2" section below is the binding spec that was followed.**

Implemented (branch `ui-consistency-stage-a`, off `main`):

- Stage A: `ui.py` (Progress + copy_with_progress + dialog helpers + one restart) + test_ui.py
  - write-capable conftest fakes + `dropbox_remote.download` cancel (was a no-op).
- Stage B: CreateZip file-walk gauge + SMB/NFS **atomic** ship (`copy_with_progress`),
  VfsCopyError unified into ui, restore extract/staging gauges, Dropbox upload+download
  gauges via `as_dropbox_callback` (activates download-cancel), maintenance notifications
  (fixed the `Maintenance` wrong-name), FRESHSTART, control/tools headings; dead code removed
  (killxbmc, xml_data_advSettings_old/New, orphaned progressDialog).
- Stage C: `onetap.apply` wipes then restores via `wiz.restore(..., post_wipe=True)` - a single
  UNINTERRUPTIBLE unit that always reaches the restart prompt (a wiped box is never stranded).
  (Historical: One-Tap Restore, and with it `onetap.apply`, was removed in v2026.07.21.1.
  Only its wipe engine remains in `onetap.py`, now driven by Fresh Start and `wiz.restore`.)
- Deferred (separate PR): Stage D buildInstaller/BUILDS/install_build removal (live routes,
  pulls in skinswap/downloader - a feature removal, out of this initiative's no-logic rule).
- Not migrated (out of scope, unchanged): the Dropbox QR/auth flow and speedtest dialog.

## Goal

Every user-facing dialog and progress gauge in EZ Maintenance++ currently lives inline in
each path, so consistency depends on discipline (which failed). This plan introduces ONE
module, `resources/lib/modules/ui.py`, that every path calls for ALL feedback: progress,
dialogs, cancel, restart. Consistency becomes **structural** - there is exactly one place
to get it right, and every path (backup / restore / compress / extract / upload / download,
across Local / SMB-NFS / Dropbox, in both the regular and One-Tap flows) routes through it.

## The problem (from the 3-agent QA audit)

Matrix of the current state (path x phase). 🔴 = bug, 🟠 = inconsistency.

| Path                  | Compress | Ship/Upload                                | Download/Copy-in                                | Extract | Cancel               | Restart       |
| --------------------- | -------- | ------------------------------------------ | ----------------------------------------------- | ------- | -------------------- | ------------- |
| Backup Local          | gauge    | -                                          | -                                               | -       | compress only        | -             |
| Backup SMB/NFS        | gauge    | 🔴 none (silent `xbmcvfs.copy` wiz.py:720) | -                                               | -       | 🔴 ship uncancelable | -             |
| Backup Dropbox        | gauge    | gauge+cancel (wiz.py:334-346)              | -                                               | -       | full                 | -             |
| Restore Local         | -        | -                                          | none (direct)                                   | gauge   | extract              | Restart/Later |
| Restore SMB/NFS       | -        | -                                          | 🔴 none (silent copy wiz.py:546)                | gauge   | extract              | Restart/Later |
| Restore Dropbox       | -        | -                                          | gauge, 🔴 cancel=no-op (wiz.py:494)             | gauge   | extract              | Restart/Later |
| One-Tap Local/SMB/NFS | -        | -                                          | 🔴 static "Please wait" (onetap.py:389-395,432) | gauge   | 🔴 cancel BRICKS     | Restart/Later |
| One-Tap Dropbox       | -        | -                                          | gauge, 🔴 cancel=no-op (onetap.py:436)          | gauge   | 🔴 cancel BRICKS     | Restart/Later |

Three root problems (two are real bugs):

1. **Cancel is broken and in one case dangerous.**
   - Download "Cancel" is a NO-OP in every download path (`wiz.py:494-501`, `onetap.py:436-445` never check `iscanceled()`; `dropbox_remote.download` swallows callback exceptions at `dropbox_remote.py:688-692`).
   - SMB/NFS copies (`xbmcvfs.copy`, `wiz.py:546,720`, `onetap.py:389-395`) cannot be canceled at all.
   - 🔴 **Cancelling One-Tap's extract AFTER the wipe bricks the box**: `_wipe` runs at `onetap.py:473`, then a cancel during extract (`wiz.py:760-761`) returns via "Restore Canceled" (`wiz.py:602`) leaving a wiped box, partial restore, and no restart prompt.
2. **Gauge coverage is patchy.** Dropbox up/down have live percent+MB gauges; every SMB/NFS/local copy is silent or a static "Please wait" (looks frozen on a slow share). The compress `DialogProgress` in `CreateZip` (`wiz.py:649`) is never `.close()`d and STICKS on an empty/all-excluded folder (`ZeroDivisionError` swallowed at `wiz.py:675,708`). The regular extract gauge (`wiz.py:590`) is never explicitly closed. Download gauge shows 0% forever if Dropbox omits the size header.
3. **Dialog drift.** 5 separate title constants + `'Maintenance'` as the wrong app name (`maintenance.py:154,174`); THREE restart mechanisms where the build-install path SAYS "restarting" but only runs `LoadProfile` (`wiz.py:834` - a lie); weak/absent wipe warning on the legacy `restore()` confirm (`wiz.py:529` bare "Yes/No"); `default.py:584` silently wipes on "Yes"; case/punctuation drift ("Restore Canceled" vs "Backup canceled").

## The standard: `ui.py` API (the "one universal language")

### Heading

`HEADING = "EZ Maintenance++"` - the single source of truth. Delete the 5 duplicate
`AddonTitle`/`ADDON`/hardcoded copies; point `control.py` dialog defaults at it too.

### Progress - one gauge for every long operation

```python
with ui.Progress("Restoring from Dropbox", cancelable=True) as p:
    p.bytes(received, total)      # -> "42%   130 of 310 MB"   (download/upload/copy)
    p.items(done, total, extra)   # -> "42%   1,204 of 2,880   file.xml"  (zip/unzip)
    if p.cancelled(): ...         # the ONE cancel check
# __exit__ ALWAYS closes -> fixes every never-closed / stuck dialog
```

- Context manager -> guaranteed `.close()` (kills the stuck-dialog class of bug).
- Seeds `update(0, ...)` on create to avoid Kodi's default 100% flash.
- `total == 0` -> indeterminate body (MB counts up, bar not stuck at a false 0%).
- `cancelable=False` disables the cancel affordance for past-the-point-of-no-return phases.

### Chunked VFS copy - the SMB/NFS fix

```python
ui.copy_with_progress(src, dst, phase="Copying to network share", cancelable=True) -> bool
```

Reads `src` / writes `dst` in ~1 MB chunks via `xbmcvfs.File`, driving a `Progress` gauge,
honoring cancel, and deleting the partial file on failure/cancel. This gives SMB/NFS/local
copies the SAME live gauge + cancel that Dropbox already has. Replaces the bare, silent,
uncancelable `xbmcvfs.copy` in backup-ship and restore-staging.

### Dialogs - one set

```python
ui.confirm(msg, yes="Yes", no="No")   # non-destructive yes/no
ui.confirm_wipe(msg)                   # destructive: explicit "this WIPES ... cannot be undone", Wipe/Cancel
ui.error(msg)   ui.done(msg)   ui.notify(msg)   ui.choose(heading, options)
ui.ask_restart(reason="")              # "Restart now / Later" -> xbmc.executebuiltin("Quit")
```

All use `HEADING`. Every destructive path uses `confirm_wipe`. Every restart uses
`ask_restart` (retire `LoadProfile` "reset").

### Cancel model (the safety rules)

- **Download must honor cancel**: the progress callback returns `False` on cancel;
  `dropbox_remote.download` checks the return and aborts (mirroring `upload`).
- **No cancel after the point of no return**: One-Tap's post-wipe extract uses
  `cancelable=False`, so the box can never be left wiped-and-half-restored.
- **Copies are cancelable** via `copy_with_progress`.

## Migration map (feedback layer only - NO logic changes)

| File                | Changes                                                                                                                                                                                                                                                                                                                                                                     |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `ui.py` (NEW)       | The library above + `test_ui.py`.                                                                                                                                                                                                                                                                                                                                           |
| `wiz.py`            | `backup` -> `copy_with_progress` for the SMB/NFS ship; `CreateZip` -> `Progress` (guaranteed close, empty-folder guard); `_backup_dropbox`/`_restore_dropbox` -> `ui.Progress` gauges; `restore` -> `Progress` extract + `copy_with_progress` staging + `ui.ask_restart`; `restoreFolder` -> `ui.choose`; all `dialog.ok/yesno` -> `ui.*`; download callback honors cancel. |
| `dropbox_remote.py` | `download` honors a canceling callback; dialogs -> `ui.*`; heading -> `ui.HEADING`.                                                                                                                                                                                                                                                                                         |
| `onetap.py`         | `apply` -> `Progress` fetch (bytes for Dropbox, `copy_with_progress` for VFS), post-wipe extract `cancelable=False`, `ui.confirm_wipe`, `ui.ask_restart`; all dialogs -> `ui.*`.                                                                                                                                                                                            |
| `tools.py`          | buffer dialogs -> `ui.choose/confirm/error/ask_restart`, `ui.HEADING`.                                                                                                                                                                                                                                                                                                      |
| `default.py`        | `FRESHSTART` -> `ui.confirm_wipe` + `ui.ask_restart`; dialogs -> `ui.*`.                                                                                                                                                                                                                                                                                                    |
| `maintenance.py`    | notifications -> `ui.notify` (fixes the `'Maintenance'` heading).                                                                                                                                                                                                                                                                                                           |
| `control.py`        | dialog default headings -> `ui.HEADING`.                                                                                                                                                                                                                                                                                                                                    |

## Dead code to remove (reduces surface, kills worst offenders)

- `killxbmc()` (`default.py` - no callers; worst heading offender).
- The legacy wizard: `buildInstaller`, `BUILDS`, `install_build` routing (replaced by One-Tap).
- `xml_data_advSettings_old/New` (`tools.py` - unused since the JSON-RPC buffer fix).

## Testing strategy

- `test_ui.py`: `Progress` body formatting (`bytes`/`items`, `total==0`), `_pct`,
  `copy_with_progress` (chunked copy over a fake VFS, cancel mid-copy, partial-cleanup on
  failure), and that dialog helpers use `HEADING`.
- Update existing `test_*` where migrated call sites change.
- Full suite green after EACH file's migration (not just at the end).

## Rollout / risk

- Build + test `ui.py` first; migrate path-by-path; one coherent release at the end.
- **No restore/backup LOGIC changes** - only the feedback layer.
- Highest behavioral-change risk: `copy_with_progress` (chunked VFS read/write) must be
  verified on the real device over SMB/NFS. Fallback: if a chunked copy errors, fall back to
  `xbmcvfs.copy` so we never regress a working copy.

## v2 - Review-mandated changes (QA + Architecture; BINDING)

Both reviews returned APPROVE-WITH-CHANGES; neither required a redesign. Both affirmed the
`ui.py` thesis and the "feedback-layer-only, suite-green-per-file" discipline. The changes
below are folded in and are binding. (Open questions from v1 are answered here.)

### Progress

- ONE class, two body formatters (`bytes`, `items`) - do NOT split.
- `cancelled()` is memoized + idempotent (cache the first True). Add `as_dropbox_callback()`
  returning `def cb(recv, total): p.bytes(recv, total); return not p.cancelled()`, so no call
  site hand-writes `return not iscanceled()` (the wiz.py:346 pattern the library exists to kill).
- `total == 0`: never compute a percent; seed `update(0, ...)` on `__enter__`; the divide-by-zero
  guard lives INSIDE `Progress.bytes()`/`items()` (not in CreateZip), so no caller can reintroduce
  the empty-folder ZeroDivisionError. Preserve `ExtractWithProgress`'s `... or 1.0` guard too.
- `__exit__` always closes, wrapped in try/except (ui.py is now a single point of failure).
- ui.py imports only `xbmc*`/`xbmcvfs` (fully unit-testable off-device).

### copy_with_progress (SMB/NFS fix) - hardened

- TRI-STATE result: OK / FAILED / CANCELLED (not a bare bool).
- Atomic destination: write to a temp/sidecar; on full success + size-check, rename to final.
  On ANY non-success exit, delete the partial before returning.
- `xbmcvfs.File.write()` returns a BOOL, not a byte count - check it (`if not f.write(chunk): raise`).
- No public `flush()`: close dst BEFORE stat/validate. Sequence: write all -> `dst.close()` ->
  `Stat(dst).st_size() == src size` assertion -> only then OK.
- `readBytes(n)` returns bytearray; EOF `while chunk:`; byte count via `len(chunk)` (short last chunk).
- `COPY_CHUNK = 1024*1024` named constant (tune on device; do not exceed 1 MB).
- Fallback to `xbmcvfs.copy` ONLY on FAILED, NEVER on CANCELLED. Fallback runs against a clean
  (partial-deleted) destination; a False fallback result raises `VfsCopyError` so backup-ship
  rotation is still skipped (preserves the last-good-backup invariant).

### Cancel-after-wipe (One-Tap) - single uninterruptible unit

- Keep the proven stage -> validate (`is_zipfile`) -> wipe order (onetap.py:429-477); build on it.
- `cancelable=False` on the post-wipe extract is necessary but NOT sufficient. Pass `post_wipe=True`
  through `wiz.restore` so that once wiping has occurred it: (a) skips the redundant re-validation
  early-return, (b) does NOT early-return on the size/zip check, and (c) ALWAYS reaches `ask_restart`
  even on a partial/empty extract (a wiped box left unprompted is the worst outcome).
- Do NOT attempt an atomic wipe+extract tree-swap (not possible on these devices).

### dropbox_remote.download cancel - land + test FIRST (Stage A)

- Callback returns False to cancel; `download` checks the return, runs the existing broken-stream
  partial-cleanup, and raises `DropboxCanceled`. Keep the `try/except` around the body-format part
  ONLY (a buggy callback must not corrupt the stream) - do not swallow the cancel signal.
- Dedicated regression test: cancel mid-stream -> partial deleted -> `DropboxCanceled` (caller shows
  "canceled," not "failed"), mirroring the existing upload-cancel test.

### Completeness (enumerate, do not hand-wave)

- maintenance.py: ALL THREE notifications (119, 154, 174) -> `ui.notify` (uses `Dialog().notification`,
  NOT `executebuiltin('Notification(...)')`). Fixes the `'Maintenance'` vs `'EZ Maintenance++'` split.
- control.py: all THREE heading defaults (136/146/150) -> `ui.HEADING`.
- onetap.py: migrate the config dialogs (pick, verify, rename input, remove confirm, menu/_pin_actions,
  notifications) and delete the local `ADDON` constant.
- Invariant to write down: FRESHSTART preserves the Addons DB (`keep_addon_db()`, no restore follows);
  One-Tap `apply` does NOT (the restore supplies the DB). A future edit must not add/remove it.
- Out of scope, state explicitly: the Dropbox QR/auth flow (`_show_auth_prompt`/`_QRWindow`) and
  `speedtest.py`'s dialog are NOT part of the backup/restore feedback surface - not migrated.

### Empty-backup-folder behavior - decide + test

An empty/all-excluded folder must produce a valid empty-but-real zip and report "complete" honestly
(not a false success on zero files), with no ZeroDivision and no stuck dialog.

### Scope split

- In scope (true dead code): remove `killxbmc()` and `xml_data_advSettings_old/New`.
- OUT of scope - SEPARATE PR: `buildInstaller`/`BUILDS`/`install_build` removal. It has LIVE routes
  (`default.py:533`, `580-593`) and pulls in `skinswap` + `downloader`/`_pbhook` - a feature/logic
  removal, which violates this initiative's "no logic changes" rule. Its dialogs die with it, there.

### Testing = a regression contract

- Extend conftest with a write-capable, mode-aware fake `xbmcvfs.File` (buffered writes, scriptable
  `write()->False`, chunked `readBytes(n)`) + a `copy` spy, so the copy/cancel/partial/fallback tests
  are realizable off-device.
- test_ui.py: `bytes(0,0)`/`items(0,0)` indeterminate; `_pct(n,0)==0`; copy OK / cancel-mid-copy
  (partial deleted, no fallback) / read-error (partial deleted -> fallback) / `write()->False` (raises)
  / fallback-False (raises `VfsCopyError`) / atomic (no partial at final path) / size-mismatch (fails);
  `Progress.__exit__` closes even on a body exception; every dialog helper asserts `HEADING`.
- New behavior tests: download-cancel aborts + cleans partial; post-wipe extract is uncancelable AND
  always forces `ask_restart`; empty-folder zip has no ZeroDivision, closes its dialog, reports honestly.
- Regression pins (must stay green; assertions may be extended but NOT weakened): `test_download_progress_total_from_api_result`,
  `test_download_broken_stream_removes_partial_temp`, `test_create_zip_raises_on_vfs_copy_failure`,
  `test_backup_vfs_copy_failure_no_success_no_rotation`, `test_apply_bad_zip_never_wipes`,
  `test_apply_empty_slot_never_wipes`, `test_wipe_protects_addon_deps_and_temp`, `test_wipe_keeps_addon_db_file`,
  `test_stage_dropbox_reports_download_progress`. A change that needs a weakened safety assertion is a
  design error, not a test update.

### Rollout - reviewed slices (not big-bang), suite green after EACH file

- Stage A (foundation): `ui.py` + `test_ui.py` + extended conftest fake + the `dropbox_remote.download`
  cancel change with its regression test. Nothing consumes `ui` yet.
- Stage B (non-destructive): backup SMB/NFS ship (`copy_with_progress`), `CreateZip`, regular restore +
  `restoreFolder`, `tools.py`, `maintenance.py`, `control.py`, `default.py` FRESHSTART dialogs.
- Stage C (the wipe path): `onetap.apply` + the post-wipe uninterruptible unit - reviewed last, with an
  on-device SMB + NFS smoke test before release.
- Stage D (SEPARATE PR): buildInstaller removal.
