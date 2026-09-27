# CLAUDE.md

This file provides guidance to Claude Code when working in this repository.

## HARD RULE: you may not say something is impossible on Apple TV

**Read `~/Code/moquette/kodi/.claude/skills/apple-tv/SKILL.md` before claiming
you cannot do something on a tvOS box.** One file, dispatch index in section 0.
Everything this project has ever needed is in it and proven on hardware: how to
wake a box that is OFF (section 7, you reboot it), how to read the logs
(section 3), how to read any setting with Kodi CLOSED (section 5), crash reports
and memory kills (section 4), deploying and proving the bytes landed (section 7),
and why a file listing is a FALSE NEGATIVE on tvOS (section 8).

Every "it is not supported", "there is no way to" and "the box is unreachable"
ever written about these boxes has been **wrong**, each time because someone
stopped after one failed command instead of reading what was already written.

Before writing that something cannot be done you must have: read the relevant
section of that playbook, checked `.claude/memory/`, retried the exact command
three times (wireless tvOS pairings throw transient errors on healthy boxes),
checked the flag against the subcommand (`copy from` takes `--user`, `info files`
takes `--username`), and run `xcrun devicectl <subcommand> --help`. Then report
what you tried and what it returned.

The ONE genuine gap is screenshots, which are impossible on tvOS because
`WinSystemTVOS.mm` never registers a screenshot surface. Cite that. Everything
else is documented.

**"Fixed" means verified on the affected device class, not verified in code.**
A green suite is not a fix. Cheapest-first: the two-layer test fake, then the
wipeable macOS bench, then a real box (playbook section 12a).

## Markdown rules (enforced by the global git hook)

These are the whole standard. There is no skill to load.

- No em dash, en dash, horizontal bar, robot emoji, or AI attribution anywhere.
  The plain hyphen `-` is always fine.
- Never begin a wrapped line with `+`, `-`, or `*`. CommonMark turns it into a
  list item and splits your paragraph.
- Never let an inline code span cross a line break. It strips the
  list-continuation indent and leaves the next agent editing a stale copy.
- Markdown is deliberately NOT auto-formatted here. Do not add it back.


## Where things stand

There is no tracker. `TASKS.md` was deleted 2026-07-21 along with the rest of
the fleet process. `git log` is the load-bearing fact. Both 2026-07-18 restore
defects are FIXED IN CODE; what survives them is summarized below.

**For anything Apple TV, read `~/Code/moquette/kodi/.claude/skills/apple-tv/SKILL.md`.**

### The two restore defects - both FIXED, one residue OPEN BY DESIGN

**`docs/restore-defects-2026-07-18.md` is the diagnosis record. It was written
before the fixes and still reads "NOT fixed"; treat it as history, not status.
This file is the status.**

Read this before touching `wiz.py`, `tools.py`, `ui.py`, `_kodisettings.py`, or
`service.py`. Short version:

- **Defect A (skin settings clobbered) is FIXED (`be31322`).** Restore writes
  `addon_data/<skin>/settings.xml`, and `_apply_skin_settings` (`wiz.py:869`) now
  re-applies those values IN MEMORY immediately before the restart, so the
  clean-shutdown flush serializes the archive's values rather than the
  pre-restore ones. It was never tvOS-only.
- **The old `wiz.py:753-764` false docstring NO LONGER EXISTS.** Earlier docs
  told agents to distrust a docstring claiming `Quit` skips Kodi's
  clean-shutdown flush. That text was corrected and the current comment
  (`wiz.py:979-981`) states the truth: `RestartApp` being desktop-only means
  `Quit` does not RELAUNCH, NOT that it skips `CApplication::Stop`. Do not go
  looking for the false version; do not reintroduce it.
- **Defect A3 (`lookandfeel.skin` itself) is OPEN BY DESIGN, not unfinished.** A
  restore that CHANGES the skin reopens on the old one, because Kodi offers no
  way to set the skin live without arming the 10-second keep-skin countdown and
  any non-Yes reverts. 2026.07.19.0 ships DETECT AND REPORT, not a fix. See
  the accepted next-cycle design is to terminate instead of `Quit`.
