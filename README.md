# EZ Maintenance++

A fork of **EZ Maintenance+** (by aenema, peno) that makes Backup and Restore work
over Kodi's VFS, so the **Backup Location** and **Restore** folder can point straight
at a network share (`nfs://`, `smb://`) or any other VFS path, not just local storage.

**This repo (`moquette/kodi-ezmpp`, public) is the single source of truth
for EZ Maintenance++:** the add-on source, its full test suite, and the build/release
tooling all live here and only here. It used to be hand-synced with a second copy of
the source in the Tony.7.Bones repo (`tony7bones.github.io`), which drifted -
fixes landed in one copy without traveling to the other. That duplication is gone
(2026-07-14), and since 2026-09-26 so is the hand-bumped metadata mirror that
replaced it: the Tony.7.Bones repo carries NO copy of this add-on and resolves
its latest GitHub Release at build time. Fix bugs and add tests here; a version
bump pushed to `main` is the release, and releasing IS publishing. Triage guide
for backup/restore failures on tvOS:
`~/Code/moquette/kodi/.claude/skills/apple-tv/SKILL.md` (the old
`ezm-backup-doctor` skill was deleted 2026-07-21).

## Why this fork exists

The original builds its backup zip with Python's `zipfile`, which can only open a
**local** filesystem path. Point its Backup Location at `nfs://host/share/...` and the
backup dies with `FileNotFoundError` because Python never sees Kodi's network layer.

EZ Maintenance++ keeps everything else identical and changes only the file I/O so it
goes through `xbmcvfs` (the same VFS the official "Backup" add-on uses):

- **Backup** builds the zip in `special://temp` (always local, where `zipfile` is happy),
  then `xbmcvfs.copy()`s the finished file to the configured destination and deletes the
  temp. A nfs/smb/any-VFS Backup Location now just works.
- **Backup cancel** cleanup uses `xbmcvfs.delete()` instead of `os.unlink()`.
- **Restore** lists the zip folder with `xbmcvfs.listdir()` and, for a remote zip,
  copies it to `special://temp` before extracting. So you can restore directly from a
  share too.

All of the original's other tools (cache clean, thumbnails, packages, log viewer,
speedtest, skin switch, the wizard) are unchanged.

## What changed (exactly)

Everything is in `resources/lib/modules/wiz.py`:

| Function        | Change                                                                                                                                      |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `CreateZip`     | Stage the zip in `special://temp` when the destination is a VFS path (`://`), then `xbmcvfs.copy()` to the destination and remove the temp. |
| `backup`        | Cancel cleanup uses `xbmcvfs.delete(backup_zip)` (VFS-safe).                                                                                |
| `restoreFolder` | List the restore folder with `xbmcvfs.listdir()` instead of `os.listdir()`.                                                                 |
| `restore`       | Copy a remote (`://`) zip to `special://temp` before `ExtractWithProgress`, then drop the temp.                                             |

The add-on id was changed to `script.ezmaintenanceplusplus` so it installs alongside the
original without conflict. No behavior changes for local-path backups.

Since the original VFS fork, the add-on has grown considerably: Fresh Start, Dropbox as
a third destination (PKCE sign-in, chunked resumable uploads, an
on-device QR code), and - the largest addition - **tvOS/Apple TV storage hardening**
(below). The version scheme is date-stamped (`YYYY.MM.DD.N`); check `addon.xml` for the
current one rather than trusting a number written down anywhere, including this file.

## Scheduled repository update check (since 2026.09.26.1)

The service (`service.py`, the 60 s maintenance tick) runs Kodi's own
`UpdateAddonRepos` builtin on a schedule, the same action as Check for updates
in the add-on browser, so a box picks up a new release within the hour instead
of waiting up to a day for Kodi's repository timer or for someone to press the
button. It refreshes the repository indexes only; whether Kodi then installs is
governed by the user's auto-update setting, which this add-on never reads, sets
or changes.

- Setting: Maintenance tab, group Repository Updates, "Check for updates every
  N minutes (0 is off)" (`repo.check_minutes`, a slider in steps of 15). Default 60,
  floor 15 (a lower value is held to 15 in code), 0 switches it off. Read
  every tick, so a change applies live.
