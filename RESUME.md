# EZ Maintenance++ - repo / build / process notes

This file was originally a 2026-06-30 handoff snapshot. It was trimmed on
2026-07-18: every point-in-time claim (version numbers, test counts, box state,
"what works" proof logs) was stale or wrong, and the backup/restore contract it
carried was a second copy of the one in `CLAUDE.md` - exactly the drift this
repo consolidated to eliminate. What remains below is the material that is
still true and is not recorded anywhere else.

**The backup/restore contract lives in `CLAUDE.md`.** It is the single copy.
Do not restate it here.

## PICK UP HERE (2026-07-19)

**Start at `TASKS.md`** - the project task index (created 2026-07-18; it did not
exist before, which is how doctrine got missed).

Both 2026-07-18 restore defects are now FIXED IN CODE and builds have been cut
through `2026.07.19.3`. Defect A (restore lost skin settings to Kodi's
clean-shutdown flush) was fixed in `be31322`; defect B (post-restore device-name
prompt discarded typed input) was fixed after its trigger was reproduced on the
local bench. What remains is A3, `lookandfeel.skin` itself, which is OPEN BY
DESIGN rather than unfinished: 2026.07.19.0 ships detect-and-report, and the
accepted next-cycle design is to terminate instead of `Quit`.

**`docs/restore-defects-2026-07-18.md` is the diagnosis record, not the status.**
It was written before the fixes and still reads "NOT fixed". `TASKS.md` is the
status; `CLAUDE.md` carries the short version at the top.

## Repo / build / test

- Repo: its OWN git repo (remote `moquette/ezmaintenanceplusplus`, PUBLIC on GitHub -
  required so a Kodi box can anonymously download a release asset), branch `main`.
  This is the ONLY place the add-on source is edited.
  **LOCAL CHECKOUT: `~/Code/moquette/kodi/ezmpp`.** The standalone path
  `~/Code/moquette/ezmaintenanceplusplus` that older docs cite DOES NOT EXIST
  (verified 2026-07-18); the sibling hub repo lives at `~/Code/kodi/repo`
  (tony7bones.github.io).
- Add-on dir: `script.ezmaintenanceplusplus/`. Version is whatever `addon.xml` says
  (date-stamped `YYYY.MM.DD.N` scheme; check the file, do not trust a number written
  down in any doc, including this one).
- Build: `./build.sh` -> `dist/script.ezmaintenanceplusplus-<version>.zip`, a
  DETERMINISTIC zip (sorted members, fixed 1980-01-01 timestamps).
  `./build.sh --check` builds twice and
  byte-compares.
- Release: a version bump pushed to `main` IS the release. CI
  (`.github/workflows/ci.yml`, `publish` job) builds, tags `v<version>`, publishes
  the zip as a GitHub Release asset on `moquette/kodi-ezmpp`, verifies the asset's
  sha256, and dispatches the hub. `tools/release.sh` is the manual re-cut only
  (`--dry-run` shows the plan); it refuses when the tag exists.
- Distribution (since 2026-09-26): `tony7bones.github.io` carries NO copy of this
  add-on. Its `_tools/catalog.json` entry is a `release-asset` template and its Pages
  build resolves this repo's latest release on every run, taking `addon.xml` and art
  out of the zip. The old hosted metadata pointer
  (`addons/hosted/script.ezmaintenanceplusplus/`), the hand bump after each release,
  and the `release.py --proxy` step are all gone; releasing IS publishing.
- Tests: `cd ~/Code/moquette/kodi/ezmpp && /opt/homebrew/bin/python3 -m
  pytest tests/ -q` (system `python3` on this machine is 3.9, too old for this suite).
  `ruff check tests/ tools/` must also be clean.

## Device verification: DELETED 2026-07-21

`tools/verify_device.py`, `verification/*.json` and the test that gated a
`nsud.py` change on a fresh two-class device run are GONE, with the rest of the
fleet process. No JSON-RPC credential is needed by anything in this repo any
more. What survives is the rule, not the machinery: "fixed" means verified on the
affected device class, cheapest-first (test fake, macOS Kodi bench, real box).

## Dropbox credentials - do not re-add a secret

Built-in App-folder app `tony-7-backup`. The PKCE `client_id` is public-by-design
and lives directly in `dropbox_remote.py` (`APP_KEY`); there is no app secret and
no `_appauth.py` file to inject at build time. The vault entries
`DROPBOX_APP_KEY`/`DROPBOX_APP_SECRET` (VAULT.md §23) are a legacy/unused leftover
from an earlier (pre-PKCE) auth design - the running code does not read them.
Earlier revisions of this doc claimed a baked `_appauth.py` had to be recreated
before building. That is wrong and was already contradicted elsewhere in the same
file; do not act on it.

## Process notes / lessons

- Do NOT background a long on-device test the owner is watching live - drive it INLINE
  and narrate. (A silent background agent looped a failing 135 MB upload 3x with no
  feedback - bad UX; the owner had to be my eyes.)
- Verify against GROUND TRUTH (the Dropbox listing) before declaring pass/fail. (I
  called it "failing" off a partial log tail; the folder showed the 25 MB userdata
  backup had actually succeeded.)
- Owner constraints: standard solutions only, NO server hacks/workarounds; lean/elegant;
  UX must beat xbmcbackup; collaborative round-table over solo cowboy work.