- **Defect B (post-restore prompt discarded input) is FIXED.** The trigger was
  never in EZM++: the since-decommissioned `skin.estuary7`'s `Home.xml:9` armed an alarm whose
  skinshortcuts rebuild ends in `ReloadSkin()` and destroys the window stack.
  `_keyboard_result` no longer collapses a non-answer into an answer,
  `prompt_devicename_after_restore` re-presents instead of advancing, and
  `service._wait_skin_settled` waits past the deferred build. EZM++ still sets
  NO timeout anywhere - do not go hunting for one.
- **Test trap (still live):** `Skin.HasSetting(<id>)` over JSON-RPC CREATES the
  setting id in memory (default false) and it persists on flush. `GetInfoBooleans`
  is not a read-only probe for skin settings. A regression test built on it passes
  for the wrong reason.

**Defect A is instance number 4 of a class this project already named.** Read
`repo/docs/playbooks/kodi-settings-clobber.md` (its instance 1 is this exact file
and owner, and the documented fix is BOTH mechanisms, not one) and
`repo/docs/plans/atv-every-boot-settings-reassert.md` (an every-boot re-assert
was REJECTED by unanimous adversarial review on 2026-07-08 - do not re-propose
it; its verdict section holds the corrected fix that became
`nsud.rewrite_userdata_xml`).

**`docs/next-update-candidates.md`** is the forward queue for everything else
investigated but unshipped: the video-cache `readfactor` question (answer: do NOT
raise it, with source proof), a `memorysize` GUI-list hazard, and an unresolved
owner decision about credentials stored in cleartext inside the backup zips.

## What this repo is

**EZ Maintenance++** (`script.ezmaintenanceplusplus`) is a fork of EZ Maintenance+
(aenema, peno) for the Tony.7.Bones Kodi fleet: four Fire OS boxes and two Apple
TVs (`../.claude/scripts/DEVICES.md`), on Kodi 22 "Piers" except atv2, which is
still on Kodi 21.3 "Omega". This repo (`moquette/kodi-ezmpp`, public) is the **single source of
truth**: the add-on source, its full test suite, and the build/release tooling live
here and only here.

**Distribution stays in the sibling repo** (remote
`tony7bones/tony7bones.github.io`, local checkout `~/Code/moquette/kodi/repo`;
the standalone `~/Code/moquette/tony7bones.github.io` path older docs cite DOES
NOT EXIST). A version bump pushed to `main` here IS the release: CI
(`.github/workflows/ci.yml`, `publish` job) builds the deterministic zip,
publishes it as the GitHub Release `v<version>`, verifies the asset's sha256,
and dispatches the hub (`repository_dispatch` type `ezmpp-release`, with the
`T7B_DISPATCH_TOKEN` secret). The hub carries NO copy of this add-on, not even
`addon.xml` (since 2026-09-26): its `_tools/catalog.json` entry is a
`release-asset` template and its Pages build resolves `releases/latest` on
every run, taking `addon.xml` and art out of the zip. The old
`addons/hosted/script.ezmaintenanceplusplus/` mirror is deleted.
There is no virtual proxy and no `repository.json` left in the hub; both
belonged to the retired dynamic-proxy design.

Until 2026-07-14 the source was hand-synced between both repos and the copies
drifted, the hub's copy holding the tests and the real fixes while this one went
stale for weeks. Fix bugs and add tests **here**. For anything tvOS, read
`~/Code/moquette/kodi/.claude/skills/apple-tv/SKILL.md`.

## The build/test/release contract

- **Deterministic packaging.** `tools/build.py` (wrapped by `./build.sh`) sorts zip
  members and fixes 1980-01-01 timestamps, same discipline as the hub's
  `generate_repo.py` and the skin repo's `build_skin.py`. `./build.sh --check` builds
  twice and byte-compares.
- **Tests are mandatory before any release.** Run
  `/opt/homebrew/bin/python3 -m pytest tests/ -q` (928 collected: 925 pass + 3 xfail, measured 2026-09-26; the
  system `python3` on this machine is 3.9, too old for this suite), and
  `ruff check tests/ tools/` must also be clean.
