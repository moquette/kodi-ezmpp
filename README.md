# EZ Maintenance++

A fork of **EZ Maintenance+** (by aenema, peno) that makes Backup and Restore work
over Kodi's VFS, so the **Backup Location** and **Restore** folder can point straight
at a network share (`nfs://`, `smb://`) or any other VFS path, not just local storage.

**This repo (`moquette/ezmaintenanceplusplus`, public) is the single source of truth
for EZ Maintenance++:** the add-on source, its full test suite, and the build/release
tooling all live here and only here. It used to be hand-synced with a second copy of
the source in the Tony.7.Bones proxy repo (`tony7bones.github.io`), which drifted -
fixes landed in one copy without traveling to the other. That duplication is gone
(2026-07-14): the proxy repo now carries only a hosted metadata mirror
(`addons/hosted/script.ezmaintenanceplusplus/` - `addon.xml` + icon + fanart, no
source) and points its `repository.json` at this repo's GitHub Release assets, the
same "own repo + release asset" pattern already proven for `skin.estuary7`
(`moquette/estuary7`). Fix bugs and add tests here; only bump the hosted metadata and
re-release the proxy over there. Triage guide for backup/restore failures:
`~/Code/moquette/kodi/.claude/skills/ezm-backup-doctor/SKILL.md`.

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
- **`tests/fake_kodi_sandbox_io.py`** - a two-layer tvOS storage fake (NSUserDefaults
  keys + a real POSIX tree, transcribed from Kodi's own source) so tests can represent
  the state that actually causes data loss ("key exists, disk file gone") - a plain dict
  fake cannot express this, which is why 33 tests once stayed green through a real bug.
  `tests/test_tvos_sandbox_io_contract.py` covers the `ControlImage` write-through
  requirement and the foreign-local-VFS-read bug on top of it.
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
`tony7bones.github.io`'s `generate_repo.py` and `moquette/estuary7`'s `build_skin.py`),
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

After cutting a release here, the Tony.7.Bones proxy repo's hosted metadata mirror
(`addons/hosted/script.ezmaintenanceplusplus/addon.xml`) needs its version bumped to
match and a proxy release (`python3 _tools/release.py --proxy`) to actually ship it -
see that repo's `CLAUDE.md` and `~/Code/moquette/kodi/.claude/skills/ezm-backup-doctor/SKILL.md` for the
exact steps and the current gap between "committed" and "released" if one exists.

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
