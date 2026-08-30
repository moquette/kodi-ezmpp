# Settings Profile: Phase 0 experiment results, 2026-08-30

The experiments `docs/settings-profile-plan.md` section 5 required, run before any
feature code. Every claim below is MEASURED unless labelled otherwise.

## The bench, and how it was proven clean

- Kodi 22.0-BETA1 (21.90.801) Git:20260727-a872eae1a5, macOS ARM 64-bit, launched
  with an isolated `HOME` under the session scratchpad, created from an EMPTY
  directory at 14:46 local on 2026-08-30 (the directory was `rm -rf`ed and
  recreated first; an earlier candidate showed prior-day mtimes and was discarded
  rather than trusted).
- A genuine first run: Kodi and the macOS `preflight` script created every file
  in the profile. The only foreign artifact ever added was `script.ezmtrial`, a
  16-line trial script staged while Kodi was closed, whose whole job is to run
  the in-process halves of these experiments (`xbmcvfs` writes and
  `executeJSONRPC` sets must run inside Kodi to mean anything).
- Control channels: JSON-RPC over TCP 9090, the EventServer on UDP 9777, and
  (after E3 enabled it) HTTP 8080. The log was read continuously; the shutdown
  flush was confirmed by its own "Saving settings" line each time.

## The source read (done first, per the plan)

Kodi `master` (the Piers line), fetched 2026-08-30:

- `xbmc/settings/MediaSourceSettings.cpp`: `OnSettingsLoaded()` calls `Load()`;
  `OnSettingsUnloaded()` calls `Clear()`, NOT `Save()`. `Save()` is called only
  from the mutation methods (`AddShare` unless `m_ignore`, `UpdateShare`, the
  delete path) and from two GUI callers (`GUIPassword.cpp`,
  `GUIDialogContextMenu.cpp`). **There is no shutdown flush of `sources.xml` on
  Kodi 22.** `CMediaSourceSettings` is registered as an `ISettingsHandler`
  (`Settings.cpp:540`), not an `ISubSettings`, so `CSettings::Save` never
  touches it.
- `xbmc/settings/Settings.cpp:570`: `CViewStateSettings` IS an `ISubSettings`
  of `CSettings`, so the settings level is serialized into `guisettings.xml` by
  every settings save, from live memory. A file-only write of
  `general/settinglevel` loses at the next save, exactly as plan 4.2 argued.
- Settings-level setters (`CycleSettingLevel`, `SetSettingLevel`) are called
  only from `GUIWindowSettingsCategory.cpp`, `GUIDialogAddonSettings.cpp` and
  `GUIPassword.cpp`. `SettingsOperations.cpp` uses the level ONLY as a filter
  parameter. No JSON-RPC path, no builtin, no Python API.
- `xbmc/interfaces/json-rpc/SettingsOperations.cpp:246-306`:
  `Settings.SetSettingValue` calls `CSetting::SetValue` and nothing else. It
  does NOT save `guisettings.xml`, and it returns `InvalidParams` for a setting
  whose `IsVisible()` is false (the parent-dependency refusal mode plan 4.1
  anticipated).
- `xbmc/application/Application.cpp:1873-1874`: the one "Saving settings" flush
  runs during `Stop()` (plus two niche visualisation-window saves at
  `:2187,:2199`). So on Piers a live set is memory-only until a clean close.
- `xbmc/addons/AddonSystemSettings.cpp:90-101`: `addons.unknownsources` set to
  true fires `ShowYesNoDialogText(19098, 36618)` from `OnSettingChanged`
  (post-commit); any answer other than YES sets it back to false.
- `xbmc/network/NetworkServices.cpp` `OnSettingChanging`: `services.webserver`
  enabling fires `ShowYesNoDialogText(19098, 36632)` PRE-commit and vetoes on
  any answer but YES; `services.esallinterfaces` has the same shape with the
  no-authentication-no-encryption warning text.