- The last-check stamp is the file `addon_data/script.ezmaintenanceplusplus/.ezm_repo_check`,
  not a setting, so a restart inside the interval fires no extra check; the
  first check after a boot lands one full interval later, never at boot.
- The check waits while a video is playing and fires on the next tick after.
- One log line per trigger at the add-on's normal level:
  `ezmaintenanceplus: repository update check triggered (every N min)`.
  An exception in the check is logged as a warning and never stops the loop.
- Tests: `tests/test_service_repo_update_check.py`.

## The share host and the PVR share step (since 2026.09.26.5)

Every box reaches the mini over Tailscale (owner decision 2026-09-26). The
mini's tailnet address lives in one place, `resources/lib/modules/sharehost.py`
(`SHARE_HOST = "100.121.59.123"`), and everything that names the mini derives
from it: the House bundle's `sources.xml` and per-class overlays carry the
`@SHARE_HOST@` token and are rendered at load, and `sharehost.migrate` rewrites
the legacy LAN address (`192.168.7.2`) wherever a box still carries it.

- A box still on the old address moves itself: at every boot
  (`service._maybe_migrate_share_host`) and on Apply Settings Profile, the
  KodiShare and KodiBackup sources and this add-on's backup and restore folders
  are rewritten to `SHARE_HOST`, one log line per item, nothing touched when
  nothing names a legacy host. Sources are live after the next restart.
- The PVR share step (`resources/lib/modules/pvrshare.py`) keeps
  `addon_data/pvr.iptvsimple/instance-settings-N.xml` equal to the templates
  the IPTV builder publishes at `nfs://<SHARE_HOST>/Users/moquette/Kodi/Share/iptv/`.
  It runs after the GUI is up at every boot and as a profile step: list the
  share, compare each template with the box's copy, write the share's copy
  atomically when they differ, and reload pvr.iptvsimple once (disable then
  enable over JSON-RPC) if anything changed. An unreachable share, a template
  that does not parse, or playback in progress means nothing is touched; the
  service retries a playback deferral on its next idle tick.
- When the share cannot be listed, a Fire TV starts the Tailscale app once
  (`resources/lib/modules/tailnet.py`), waits up to 20 s and re-tests; an Apple
  TV logs that the tailnet is down.
- Log lines to look for: `ezmaintenanceplus: profile: ... share host migrated to
  100.121.59.123 (was ...)`, `ezmaintenanceplus: PVR share settings:
  instance-settings-N.xml updated from the share`, `... pvr.iptvsimple reloaded
  (disable/enable)`, and `ezmaintenanceplus: tailnet: ...`.
- Tests: `tests/test_pvr_share.py`.

## The move to Estuary++ (since 2026.09.27.1)

Owner decision 2026-09-26: the skin Estuary POV (`skin.estuary.pov`) is renamed
Estuary++ (`skin.estuary.plusplus`). To Kodi a new add-on id is a new add-on,
so nothing upgrades across the rename by itself. The boot service does it
(`resources/lib/modules/skinmigrate.py`), one log line per step, idempotent:

- When the active skin is the old id (or the old id is installed and the new
  one is not), the service installs `skin.estuary.plusplus` through Kodi's own
  installer (the `InstallAddon` builtin, so the add-on database carries
  `origin = repository.tony7bones` and the new skin auto-updates), answering
  Kodi's "Would you like to download this add-on?" confirm itself; copies
  `addon_data/skin.estuary.pov/settings.xml` to the new id's folder when the
  target is absent (through `nsud.persist_one`, so both Apple TV layers agree;
  the skin's setting keys keep their `pov_` prefix, which is what carries the
  arranged menu across); and switches the skin live, answering Kodi's ten
  second "Keep skin?" countdown Yes. The verdict is read back from the live
  skin, never assumed; a miss is logged as an error, Kodi's own revert leaves
  the box on the old skin, and the next start retries.
- On the start after the switch, with Estuary++ active and the old skin still
  installed, the old skin's directory and cached package zips are removed and
  `UpdateLocalAddons` drops its database row. `addon_data/skin.estuary.pov/`
  is kept for now.