- **Tool versions are pinned in `requirements-ci.txt` and `ruff.toml`**, which CI
  installs from and which `../bin/check-all` provisions. `ruff.toml` also declares
  `[lint] select`, so a ruff release cannot change what "clean" means here (0.16.0
  put 283 findings on untouched code when the set was inherited rather than
  declared), and its `required-version` makes the global auto-format hook a no-op.
  Consequence: a bare `ruff` off PATH refuses to run unless PATH ruff is the pinned
  version. Lint scope is `tests/` and `tools/` only; `script.ezmaintenanceplusplus/`
  stays out on purpose, as inherited upstream debt marked by
  `tests/test_addon_source_lint_debt.py`.
- **`tools/check_unreleased_changes.py` warns when source moved at an
  already-released version.** The publish job is idempotent by tag, so a fix
  committed without bumping `addon.xml` builds, tests green, publishes nothing,
  reaches zero boxes and goes red nowhere. It is a warning by design, because
  batching several commits into one later release is the normal workflow here.
  `tools/release.sh` is the hard block; it refuses outright when the tag exists.
- **CI publishes the release; `tools/release.sh` is the manual re-cut only.**
  The `publish` job in `ci.yml` runs on every push to `main` after `test` is
  green: it builds, and if the tag `v<version>` for `addon.xml`'s version does
  not exist it creates the GitHub Release with the zip attached (`--target` the
  pushed SHA), verifies the asset is anonymously downloadable and its sha256
  matches the CI build, then dispatches the hub. Idempotent by tag. `tools/release.sh`
  does the same by hand, anchored to `origin/main` (never local/unpushed work),
  and refuses outright when the tag exists. Never `gh release create` by hand.
- **Releasing here IS publishing (since 2026-09-26).** The hub needs NO commit:
  the dispatch (or its daily cron) makes `pages.yml` resolve the latest release
  and rebuild `/static/`, which is what boxes actually read; a box picks the
  version up on its next Check for updates. The old follow-up (bump the version
  and news line in `repo/addons/hosted/script.ezmaintenanceplusplus/addon.xml`)
  is GONE with that directory, as is the hub's `check_hosted_release_sync.py`
  gate that policed it; the `python3 _tools/release.py --proxy` mode went with
  the proxy engine before that. If `/static/` is behind a green release, read
  the hub's latest "Build & Deploy Pages" run for a `::warning::` and a `stale`
  entry; never commit a copy of `addon.xml` to the hub.
- **This add-on's changelog is hand-written, multi-line prose** (`changelog.txt` +
  the `<news>` block in `addon.xml`) - NOT the one-line convention the hub's
  `release.py` automation expects. Never run that automation against this add-on's
  news; it has corrupted the changelog before (~190 lines mangled in one run).

- **A box cannot DOWNGRADE from the repo, so withdrawing a feature means shipping a
  NEWER version with it removed.** Two measured reasons: `repo/_tools/generate_repo.py`
  prunes superseded zips on every generate, so the catalog only ever offers one version
  for a box to pick; and the fleet runs `general.addonupdates` = `0`, which Kodi's
  `system/settings/settings.xml` documents as AUTO_UPDATES_ON, so a hand-installed older
  zip silently re-upgrades itself on the next repo check. Do not confuse that setting
  with `addons.updatemode`, which is the unknown-sources policy and unrelated. Verify a
  box with `Settings.GetSettingValue` over JSON-RPC.

## The scheduled repository update check (service.py, since 2026.09.26.1)

`_service_loop` runs `_maybe_update_addon_repos` on every 60 s tick when no
video is playing; it fires `xbmc.executebuiltin("UpdateAddonRepos")` once per
interval (`repo.check_minutes`: default 60, floor 15, 0 off) and logs
`ezmaintenanceplus: repository update check triggered (every N min)`. The
last-check stamp is a file, `addon_data/<id>/.ezm_repo_check`, armed by
`_arm_repo_check_clock` at service start so the first check after boot is a
full interval later and a restart inside the interval fires nothing extra.
Owner rule: pressing the check is fine, changing the setting is not. The
feature never reads or writes `addons.updatemode` or `general.addonupdates`,
and `tests/test_service_repo_update_check.py` pins that along with the cadence,
the playback deferral, the clamp and the restart persistence.