## E1: does a live write to sources.xml survive to the next boot? YES, all four arms

Each arm: the trial script merged a new `<source>` into
`special://profile/sources.xml` through `xbmcvfs` (read via `xbmcvfs.File`,
ElementTree merge into `<files>`, write via `xbmcvfs.File(..., "w")`), the
session was closed as the arm required, Kodi was relaunched, and the arm ended
at USABLE: `Files.GetSources` listed the entry by path AND `Files.GetDirectory`
browsed it and returned its contents.

| Arm | Session state | Close | On disk after close | After relaunch |
| --- | ------------- | ----- | ------------------- | -------------- |
| 1 | untouched sources | clean quit (`Application.Quit`; "Saving settings" logged) | entry present | listed AND browsable |
| 2 | untouched sources | `pkill -9` | entry present | listed AND browsable |
| 3 | a source REMOVED through the File Manager UI first (context menu, confirm) | clean quit | entry present, UI removal also intact | listed AND browsable |
| 4 | UI removal first (dirty session control) | `pkill -9` | entry present | listed AND browsable |

Two corroborating measurements:

- The UI removal in arms 3 and 4 hit the disk file IMMEDIATELY (the entry was
  gone from `sources.xml` while Kodi kept running): save-on-modify, exactly as
  the source read predicts.
- The clean-shutdown "Saving settings" flush ran in arms 1 and 3 and did not
  touch `sources.xml`.

**Decision (plan 5.1 tree, first branch): class C SHIPS in phase 2** as the
single-step additive merge plus restart. The deleted `boxsetup.add_media_sources`
was correct. `bootstrapper/README.md:403`'s claim that sources "only stick if
written while Kodi is stopped" is false on Kodi 22 for in-process VFS writes;
bootstrapper is ARCHIVED (2026-08-29) so the file is not being edited, and this
paragraph is the recorded correction.

## E2: is there any live path to the settings level? NO

- Source: GUI-only setters, listed above.
- Bench: `Settings.GetSettings` at expert level returned 317 ids; none contains
  `settinglevel`.

**Decision: class B stays DROPPED.** `90-settinglevel.xml` does not enter the
bundle.

## E3: the confirmation-gated settings, and there are THREE of them, not one

The plan asked about `addons.unknownsources`. The bench found the same gate on
two more payload ids. All measured on this bench:

| Id | Dialog | Fires | Unanswered behaviour |
| -- | ------ | ----- | -------------------- |
| `addons.unknownsources` | "Add-ons will be given access to personal data ... Proceed?" | `OnSettingChanged`, POST-commit | calling thread blocks indefinitely (17.3 s in the run, until answered); value already true in memory |
| `services.webserver` | "Anyone who has access to the web interface ... Proceed?" | `OnSettingChanging`, PRE-commit veto | value stays false |
| `services.esallinterfaces` | "These services offer neither authentication nor encryption ... Proceed?" | `OnSettingChanging`, PRE-commit veto | value stays false |

The transport the set arrives on decides everything:

- **Remote TCP JSON-RPC: unusable for these three.** The dialog a TCP-thread
  set posts is INPUT-DEAD: `SendClick`, `Input.Right/Select`,
  `Input.ExecuteAction` and an EventServer keyboard event all bounced off it,
  focus never moved. Worse, `services.esallinterfaces` over TCP deadlocks the
  TCP server against itself (the set restarts the very server handling it):
  9090 kept accepting but never read again (Recv-Q pinned in CLOSE_WAIT), a
  later `Application.Quit` hung before "Saving settings", and the process
  needed SIGKILL, losing every live set of that session.