- Guards: never while something is playing (the service retries on its next
  idle tick); a box whose repository index does not offer the new skin yet
  skips with one line and tries at the next start; nothing at all happens on a
  box with stock Estuary or no POV skin; the Maintenance setting "Move this
  box to Estuary++ automatically" (`migrate_skin`, default on) switches it off.
- Restore: an archive naming the old skin restores onto Estuary++ when the new
  skin is installed (the archive's skin settings are copied to the new id's
  folder, then re-applied live as before), and the post-restart skin check
  accepts either id. Old archives keep restoring.
- Log lines to look for: `ezmaintenanceplus: skin migration: install: applied
  -> ...`, `... settings: applied -> settings.xml carried across (N bytes)`,
  `... switch: applied -> switched to skin.estuary.plusplus (keep-skin dialog
  answered yes)`, and on the following start `... remove old skin: applied ->
  skin.estuary.pov removed (...) and dropped from the add-on database`.
- Tests: `tests/test_skin_migration.py`.

## tvOS/Apple TV storage hardening (why this add-on is more careful than it looks)

Apple TV stores Kodi's files fundamentally differently from every other platform: the
whole Kodi home tree lives under `Library/Caches` (which the OS may purge), and Kodi
vectors `.xml` files under `userdata/` into NSUserDefaults so they survive a purge. A
key **shadows** the disk file - it does not mirror it, and nothing ever copies a key
back to disk. Getting this wrong has cost real user data twice: a 2026-07-08 incident
where a settings-durability rewrite needed a backup that reads both storage layers, and
a 2026-07-14 incident where an overly broad vectoring rule deleted the POSIX copy of a
skin's customized main-menu data, which the skin then couldn't read back (fixed in
`nsud.py`'s `_should_vector`, scoped to exactly what Kodi's VFS actually reads). The full
storage model (with exact Kodi source citations) lives in the sibling proxy repo's
`~/Code/moquette/kodi/.claude/skills/kodi-storage-map/SKILL.md` - read it before touching `nsud.py`,
`nsub.py`, or `wiz.py`.

Three things in this repo exist specifically to keep that class of bug from shipping
again:

- **`tests/test_no_raw_userdata_writer.py`** - a chokepoint lint (AST-based, not a unit
  test) that fails if any function writes a userdata/`addon_data` XML with plain
  `open()`/`xbmcvfs.File()` without routing through `nsud.persist_one`. Written after an
  adversarial review found the exact same bug class, unguarded, in a second function
  (`boxsetup._write_weather_settings`) that nobody had thought to test.