## The share host and the PVR share step (since 2026.09.26.5)

Owner decision 2026-09-26: every box reaches the mini over Tailscale, never the
home router's DHCP address. The mini's tailnet address is fixed by Tailscale.

- **One constant.** `resources/lib/modules/sharehost.py` holds `SHARE_HOST`
  (`100.121.59.123`), the derived `SHARE_URL`, `BACKUP_URL`, `IPTV_URL`, and
  `LEGACY_HOSTS` (the addresses boxes used to carry; `192.168.7.2` today). No
  other file names the mini. The House bundle's `sources.xml` and the per-class
  overlays carry the `@SHARE_HOST@` token and `profile.load` renders it; a bundle
  file naming a legacy host literally FAILS validation.
- **Migration, two places, same rewrite.** `sharehost.migrate` moves every
  `nfs://<legacy>[:port]/` to `SHARE_HOST` and nothing else. The profile's
  own-setting step and sources step report a moved item as `applied` with the
  old value in the detail; `profile.ensure_share_host_migrated` does the same
  at every boot from `service._maybe_migrate_share_host` (this add-on's
  `download.path`/`restore.path` and the `<files>` sources), one log line per
  item, silent and storage-untouched when nothing names a legacy host.
- **The PVR share step** (`resources/lib/modules/pvrshare.py`): lists
  `IPTV_URL` over `xbmcvfs.listdir`, migrates and parses each
  `instance-settings-N.xml`, and when the box's copy under
  `addon_data/pvr.iptvsimple/` differs writes the share's copy (temp file,
  `os.replace`, mode bits of the file replaced, then `nsud.persist_one`). If
  anything changed it reloads the client ONCE via `Addons.SetAddonEnabled`
  false then true, bracketed by the restore's PVR pause marker so a crash
  between the two calls is healed by `_maybe_resume_paused_pvr`. It never
  enables a client the box had off. MEASURED on office 2026-09-26 (kodi.log):
  the toggle at 17:24:34 produced `Recreating PVR client: addonId=pvr.iptvsimple,
  instanceId=1` and `instanceId=2` at 17:24:49 and 17:24:54, after which
  pvr.iptvsimple loaded `customTVGroups-*.xml` from `nfs://100.121.59.123/`
  where the 17:11 boot had loaded them from `nfs://192.168.7.2/`; live TV
  played after it (A&E, speed 1, time advancing). That is the proof the
  disable/enable is honoured; no PVR manager restart exists to compare
  against. Guards: unreachable share or no instance
  files is a `skipped` (nothing touched, one log line); a template that is not
  utf-8, not XML or not `<settings>` is never written; nothing runs while
  something plays (the service retries on its next idle tick, the profile
  reports the deferral). `skipped` is an OK outcome for the profile flow.
- **The Tailscale nudge** (`resources/lib/modules/tailnet.py`): when the share
  root does not list, an Android box starts `com.tailscale.ipn` ONCE per
  process via `xbmc.startAndroidActivity`, waits up to 20 s re-probing, and
  logs one line either way; tvOS logs that the tailnet is down. MEASURED on
  office 2026-09-26: the launcher and leanback activity both resolve to
  `com.tailscale.ipn/.MainActivity`; the CONNECT_VPN intent on `.IPNReceiver`
  is a broadcast and unreachable from Kodi's Python. GUESS, not measured:
  that opening the app reconnects a client the user left disconnected.
- **Where it runs.** `service.py` calls `_maybe_sync_pvr_share` after the GUI
  wait (an NFS listing can block on a dead mount and must never delay the
  startup sequence); `profile.plan` appends a `pvr-share` op after the sources
  and before the repository enable. Tests: `tests/test_pvr_share.py`.
- **This is the second sanctioned boot-time add-on toggle** next to the
  restore-scoped PVR pause: bounded, always re-enables, only when the client
  was already enabled, reported loudly when it fails. Read the backup/restore
  contract's "one bounded toggle" line with this exception in mind.