- **In-process (a script thread calling `executeJSONRPC`): works.** The dialog
  posts ABOVE an open `DialogProgress`, the script thread blocks in the set,
  and the dialog IS answerable by ordinary GUI input (measured: focus moved
  No to Yes, Select accepted). Answered YES: `addons.unknownsources` returned
  `true` and read back `true`; `services.webserver` returned `true`, read back
  `true`, and the web server was serving authenticated HTTP JSON-RPC seconds
  later; `services.esallinterfaces` returned `true`, read back `true`, and both
  the EventServer and the TCP server restarted and kept working (no deadlock:
  the restart was not initiated from the TCP server's own thread).

The measured failure mode differs from the `lookandfeel.skin` countdown in the
one way that matters: there is NO countdown and NO auto-revert on silence. A
missed or destroyed answer leaves the value at its OLD value (or, for
unknownsources, leaves the dialog blocking a worker thread), which read-back
detects and the result record reports. Corruption of the
`lookandfeel.skin`-on-atv2 kind is not reachable here.

**Decision: the three confirm-gated ids are live-set in-process from a worker
thread while the apply engine watches for Kodi's own confirm (verified by its
localized text, `xbmc.getLocalizedString(36618/36632/36633)`) and answers YES,
bounded, with read-back verification; a set that never returns is `timeout`, a
non-true return or failed read-back is `refused`.** The one confirm at the top
of the flow is the user's consent for all of it: the profile the user asked to
apply IS the thing these dialogs ask about.

### E3 second half: enablement while unknownsources is false. WORKS

`script.ezmtrial` was staged into `addons/` while Kodi was closed, discovered
by the startup scan, and reported `enabled: false`. With
`addons.unknownsources` still false, `Addons.SetAddonEnabled` returned "OK" and
`Addons.GetAddonDetails` immediately reported `enabled: true`.

**Decision: plan 7.4 steps 3 and 4 do NOT swap.** Staging and enabling need no
unknownsources precondition.

## The settle question (plan 7.4 step 4): answered

`guisettings.xml`'s mtime did not move across a batch of live sets plus a 3 s
wait: on Piers, a JSON-RPC set triggers NO asynchronous Kodi save
(`SettingsOperations.cpp` calls only `SetValue`; the flush is the clean-close
"Saving settings"). "Settle" is therefore a no-op on Piers; the
materialize-once, write-once, vector-once shape stands as the belt-and-braces
for the unclean kill, and there is no Kodi-initiated save to race. The unclean
kill genuinely loses memory-only sets (measured: the SIGKILLed session's
esallinterfaces=true came back as the stock false), which is exactly why the
file half exists.

## Other measurements the phases need

- **All 13 silent payload ids set cleanly over JSON-RPC with no dialog**, one
  call each, including the string enums (`locale.audiolanguage` "original",
  `locale.subtitlelanguage` "forced_only") and `system.playlistspath`. After a
  clean quit every one of them, plus the three confirm-gated ids, was in
  `guisettings.xml` with the sent value and no `default="true"` marker.
- **`system.playlistspath` is absent from `Settings.GetSettings` at every
  level yet fully gettable and settable.** A catalog captured from GetSettings
  alone would falsely reject it; the authoring-gate catalog
  (`tests/data/kodi22-setting-ids.txt`) is therefore the UNION of the
  GetSettings ids (317) and the ids Kodi itself wrote into the fresh profile's
  `guisettings.xml` (101 more, 418 total).
