# Folding EZ Maintenance++ into skin.estuary.pov

Status: PROPOSAL, awaiting owner decision. Written 2026-08-28.

## What was asked

Fold `script.ezmaintenanceplusplus` (399 KB) into `skin.estuary.pov` (2.5 MB)
and reach it from a menu button. One artifact, not two. "The smallest most
compact Swiss Army knife of a skin."

Owner's stated drivers, all four at once: fewer artifacts to maintain, one-button
UX on the box, smaller total footprint, fewer install failure modes. Scope asked
for: everything, one artifact. Appetite: open to reworking restore if needed.

## Two findings that reframe the question

**Kodi will not stop you.** A skin add-on CAN declare `xbmc.python.pluginsource`,
serve `plugin://skin.estuary.pov/...`, and stay hidden from the add-on browser
with an empty `<provides>`. Measured in Kodi 22.0b1 source:
`AddonInfoBuilder.cpp:395-598` appends every `<extension>` to `m_types` and takes
`m_types[0]` as the main type; `AddonInfo.cpp:193-211` makes `HasType` true if
ANY extension matches, so one add-on can satisfy both the SKIN and PLUGIN
lookups; `Addon.cpp:637-649` picks the library of the type it was constructed as;
`PluginSource.cpp:44-57` inserts nothing for a PLUGIN with empty `<provides>`.
Put `xbmc.gui.skin` first. No add-on in this tree does this (17 skin `addon.xml`
files surveyed, zero declare pluginsource), but nothing forbids it.

**The boundary rule is already broken four times, deliberately.**
`skin.estuary.pov/addon.xml:13` hard-imports `plugin.video.pov` with a comment
calling it "a real install dependency, not a suggestion", and `xml/Home.xml`
hardcodes dozens of `plugin://plugin.video.pov/?mode=...` paths.
`skin.estuary8/addon.xml:53,63` imports two of its own add-ons, and its
`xml/DialogButtonMenu.xml:121-124` ships an `RunAddon(script.ezmaintenanceplusplus)`
row. So the rule as practised is narrower than the rule as worded: **a coupling
is acceptable when DECLARED (Kodi installs both together) or GUARDED (absence
degrades visibly), and forbidden when SILENT.** The 2026-07-22 `ezm.footer`
defect was silent, which is why it cost two emergency releases in one morning.

## What actually blocks the full merge

Not the rule, and not Kodi. One mechanism, measured three ways.

**Fresh Start refuses to run from any skin inside the wipe root.**
`default.py:600-617` resolves `special://skin/`, compares against
`special://home/`, and returns early with "Please switch to the default Estuary
skin before running Fresh Start." It checks by PATH, never by skin id,
deliberately, so the add-on stays skin-agnostic. `skin.estuary.pov` installs
under `special://home/addons/`, so a folded-in Fresh Start refuses to run from
its own host every time. And the moment the user obeys that instruction and
switches to stock Estuary, the menu button no longer exists, because Kodi
unloads a non-active skin's XML.

**The wipe deletes the skin.** `onetap.py:53-68` `_wipe_excludes()` keeps `temp`,
`backupdir`, `backup.zip`, five `script.module.*` deps, and the add-on's own id
via `getAddonInfo("id")`. No skin is on that list. EZM++ survives its own wipe
today only because it is a separate add-on with its own id.

**Restore runs the same wipe with Kodi still alive, and has no skin guard.**
`wiz.py:1402-1431` calls the same `onetap._wipe`; `onetap.py:84` records that
"restore CANNOT exit: it keeps Kodi alive for the whole zip extract." Restore
then writes the archive's `lookandfeel.skin` (`wiz.py:1031-1078`, `:1116`), whose
documented failure mode is the box landing on stock Estuary
(`_kodisettings.py:57-61`, and the atv2 incident quoted at `wiz.py:1062-1066`).

So folded in, the tool refuses to start; and with the gate removed it deletes
itself mid-operation while Kodi holds its XML open. The mandatory post-wipe
dialog (`ui.ask_terminate`, `ui.py:597-599`) also has no skin left to draw it.

**The existing lint does not protect against this.**
`tests/test_no_skin_specific_listitem_property.py` polices ListItem properties
only. A wholesale move sets no ListItem property, so it stays green while the
rule it exists to protect is comprehensively violated. Its silence is not
approval. The test that encodes the real constraint is
`tests/test_menu_tool_actions.py:2134-2168`
`test_freshstart_requires_stock_estuary_skin`.

## The size argument is real, but it is not about merging

The 399 KB zip is:

| Bucket | Bytes | Share |
| --- | --- | --- |
| `fanart.jpg` | 308,304 | 32.1% |
| `icon.png` | 52,290 | 5.4% |
| Python, `resources/lib/modules/` | 356,614 | 37.1% |
| Python, `default.py` + `service.py` | 70,420 | 7.3% |
| `changelog.txt` | 30,791 | 3.2% |
| `addon.xml` (mostly the `<news>` blob) | 23,114 | 2.4% |
| `resources/skins/Default/` | 41,315 | 4.3% |
| language, one locale | 2,351 | 0.24% |

**37.5% is artwork that only decorates menu rows** (`control.py:42-49`), and
about 105 KB more is optional feature code (`speedtest.py` 63 KB, vendored
`_vendor/qrcode/` 46 KB for the Dropbox sign-in QR). Language files are
negligible.

On the skin side `xml/` compresses 13:1, so adding XML and Python is nearly free;
the 1.2.0 trim (`estuary-pov` commit `015566a`) concluded that chasing dead
markup was worth under 10 KB while images and fonts were worth 1.9 MB.