- **`tests/fake_kodi_storage.py`** - the two-layer tvOS storage fake (NSUserDefaults
  keys + a real POSIX tree, transcribed from Kodi's own source) so tests can represent
  the state that actually causes data loss ("key exists, disk file gone") - a plain dict
  fake cannot express this, which is why 33 tests once stayed green through a real bug.
  `tests/test_fake_kodi_storage.py` guards the fake itself. The similarly named
  `tests/fake_kodi_sandbox_io.py` models a different bug family (the App Sandbox
  cross-layer READ quirk for local and `special://temp` files, which are not under
  userdata); `tests/test_tvos_sandbox_io_contract.py` covers the `ControlImage`
  write-through requirement and the foreign-local-VFS-read bug on top of it.
- **`service.py`'s boot-time `__pycache__` purge** - CPython invalidates a `.pyc` by the
  source's recorded mtime AND size, and `tools/build.py` stamps every zip entry
  1980-01-01 for reproducible builds, so across builds the mtime half is constant and
  staleness detection collapses onto size alone. A same-length edit leaves a stale
  `.pyc` valid and the box keeps running the OLD code after a correct upgrade landed
  the new source beside it (observed on an Apple TV, 2026-07-19).

### Device verification: the gate is GONE (2026-07-21)

`tools/verify_device.py`, the committed `verification/<version>.json` artifacts and the
tests that enforced them were **deleted on 2026-07-21** together with the rest of the
fleet process. Nothing in this repo pulls evidence off a box any more, and the
`ezm_contract_fingerprint` window property the boxes used to publish for that tool was
removed on 2026-07-22 once it was clear nothing read it. Older docs in `docs/` still
describe the gate; they are history.

The rule it existed to serve did not go away: **"fixed" means verified on the affected
device class, not verified in code.** Verify cheapest-first - the two-layer test fake
here, then the wipeable macOS Kodi bench, then a real box.

## Repo, build, and tests

```sh
# Full test suite (system python3 on this machine is 3.9, too old for this suite)
/opt/homebrew/bin/python3 -m pytest tests/ -q

# Lint
ruff check tests/ tools/

# Build the installable zip: dist/script.ezmaintenanceplusplus-<version>.zip
./build.sh
./build.sh --check   # builds twice and byte-compares (determinism gate)
```

`./build.sh` is a thin wrapper over `tools/build.py`, which builds the zip
**deterministically** (sorted members, fixed 1980-01-01 timestamps - same discipline as
`tony7bones.github.io`'s `generate_repo.py`),
so a rebuild of the same source is byte-for-byte identical and a release's sha256
actually means something.

## Release

```sh
tools/release.sh              # build, tag v<version>, publish the GitHub Release asset, verify
tools/release.sh --dry-run    # show the plan, tag/release nothing
```

`tools/release.sh` builds the deterministic zip, tags it `v<version>` (anchored to
`origin/main`, never local/unpushed work), publishes the zip as a GitHub Release asset
on this repo via `gh release create`, then **verifies the asset is anonymously
downloadable and its sha256 matches the local build** - a release that fails
verification is treated as a release that would ship broken bytes to a live box, so it
is a hard failure, not a warning.

`tools/release.sh` is the manual re-cut. The normal path needs no human on the
release step: CI (`.github/workflows/ci.yml`) publishes `v<version>` on the first
push to `main` that carries a new version, verifies the asset, and dispatches the
Tony.7.Bones repo, whose Pages build resolves the latest release and serves it
under `/static/` (since 2026-09-26; there is no hosted metadata to bump there
any more). A box picks it up on its next Check for updates. Confirm a release
went live with `~/Code/moquette/kodi/.claude/skills/update/SKILL.md` section 1.

## Install / use it with a network share

1. Install the zip in Kodi: **Add-ons -> Install from zip file** (you may need Settings
   -> System -> Add-ons -> **Unknown sources** enabled first), or install it from the
   Tony.7.Bones Kodi repository, which serves this add-on's latest release.
2. Open the add-on's settings -> set **Backup Location** to your share folder
   (the folder browser can reach network sources), e.g. an `nfs://` or `smb://` path.
3. Run a Backup. It stages locally, then lands on the share.
4. To restore: set the **Restore from Zip Location** to the same share folder and run Restore.

## Fresh Start

Beyond VFS backup/restore, the "++" fork adds a guarded reset from the add-on's main menu:

- **Fresh Start** - wipe to a clean Kodi canvas with only this add-on left (its dependencies
  and your backups survive), then close so you can reopen Kodi. It keeps the add-on enabled
  through the wipe so it is never "lost" afterward, shows a progress bar while it works, ends
  with a Shut down / Later prompt, and can optionally keep your File Manager sources and your
  repositories through the wipe (Settings > Fresh Start). It runs only from the built-in
  Estuary skin, so the finishing prompt can always be shown after the wipe.

The hardened wipe never removes this add-on, its dependencies, or your backups.

## Credit and license

Forked from **EZ Maintenance+** by **aenema** and **peno**. License is unchanged from the
upstream add-on (see `script.ezmaintenanceplusplus/addon.xml`). This fork only adds VFS
network-destination support, Fresh Start, Dropbox, and the tvOS storage hardening above.

**aenema** and **peno** authored the _original_ add-on only. They are not affiliated with,
and have not endorsed, this fork, and have not given permission for their names to be used
as its authors. They are therefore credited here as the upstream we forked, and are
deliberately kept out of the add-on's own `provider-name` (which lists only the fork
maintainer). This README credit is a factual statement of lineage, not a claim of authorship
or endorsement.