- **Kodi 22's stock `filelists.showparentdiritems` is `true`** (fresh-profile
  value and default both true). The current bootstrapper fragment pins true and
  says stock is true: consistent. `bootstrapper/defaults.txt` still says
  "Show parent folder items DISABLED" and is wrong about the tree it describes;
  plan open item 1 remains an owner call, and the bundle carries `true` (the
  current tree's value) until he says otherwise.
- **A dependent-setting refusal is real**: `Settings.SetSettingValue` returns
  the JSON-RPC error path (`InvalidParams`, per source `IsVisible()` gate) for
  an invisible setting; none of the current payload ids tripped it in fragment
  order.

## Phase 2 gate: the projection differential, run and CLEAN

Same day, after the engine landed. A THIRD fresh isolated-HOME first-run
profile (the second was deliberately burned; see the finding below), the built
zip plus its script.module dependencies staged from the official hub
artifacts, EZ Maintenance++ enabled over JSON-RPC, and the REAL flow driven
end to end: one confirm answered, the add-on answered Kodi's web server
warning ITSELF (observed on screen mid-flow, untouched by the driver), one
result message - "Settings profile applied. The media sources appear after
Kodi reopens." - in about 7 seconds, restart accepted, clean "Saving
settings" flush.

After the restart, measured over JSON-RPC:

- All 16 class A ids live with the bundle's exact values.
- All 3 sources present BY PATH in `Files.GetSources`, and the `.T7B` source
  BROWSES (`Files.GetDirectory` returned its entries). The profile's
  pre-existing `<video>` section survived the merge.
- Both add-ons enabled; the repository enable was the final apply op.
- The own-settings leaf carried the bench overlay's values with no
  `default="true"` marker.
- The boot check ran on the restart, verified sources and settings LIVE,
  logged `boot profile-check: applied profile verified live`, spoke nothing,
  and consumed the marker.
- Bundle values byte-compare IDENTICAL to `bootstrapper/settings/defaults.d`
  (minus class B), the one-off replacement for the retired launcher-adapter
  gate.
- Zero ERROR lines in `kodi.log`, captured against a zero-error baseline.

The idempotent RE-RUN, same box, 2.5 seconds: every item `already-correct`,
the "This box already matches the settings profile" acknowledgement, NO
restart offer, no storage writes.

### What the first full run caught, and what it changed

The first end-to-end run reported an honest PARTIAL (37 of 43): the original
fragment order enabled `services.webserver` BEFORE its password existed, and
`CNetworkServices::OnSettingChanging` vetoed it with an OK dialog (string
36635, "a password must be entered as well") that nothing watched - a 20 s
timeout, two cascade refusals, and an unanswerable modal left on screen that
then wedged the quit. Three durable fixes came out of it:

1. The bundle's services fragment now sets port, authentication, username and
   password BEFORE `services.webserver` - the parent-before-dependent rule's
   second measured instance.
2. EVERY class A set now runs bounded through the worker-and-watch path, not
   just the three confirm-gated ids: an unexpected OK dialog is captured as
   the refusal reason and closed; a foreign yes/no is closed unanswered; a
   set that never returns is `timeout`. "Either every set carries a bound, or
   class A does not ship as a loop" was the plan's own condition, now met
   literally.
3. `Network.MacAddress` returned the literal "Busy" into the marker stamp
   (Kodi's not-ready InfoLabel value); the writer now retries and records
   non-MAC stamps as empty, and the reader treats a non-MAC stamp as
   UNSTAMPED, never as another box.

## What changed against the plan's letter

- Bootstrapper's `settings/defaults.d/` grew since 2026-08-04: three new class
  A ids (`pvrplayback.delaymarklastwatched` 360, `system.playlistspath`,
  `lookandfeel.enablerssfeeds` true in a new `60-interface.xml`) and the locale
  values are now the measured office-box values ("original" / "forced_only"),
  not the plan's Appendix A ("English" / "none"). The bundle is authored from
  the CURRENT tree, so class A is 16 ids, not 13.
- `bootstrapper` was archived 2026-08-29: every plan step that edits it (the
  run-time bundle resolution in `bin/reset-kodi` / `bin/seed-kodi`, the launcher
  adapter, the byte-for-byte phase 1 gate, repointing `npm run validate` and
  `preview`) is SKIPPED, not silently dropped. The bundle data was instead
  verified value-for-value against `bootstrapper/settings/` at authoring time.
- `tools/resolve_profile.py` is not built: its only consumer was the launcher
  adapter. The flattening rules live once, in `profile.load()`, as pure
  functions that tests and any future consumer import.