**Most of the size win is available today without merging anything.**

## Recommendation: two stages, and do not start with the merge

### Stage 1, now, no risk to recovery

1. **Guarded launcher button.** New `<item>` in
   `estuary-pov/skin.estuary.pov/xml/Home.xml` between `:999` and `:1105`,
   modelled on the Add-ons item at `:1063-1071`:
   `<onclick>RunAddon(script.ezmaintenanceplusplus)</onclick>` with
   `<visible>System.HasAddon(script.ezmaintenanceplusplus)</visible>`. This is
   exactly the shipped `skin.estuary8/xml/DialogButtonMenu.xml:121-124` pattern.
   Guarded, so the skin works without the add-on and the add-on still works on
   stock Estuary. Add the matching `HomeMenuNo...Button` toggle in
   `xml/SkinSettings.xml` following the pattern at `:167`. Needs a small
   `icons/sidemenu/*.png`, 1 to 2 KB compressed.
2. **Strip the artwork and the optional modules.** Delete `fanart.jpg`, shrink
   `icon.png`, and decide whether speedtest and the QR vendor earn their 105 KB.
   Target roughly 150 KB, a bigger and safer saving than the merge produces.
3. **Add the missing boundary test to `estuary-pov`.** `estuary-7` has
   `tests/test_no_addon_awareness.py`; `estuary-pov` and `estuary-8` have no
   equivalent. If POV and EZM++ are now deliberate exceptions, encode exactly
   which ids are allowed and fail on any other, so the next coupling is a
   decision rather than an accident.

Install ordering is already in our favour and does not depend on file size: Kodi
applies updates sequentially in add-on id order (`AddonManager.h:37` is a
`std::map`, `AddonInstaller.cpp:625-647` installs synchronously in list order),
and `script.` sorts before `skin.`.

### Stage 2, only if Stage 1 leaves the goal unmet

A true single artifact is achievable, but it means **redefining what Fresh Start
is**, not relocating code. Today it means "bare Kodi". It would have to mean
"bare Kodi plus our skin":

- Add the skin id to `_wipe_excludes()` so the tool survives its own wipe, and
  accept that Fresh Start no longer returns a box to stock.
- Replace the `default.py:608` path gate, since running from our own skin becomes
  the point.
- Pin restore's skin handling so it never switches away (`wiz.py:1031-1078`,
  `:1116`), and decide what a cross-skin archive restores to.
- Rebuild every menu. `default.py` is a real `plugin://` directory
  (`addDirectoryItem` at `:784`, `endOfDirectory` at `:1061`, `?action=` routing
  at `:841`); folded in, `sys.argv[1]` is no longer a plugin handle, so the whole
  menu layer becomes `xbmcgui.Dialog().select()` or skin XML. The skin already
  has reusable shells: `Custom_1102_TextViewer` for output,
  `Custom_1101_SettingsDialog` and `Custom_1107_SearchDialog` for input.
- Rehome settings and state. `addon_data/script.ezmaintenanceplusplus/` becomes
  `addon_data/skin.estuary.pov/`, which is the directory Kodi's shutdown flush
  rewrites from in-memory skin settings, and which `wiz.py:62-66` currently
  protects as "the ONE addon_data". The `.ezm_restore_check` and `.ezm_pvr_paused`
  markers (`tools.py:426`, `:504`) survive the wipe today only because that
  directory is named after the add-on id.
- Fix `speedtest.py`'s hardcoded
  `special://home/addons/script.ezmaintenanceplusplus/...` launch path
  (`default.py:1008-1011`).
- Decide what happens to `service.py`. The skin has `xbmc.service`
  (`addon.xml:33`) with no `visible=` gate, so it runs even when the skin is not
  active, which helps here. But start order is scheduler-decided: measured
  inverted 3 times in 8 (`scripts/services.py:16-33`).
- There is no declarative settings schema in a skin. Every one of EZM++'s
  `resources/settings.xml` controls becomes hand-authored XML with
  `Skin.HasSetting`/`Skin.ToggleSetting`, strings only, with no false-versus-unset
  distinction and no per-setting reset.

This is a rework of the most dangerous code in the project, whose failure mode is
a bricked box, on a fleet with **no tvOS backup at all**
(`~/Kodi/Backup/tvos/` does not exist). If it is done, it is proven on a Fire TV
with adb and a verified archive before it goes near an Apple TV.

## Verification

Stage 1:

```sh
cd estuary-pov && python3 -m pytest tests/ -q
cd estuary-pov && python3 tools/build_skin.py skin.estuary.pov --check
cd ezmpp     && /opt/homebrew/bin/python3 -m pytest tests/ -q   # 666 tests
bin/check-all
```

On a box: the button appears on Home, launches EZM++, and is absent when the
add-on is not installed.

Stage 2, additionally: Fresh Start and clean-clone restore both proven end to end
on a wipeable Fire TV, with a verified archive taken first, before any Apple TV
sees it.

## Open question for the owner

`skin.estuary.pov` 1.2.2, published 2026-08-28, has a boot service that writes an
`_scproxy` shim into `script.module.requests/lib/` to repair POV, Multi Weather,
`script.openweathermap.maps` and EZM++. That is a skin repairing four add-ons.
`.claude/memory/project-kodi22-tvos27-atv1-blockers.md:200-204`, written three
hours before that commit, records the same placement as REJECTED on
boundary-rule grounds. Either it was reopened or the memory is stale and should
be corrected. It is the live precedent that most resembles this proposal, so
which it is changes how much licence Stage 2 has.