## The skin migration to Estuary++ (since 2026.09.27.1)

Owner decision 2026-09-26: Estuary POV (`skin.estuary.pov`) became Estuary++
(`skin.estuary.plusplus`), and every box migrates itself. The whole mechanism
is `resources/lib/modules/skinmigrate.py`; `service.py` only calls it
(`_maybe_migrate_skin`, after the GUI wait and before the PVR share step, owed
again on the next idle tick only when it deferred for playback).

- **This is the ONE module allowed to name a skin id.** The owner's decoupling
  guard (`tests/test_devicename_buffer_preserve.py`, `_RENAME_EXEMPT`) exempts
  exactly `skinmigrate.py`, with the reason; `service.py` stays skin-agnostic
  and its own token test still holds. The module is transitional and goes when
  the old id is retired from the hub (the rename plan's stage E).
- **Install goes through `InstallAddon`**, Kodi's own installer, NOT the
  profile's extract-and-enable path: the installer stamps `installed.origin`
  with the repository id (AddonInstaller.cpp, "Write origin to database via
  addon manager"), and a blank origin is an add-on Kodi never updates. The
  builtin's confirm (strings 24076/24100/24101) is answered by the profile's
  measured mechanism (text match on `Control.GetLabel(9)`, walk to control 11,
  select). The install is modal, so `CSkinInfo::OnPostInstall` raises no
  "switch to this skin?" prompt of its own (Skin.cpp gates it on `!modal`).
  The id is resolved first through `Addons.GetAddons` with `installed=false`
  (JSON-RPC's route to `GetInstallableAddons`); a hub that does not list it is
  a one-line skip.
- **The settings copy is a byte copy read through the VFS**, written atomically
  and persisted with `nsud.persist_one`, only when the target is absent. The
  skin saves its settings to disk 500 ms after any change (Skin.cpp,
  `CSkinSettingUpdateHandler`, `DELAY = 500ms`), so the copy is the live state.
- **The switch is live** (`Settings.SetSettingValue lookandfeel.skin`) with the
  "Keep skin?" dialog (13123/13111, 10 s, ApplicationSkinHandling.cpp
  `ReloadSkin(confirm)`) answered Yes by the same watcher, and the verdict read
  back from `xbmc.getSkinDir()`. The shutdown-window write was NOT chosen
  because it only lands at the next Kodi start and an appliance can run for
  days; the live switch lands when the release does. A miss is an `error` in
  the log; Kodi's own revert leaves the old skin, and the next start retries.
  Never let that dialog time out (atv2, 2026-07-17).
- **Removal waits a start**: with Estuary++ active and the old skin present,
  `shutil.rmtree` of its directory plus its `packages/` zips, then
  `UpdateLocalAddons` so `CAddonDatabase::SyncInstalled` drops the row.
  `addon_data/skin.estuary.pov/` stays until stage E.
- **Restore mapping**: `wiz._map_restored_skin` (after the archive's skin
  settings are captured, before the boot-skin write and the restore-check
  marker) maps the old id to the new one when the new skin is present and
  copies the archive's settings file to the new folder (a raw copy at the
  extract stage, allowlisted in `test_no_raw_userdata_writer.py` because the
  restore's own re-vector pass follows it and a persist there would drop the
  POSIX copy wiz reads next). `service._skin_ids_match` accepts either id.
- Setting `migrate_skin` (Maintenance, group Skin, default true). Tests:
  `tests/test_skin_migration.py`.

## The tvOS/Apple TV storage rules (read before touching `nsud.py`/`nsub.py`/`wiz.py`)

Apple TV shadows certain userdata `.xml` files into NSUserDefaults; a key SHADOWS the
disk file, it does not mirror it, and Kodi never copies a key back to disk. Getting
this wrong has destroyed real user data twice (2026-07-08, 2026-07-14). The
authoritative model (with exact Kodi source citations) lives in the fleet meta
repo: `~/Code/moquette/kodi/.claude/skills/apple-tv/SKILL.md`. Three
mechanical guards in this repo enforce the lessons - do not remove or route around
them without understanding why they exist:

- `tests/test_no_raw_userdata_writer.py` - a chokepoint lint (AST-based) that fails
  if any function writes a userdata/`addon_data` XML with plain `open()`/
  `xbmcvfs.File()` without calling `nsud.persist_one`.
- `tests/fake_kodi_storage.py` + `tests/test_fake_kodi_storage.py` - the two-layer
  tvOS storage fake (NSUserDefaults keys + a real POSIX tree) whose `state()` can
  represent "key exists, disk file gone" - the shape a plain-dict fake cannot
  express, which is why 33 tests once stayed green through a real bug.
- `tests/fake_kodi_sandbox_io.py` + `tests/test_tvos_sandbox_io_contract.py` - a
  DIFFERENT bug family, per its own docstring: the App Sandbox cross-layer READ
  quirk for local and `special://temp` files, which are not under userdata. An
  earlier revision of this file credited it with the two-layer userdata model;
  that was wrong, and the plan review that caught it is recorded in
  `settings-profile-plan §8`, item 2.

Two corrected facts, now consistent everywhere in this project - do not let either
regress:

- `xbmcvfs.delete()` **cannot** delete a userdata `*.xml` on tvOS. It drops the
  NSUserDefaults key and reports success; the POSIX file is left on disk, silently.
- Kodi does **not** re-materialize a disk file from its NSUserDefaults mirror. A key
  shadows the disk; nothing ever copies it back.

## The backup/restore contract (owner-decided 2026-07-16)

These are the rules every session must hold the backup/restore/wipe code to
(implementation landing 2026-07-16; treat any older behavior in the tree as a bug,
not a spec):

- **Full means full.** A full backup captures EVERYTHING on both OSes, INCLUDING
  `addon_data/pvr.iptvsimple`. The 2026.07.08.5 "zero IPTV" backup exclusion is
  REVERSED - do not reintroduce it. The only exclusions are the add-on's own
  `settings.xml` (it carries the Dropbox token) and `special://home/temp` at the
  ROOT only.
- **Two-layer tvOS capture, loud failures.** A tvOS backup reads BOTH layers: the
  POSIX walk plus the NSUserDefaults plist capture (`nsub.py`), IPTV included. A
  tvOS capture failure FAILS the backup loudly; a backup never silently omits what
  it could not read.
- **Manifest + truthful reporting.** Every backup embeds `backup_manifest.json`
  (`{"created","source_os","entries","failed":[...]}`). Restore verifies the
  extract against it and reports extracted/skipped/failed truthfully; a partial
  restore is reported as PARTIAL, never "Complete".
- **Instance-settings sweep; one bounded toggle.** Restore sweeps the target's
  STRAY `instance-settings-*.xml` AFTER the extract (files the archive does not
  carry; a cancel can never destroy config the box already had) so pvr.iptvsimple
  state exactly equals the archive (the duplicate-instance brick guard). The ONLY
  sanctioned add-on toggles in boot and restore are the restore-scoped PVR pause
  and, since 2026.09.26.5, the PVR share step's reload of an ALREADY-enabled
  pvr.iptvsimple (the share host section above; it rides the same pause marker)
  (Apply Settings Profile, below, is the one OTHER actor allowed to enable
  add-ons): when the
  archive carries IPTV config and pvr.iptvsimple is enabled, restore disables it
  for the extract window and ALWAYS re-enables it afterward (cancel path
  included; a re-enable failure is reported loudly). Without the pause, the live
  client flushes stale in-memory instance settings over the restored files at
  the next clean shutdown (hardware-proven, kodi-settings-clobber.md). Boot-time
  work is limited to SELF-HEALING an interrupted or superseded restore: resuming a
  restore-paused PVR client, the once-per-version stale-key purge, the stale
  bytecode purge, the read-only post-restore check, and the read-only
  post-profile check. Boot NEVER installs, stages, or enables an add-on the box
  did not already have enabled, and restore never installs or stages add-ons.
  **Apply Settings Profile is the one deliberate exception to both lines**, as
  a THIRD actor: user-invoked, foreground, behind an explicit confirm, applying
  a validated bundle only (`resources/lib/modules/profile.py`, `settings-profile-plan §9`).
  It stages, enables and (for its three confirm-gated core settings) answers
  Kodi's own warning dialogs; its boot-side half checks and reports ONLY. Boot
  and restore stay exactly as restricted as written above - do not read this
  exception as loosening either of them.
- **Two-layer wipe.** A wipe on tvOS (One-Tap clean wipe, Fresh Start) clears BOTH
  layers - the POSIX files AND the NSUserDefaults keys - with the same exclusions.
  A POSIX-only wipe leaves stale keys that shadow the restored files; that bug
  class is closed, do not regress it.
- **Stale-key purge semantics.** `nsud.purge_stale_keys` clears the
  vector-everything-era stale keys. It MUST materialize any key-only file to disk
  first - the purge never destroys the only copy of anything. It runs ONLY
  automatically, from three clearers: inside every restore (`wiz.py`, both the
  wipe and merge paths), once per add-on version at boot (`service.py`), and the
  two-layer wipe's own key pass (`onetap.py`). The manual "Purge stale tvOS keys"
  menu action was REMOVED in 2026.07.19.5 - it covered no case the automatic
  clearers miss, and it asked a non-technical owner to self-diagnose an invisible
  symptom. Do not reintroduce a manual entry point.
- **The purge and the duplicate-listing probe must agree.** `purge_stale_keys`
  deliberately KEEPS the skin's dual-layer `script.skinshortcuts/*.DATA.xml`
  sidecars, so `restorecheck.duplicate_listing_hits` must not count them either -
  it IMPORTS `nsud._is_skin_menu_sidecar` rather than re-deriving the rule. Two
  copies of that predicate drifting is what made every tvOS restore end in a
  "needs attention" the owner could do nothing about (atv2, 23 false hits,
  `verification/2026.07.19.4.json`). The exclusion is the `*.DATA.xml` sidecar
  pattern ONLY: `script.skinshortcuts/settings.xml` and every non-sidecar
  duplicate still hit, because a real stale key shadowing a restored file is the
  one thing that probe exists to catch.

The tvOS storage facts above remain true and load-bearing under this contract: a
key SHADOWS the disk file, Kodi never re-materializes a disk file from a key, and
`xbmcvfs.delete()` on tvOS drops only the key while leaving the POSIX file. The
two-layer wipe and the purge exist BECAUSE of those facts.

## House rules (inherited from the fleet's workflow)

- implement -> TEST -> gate (`../bin/check-all ezmpp`, which runs pytest and the
  pinned ruff; add `--fast` to skip the deterministic build check) -> commit/release.
- **Routine changes get a one-line commit message.** Long-form records
  (acceptance logs, multi-paragraph commits) are for genuine incidents only.
- Approval is needed for DESTRUCTIVE or OUTWARD-FACING actions only: wiping a
  box, restoring onto a box, publishing, pushing. Reading logs, listing files,
  read-only JSON-RPC queries and inspecting archives need no approval. The
  office Fire TV `192.168.7.162` HANDS-OFF rule was **LIFTED 2026-07-21**; it
  is a normal target and the rule must not be reinstated.
- **Always pin `adb -s <ip>:5555`.** Several boxes sit on adb at once, so an
  unpinned command lands on whichever one adb happens to pick.
  `repo/_tools/firetv.sh` lost its silent `192.168.7.162` default on
  2026-07-21: it now needs an alias or `FIRETV_IP` and echoes the target it
  resolved. Check what it points at before running it.
- Safety core, unchanged: a backup must contain what it claims (one
  archive-contents inspection when backup/restore code changes); CI green before
  deploy; skins install from the Kodi repo, never adb/devicectl push.
- No AI attribution anywhere; no em dashes in written deliverables.
- Never create anything for this add-on under `repo/addons/`. The source copy
  went 2026-07-14, the code-less shim `repo/addons/script.ezmaintenanceplusplus/`
  in `08d9a3d` on 2026-07-20, and the metadata mirror
  `repo/addons/hosted/script.ezmaintenanceplusplus/` on 2026-09-26. Boxes read
  `/static/`, which the hub builds from this repo's latest release.
