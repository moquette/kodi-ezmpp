# Plan - Settings Profile: one command that customizes a fresh Kodi install

Status: history as of 2026-09-26. Phases 0 to 3 shipped: the Phase 0 results
are `settings-profile-experiments-2026-08-30.md`; the engine
(`resources/lib/modules/profile.py`), the bundle (`resources/profiles/house/`),
the menu row and the boot check landed 2026-08-30 (`d02da58`) and have shipped
since v2026.08.31.3, with the share-host and PVR share steps added 2026-09-26
(`../CLAUDE.md`, which is the living contract). Never written: `tools/resolve_profile.py`,
the `bootstrapper` adapter and its byte-for-byte gate (`bootstrapper` was
deleted 2026-08-31, superseded by `bootstrap-light/`), and Phase 4 ("Save this
box as a profile"; no such row exists in `default.py`). The §12 checklist is
left as it was written and is not a status.
Written: 2026-08-04. Revision 3, after two rounds of QA and architecture review.
Owner decisions in §1 are settled; do not re-litigate them.
Start at §12, the checklist. Phase 0 gates everything else.

## 0. The ask, in one line

A user installs Kodi, installs EZ Maintenance++, picks one menu item, and the box
comes up carrying the owner's essential settings: services, add-on policy, guide
behaviour, language, file manager sources, the T7B repository, and this add-on's
own backup folder.

`bootstrapper` already produces exactly that box, in 18 seconds, on a macOS
bench, by seeding files while Kodi is CLOSED. This plan is about producing the
same box from INSIDE a running Kodi, which is a different problem with different
mechanisms and a named failure class attached to it.

## 1. Owner decisions (settled 2026-08-04)

1. **The shipped profile is the owner's own settings.** The bundle carries the
   real values: the mini's NFS shares, the T7B repository, the `kodi`/`kodi` web
   server login. It is not a neutral sample.
2. **Those values are generic and shipping them publicly is accepted.** This was
   raised and accepted. It is not an open risk and is not to be re-raised at
   review time.
3. **EZ Maintenance++ owns the bundle.** `bootstrapper` becomes a consumer.

## 2. Goals and non-goals

### Goals

- One menu row, one confirm, one restart, one honest result message.
- Additive and idempotent. Safe on a fresh box and safe on a configured one.
  Re-running changes nothing that is already correct, and that outcome is
  DISTINGUISHABLE from "applied" in the result record, or the claim is untestable.
- Every value is data in a bundle, never a literal in code.
- Every applied value is verified, not assumed, and anything that cannot be
  verified before the restart is verified on the next boot.
- One source of truth shared with `bootstrapper`, with no second copy to drift.

### Non-goals

- Not a clone. This is not "restore a golden backup" (see 3.3).
- Not a wipe. It never removes settings, sources, add-ons or user data. Fresh
  Start owns destruction and stays untouched.
- Not automatic. It never runs at boot and is never offered at boot.
- Not a folder of options. One row (6.1).
- Not a bench provisioner. `settings/windowed.d/` in `bootstrapper` is bench-only
  and excluded from the bundle: `videoscreen.screen` of -1 and `screenmode` of
  WINDOW would put a television into a window.

## 3. Background

### 3.1 What `bin/reset-kodi` does, and why it is simple

Thirteen ordered steps, twelve of which run with Kodi not running:

1. Parse args. `--verify` runs the post-launch checks and nothing else, so a
   caller that owns its own launch reuses those checks instead of copying them.
2. Refuse to run while Kodi is up.
3. Announce the wipe targets and confirm.
4. Back the profile up to a tar.gz and verify the archive by listing it.
5. Harvest the saved NSWindow frame BEFORE the wipe. Two statements, not one
   pipeline: under `pipefail` a missing key killed the run silently.
6. Wipe profile, logs and plist. `defaults delete` plus `rm` plus
   `killall cfprefsd`, because cfprefsd rewrites the plist from cache.
7. Merge `settings/windowed.d` and `settings/defaults.d` into `guisettings.xml`.
   Fragments are standalone XML documents, glob order is precedence, xmllint
   validates every fragment and the merged whole, and no fragment carries
   `default="true"`.
8. Copy `sources.xml` and touch `.setup_complete`, which disables the macOS
   `preflight` perl script that used to overwrite sources on first run.
9. Stage `addons/<id>/` directories, xmllint-gating each `addon.xml`.
10. Fabricate `Addons33.db` and INSERT `installed` rows with `enabled=1`.
    `CAddonDatabase::SyncInstalled` only inserts rows for add-ons the table does
    not already list, so a pre-written row survives the first scan untouched.
11. Seed `userdata/addon_data/<id>/settings.xml`, stripping comments via xpath,
    because `CAddonSettings::Load` calls `Attribute("id")` on every child node
    without checking it is an element and a comment node is a SIGABRT on the
    first `getSetting()`.
12. Write the NSUserDefaults window frame. The content area must be exactly 16:9
    or `skin.estuary8` selects its 1920x1200 profile and the EPG grid draws a
    blank row.
13. Launch once, then verify over JSON-RPC: add-on state from
    `Addons.GetAddonDetails` (Kodi's own view, not the database we wrote),
    sources compared BY PATH rather than by name, plus a non-fatal add-on
    database schema drift check.

Four properties carry over regardless of mechanism: values are data not code;
every generated document is reparsed before it is trusted; results are verified
rather than assumed; and the work costs exactly one launch.

### 3.2 Why almost none of it ports

Steps 6 to 12 are all "write to disk while nothing owns the file". EZ
Maintenance++ runs inside a live Kodi, which puts every one of those writes into
the failure class already named in `repo/docs/playbooks/kodi-settings-clobber.md`:
a live component holds settings in memory and flushes them at a lifecycle event,
so a direct file write is either clobbered by that flush or an in-memory set is
lost because the flush never happens. Restore defect A was instance 4.

The design question is therefore not "how do I port reset-kodi" but, per payload
item, the playbook's own two questions: **is there a live setter, and does an
in-memory owner rewrite the file wholesale at a lifecycle event?**

### 3.3 Why this is not "restore a golden backup"

A backup is a CLONE: skin, every add-on, IPTV configuration, credentials,
version-fragile, device-class-bound. A profile is a PROJECTION: about twenty
deliberate keys, no add-on state that was not asked for, safe on a box whose
current contents must survive. Different tools. The profile is the one that
answers "new install, one command, essential settings".

## 4. The four payload classes

Split on the playbook's two questions, not on "does it have a setting id".

| Class | Payload | Live setter? | Wholesale flush by an in-memory owner? | Mechanism |
| ----- | ------- | ------------ | -------------------------------------- | --------- |
| A | Real `<setting id>` core settings | Yes, `Settings.SetSettingValue` | Yes, the settings manager rewrites `guisettings.xml` from memory | Live set for all ids, settle, then ONE file write plus ONE `persist_one` (4.1) |
| B | `general/settinglevel` | No | Yes, same document, same flush | No viable mechanism inside a running Kodi. Dropped unless E2 finds a live path (4.2) |
| C | File manager sources | No | Unknown, pending E1 | Additive MERGE of entries through the VFS, plus `persist_one` (4.3) |
| D | Add-ons and their `addon_data` | Partly: enablement yes, `addon_data` no | Yes, per add-on, once it is enabled and has loaded its settings | Write `addon_data` BEFORE enabling; stage, refresh, enable, poll (4.4) |

### 4.1 Class A

Everything in `settings/defaults.d/20-services.xml`, `30-addons.xml`,
`40-media.xml` and `50-language.xml`.

**The apply shape is materialize-once, write-many, vector-once.** A per-item
"live set, then file write, then `persist_one`" loop is BROKEN on tvOS and racy
everywhere else, and both halves of that are already recorded in this tree:

- `nsud.persist_one` deletes the POSIX copy after a confirmed read-back
  (`nsud.py:337`). `_kodisettings.write_guisetting` returns `False` immediately
  when the file is absent (`_kodisettings.py:141-142`). So on an Apple TV every
  class A file write after the first silently no-ops, and returns a bare `False`
  that nothing surfaces.
- The premise that a live `SetSettingValue` makes Kodi save `guisettings.xml`
  (`wiz.py:1069`) means a per-item loop interleaves thirteen of our own full-file
  ElementTree rewrites with thirteen Kodi-initiated saves of the same path. Kodi
  silently ignores a malformed `guisettings.xml` in full.

The correct sequence is already implemented twice, with the reason written down
at `wiz.py:1188-1193`: re-materialize the file from the VFS first (never a stub,
which would wipe every other setting), edit, and take ONE `persist_one` at the
end.

So: all live sets in fragment order, settle, re-materialize, one merged write,
one vector.

**Two ordering traps** the launcher solves by filename prefix and the apply engine
must preserve: `services.esallinterfaces` is a no-op unless `services.esenabled`
is already true, and Kodi can refuse a `SetSettingValue` outright when a parent
condition is unmet, so a dependent applied before its parent fails silently.

**A third refusal mode is suspected and must be measured (E3):** Kodi gating a
change behind a dialog nothing can answer. There is hardware evidence for this
class already (`_kodisettings.py:57-64`, `lookandfeel.skin` on atv2, 2026-07-17),
and `addons.unknownsources` raises a warning confirmation in Kodi's own settings
UI. Whether that gate lives in `OnSettingChanging`, and therefore fires for a
JSON-RPC set too, is measured, not assumed.

**Never-apply ids.** `profile.py` IMPORTS `_kodisettings._BOOT_STATE_ONLY`
(`_kodisettings.py:85-87`) rather than restating it. Two copies of a predicate
drifting is the failure that forced `restorecheck` to import
`nsud._is_skin_menu_sidecar`, and the contract already names it. Bundle
validation REJECTS a profile containing any of those ids, and rejects any
fragment carrying `default="true"`.

### 4.2 Class B

`general/settinglevel` lives in its own `<general>` block, not as a setting id.
`bootstrapper/README.md:191-193` records that it has no JSON-RPC path. Its
fragment carries a load-bearing empty `<viewstates />` stub, because
`CViewStateSettings::Load` returns early without it.

**This tree already argues the outcome.** `tools.py:439-442` and `wiz.py:1055-1058`
both record, with a source citation, that a clean shutdown serializes
`guisettings.xml` from LIVE memory. The settings level is serialized into that
same document and has no live setter. A file-only write therefore loses at the
next clean close, and it loses whether that write happens now or on a later boot,
because `service.py` runs after Kodi has loaded its settings (`__main__` at
`service.py:540`). It would survive only an unclean kill, which is the normal
Fire TV close and not the Apple TV one: it would appear to work on Fire TV and
fail nondeterministically on Apple TV, which is the worst possible outcome.

**Decision, taken now rather than left open:** class B is DROPPED from the bundle
unless E2 finds a live path. Phase 2 is not gated on it. E2 still runs, because a
live path would be cheap to use if it exists, but the plan does not depend on it.

### 4.3 Class C

**The bundle carries source ENTRIES, never a document.** `bootstrapper`'s
`settings/sources.xml` is a complete `<sources>` document with stubs for every
media section; copying it onto a configured box DELETES every source that box
already had, which contradicts the additive guarantee in §2 outright. The
launcher may write it whole because it runs against an empty profile. The add-on
may not.

**The correct algorithm already shipped to this fleet for two months** and was
deleted with `boxsetup.py` in `e52d170`. Port it back rather than re-derive it
(`git show e52d170^:script.ezmaintenanceplusplus/resources/lib/modules/boxsetup.py`,
`add_media_sources`). Its four load-bearing properties, all of which the naive
version loses:

1. Reads through `xbmcvfs`, explicitly not POSIX, because on tvOS a plain read can
   see a stale or dropped disk copy and make the merge clobber existing sources.
2. Merges into `<files>`, deduping on name AND path.
3. Consolidates multiple sources on the same URL into one `.T7B` entry. The
   name-only dedupe in the add loop does not catch a same-URL duplicate; this was
   audit Finding G.
4. Ends in `nsud.persist_one("sources.xml", ...)`. `nsud._should_vector`
   (`nsud.py:104`, rationale at `:473`) puts top-level `userdata/*.xml` in scope
   and names sources explicitly. Without the vector the write is invisible to
   Kodi on an Apple TV, with no error raised.
5. Gates BOTH the write and the vector behind `if added or renamed:`. Without
   that guard a second run rewrites `sources.xml` and re-vectors it, which on
   tvOS is a key rewrite plus a POSIX drop: a real mutation on a run that changed
   nothing, and the §2 idempotency claim would be false at the storage
   layer even though the file content is identical.

Two smaller details to carry across with it: the `<default>` element insert when
the `<files>` section lacks one, and the fact that it writes `ET.tostring(...)`
with no XML declaration.

Two path rules the bundle validator enforces: never a port, because Kodi's own
browse dialog hands back `nfs://<host>:2049/...` which both breaks directory
listing and registers as a different source; and never a missing trailing slash,
because Kodi dedupes on the exact path string.

**What E1 decides** is only whether the write survives to the next boot, which is
narrower than "the two repos contradict each other". Both repos agree Kodi caches
sources at startup. They disagree only about whether a clean-shutdown flush
rewrites the file. See 5.1 for the decision tree, including the branch where
class C leaves the add-on entirely.

### 4.4 Class D

Writing `Addons33.db` is unavailable: Kodi caches enablement in memory, so the
row would be ignored and then overwritten. The live equivalent, using only calls
this tree already makes:

1. Write the add-on's `addon_data` FIRST, while nothing owns it, through the same
   write-then-`persist_one` path as everything else, and with comments stripped.
   Both of those are load-bearing and neither is optional:
   - `nsud._should_vector` puts `addon_data/<id>/settings.xml` IN scope
     (`nsud.py:171`), so a plain write is invisible to Kodi on tvOS with no error
     raised. The chokepoint lint may not catch it either: `_mentions_userdata`
     needs a string constant containing `addon_data` or a `*_path()` helper name,
     and a path built from bundle data is neither.
   - `CAddonSettings::Load` walks every child node and calls `Attribute("id")`
     without checking it is an element, so a comment node is a SIGABRT on the
     first `getSetting()`. The bundle is hand-authored and the launcher's
     equivalent file is a sixteen-line comment block around two settings, which
     is exactly the shape that would be copied in.

   **If the target add-on is ALREADY ENABLED**, which is the normal state on a
   configured box and on every idempotent re-run, writing the file is not
   available: Kodi has loaded that add-on's settings into memory and flushes them
   over the file at the clean shutdown `ask_restart` triggers. Two options, and
   silently writing anyway is not one of them:
   - a bounded disable, settle, write, re-enable in a `finally`, which is
     Mechanism B from the playbook and is what the restore path already does for
     pvr.iptvsimple. This makes it a second sanctioned add-on toggle, so §9
     must cover it.
   - or detect already-enabled and report that leaf as NOT applied with the
     reason, which keeps the reporting contract honest at the cost of the feature
     on that leaf.

   The choice is owner-gated and is open item 6.
2. Copy the add-on directory into `special://home/addons`.
3. `xbmc.executebuiltin("UpdateLocalAddons")` (`wiz.py:1650`, `default.py:668`).
4. `Addons.SetAddonEnabled` (`service.py:483`, `wiz.py:2358`).
5. Confirm with `Addons.GetAddonDetails`, on a bounded poll rather than a single
   immediate read.

**`InstallAddon` is not the default path.** Nothing in this tree calls it; it
routes through `CAddonInstaller`, can present a modal confirm (which violates the
no-prompt rule in 6.2), and runs on a background thread so an immediate
`GetAddonDetails` races it. It also cannot work at the moment it would be called:
enabling `repository.tony7bones` does not fetch its index, so until Kodi retrieves
`https://tony7bones.github.io/static/addons.xml` there is nothing to resolve. If
it is wanted later it needs an explicit repo refresh plus a bounded wait, measured
first. `script.image.resource.select` requires only `xbmc.python`, so staging is
dependency-free today and the install path buys nothing but a new failure mode.

**Enable the T7B repository LAST**, after the apply and immediately before the
restart offer. The hub is where this add-on's own release is advertised, and
`general.addonupdates` is not in the payload, so Kodi is free to update EZ
Maintenance++ while EZ Maintenance++ is mid-apply.

That BOUNDS the window; it does not close it. `ask_restart` has a "Later" branch
that returns False and does nothing (`ui.py:637-654`), so the box can sit for a
long time with the repo enabled, `general.addonupdates` at Kodi's default, and
this add-on's Python still executing. That is the intended steady state and is
acceptable, but `general.addonupdates` should be in or out of the bundle by
decision rather than by omission (open item 7).

**This add-on's own settings** (`download.path`, `restore.path`) are set with
`xbmcaddon.Addon().setSetting()`, which updates the live store and the file
together and avoids the raw-write path entirely.

**Dependency resolution** is not implemented in phase 2. Neither current entry has
a non-`xbmc.*` `requires`, so it is a forward risk rather than a gap; the launcher
already solved it in `kodi_repo_requires` and that is where to look when a bundle
first needs it.

## 5. Phase 0: the experiments that gate the design

Each runs on the macOS bench and, where the answer is platform-specific, from
INSIDE Kodi rather than from a shell: `xbmcvfs` is in-process, and on tvOS
`sources.xml` is vectored while on the bench it is a plain file, so a shell trial
cannot speak for tvOS or Android.

**Do the source read first.** This project settles questions like these from
Kodi's source everywhere else (`CAddonDatabase::SyncInstalled`, the `preflight`
perl script, `CViewStateSettings::Load`, `CAddonSettings::Load`). Read
`MediaSourceSettings.cpp` and `Application.cpp` for who calls
`CMediaSourceSettings::Save()` and when, and check whether `CViewStateSettings` is
an `ISubSettings` of `CSettings::Save`. A source answer generalizes to all three
platforms; a bench trial does not. Then confirm on the bench.

Results are written into `docs/settings-profile-experiments-<date>.md` in this
repository, whatever they say.

### 5.1 E1 - does a live write to `sources.xml` survive to the next boot?

Four arms, because one arm cannot close the question:

| Arm | Session state | Close | What it isolates |
| --- | ------------- | ----- | ---------------- |
| 1 | untouched sources | clean quit | the plain case |
| 2 | untouched sources | unclean kill (`pkill -9`, or `am force-stop` on a Fire TV) | whether the flush is the only loss mechanism |
| 3 | a source added or removed through the UI first | clean quit | whether a dirty in-memory list is what triggers a save |
| 4 | dirty list | unclean kill | control |

Each arm ends the same way, and ends at USABLE rather than at "the file looks
right": relaunch, `Files.GetSources` compared by path, then `Files.GetDirectory`
to prove the source browses. `bootstrapper/README.md:395-400` already has those
exact commands.

Decision tree:

- **Survives arms 1 and 3:** class C is a single-step merge plus restart, the
  deleted `boxsetup` was correct, and `bootstrapper/README.md:403` needs
  narrowing.
- **Lost in arm 1, kept in arm 2:** the write is real but the clean-shutdown flush
  eats it. Class C cannot ship in phase 2 without a mechanism that avoids a clean
  close, and a self-initiated hard exit is out of scope and needs its own owner
  decision. Class C is deferred to a later release, not bodged.
- **Lost in all arms:** class C leaves the add-on. There is no deferred fallback
  (5.3).

### 5.2 E2 - is there any live path to the settings level?

Reframed from "does the file write survive", which 4.2 already answers, to the
only question still open: does a live path exist at all? Check whether a settings
id for it appears in `Settings.GetSettings` at expert level, whether any builtin
sets it, and whether `CViewStateSettings` participates in `CSettings::Save`.

Found: class B ships with class A in phase 2. Not found: class B stays dropped.

### 5.3 There is no deferred-write fallback, and why

An earlier revision proposed recording a pending payload and having `service.py`
perform the write on the next boot. That is deleted. It cannot work, for a reason
that applies to both B and C: Kodi reads `guisettings.xml` and `sources.xml` at
STARTUP, before any Python service can run, and the same shutdown flush that ate
the first write eats the second. Two restarts buy nothing the in-session write did
not.

It is also the wrong shape historically. An every-boot re-assert was rejected by
unanimous adversarial review on 2026-07-08 and that verdict stands. A one-shot
marker genuinely differs from what was rejected on two of the three kills, but
kill 2 lands unchanged: on tvOS, non-`.xml` files under `addon_data` live in the
purgeable Caches tree, so the marker is not durable across a power-off.

### 5.4 E3 - does a live set of `addons.unknownsources` raise an unanswerable confirmation?

Live-set it from a script with a progress dialog owning the screen, and record
whether it prompts, hangs, or returns false. If it prompts, that id needs the
same treatment as `lookandfeel.skin` and the plan needs a documented answer for
it, because it is not optional payload: `addons.updatemode` only takes effect
while it is true.

**The same experiment answers a class D question**, in one extra JSON-RPC call:
does `Addons.SetAddonEnabled` succeed on a hand-staged directory while
`addons.unknownsources` is FALSE? `bin/reset-kodi` proves the staged-and-enabled
path works, but only on a profile where `unknownsources` was already true before
the first launch (seeded at `:353`, launch at `:498`). There is no evidence for
the negative case, and 7.4 enables add-ons at step 3 while `unknownsources` is
not set until step 4.

## 6. The user-facing design

### 6.1 One row, and why

Add exactly one row to the main menu. Not a folder.

"Set up this box" was a folder of five items of which one was ever used, and it
was deleted for that reason in `e52d170` on 2026-07-22. Rebuilding a folder
rebuilds that outcome. The name is burned and must not be reused: a stale
favourite pointing at the old action still routes to a silent no-op.

Working name: **Apply Settings Profile**.

The Backup and Restore rows carry their folder beside them, but that pattern does
NOT transfer here: those are `control.selectDialog` rows padded by `_menu_rows`
(`default.py:947-955`), while the root menu is a plugin directory built with
`CreateDir` (`default.py:37-93`) where label2 rendering is skin-dependent, and
`tests/test_no_skin_specific_listitem_property.py` exists to stop exactly that
coupling. The profile name goes in the confirm dialog, not beside the row.

### 6.2 The flow

1. Confirm, naming what will change in plain terms and stating that nothing is
   removed.
2. Progress dialog with real step messages.
3. Apply, in the order of 7.4.
4. Verify in-flow (7.6).
5. ONE result message. A partial result is reported as partial, never as
   complete, per the reporting contract.
6. `ui.ask_restart`, which already words itself correctly per platform.

No questions during the flow. Every mid-flow prompt this project ever shipped has
since been removed, twice, and the pattern is settled.

### 6.3 Wording rules

Counts and paths go to the log; the screen gets one message in the owner's
language. Class C is not applied until the restart, so the in-flow message says
sources take effect after the box reopens rather than reporting them as applied.
Nothing promises a state the mechanism cannot keep if the owner powers the box off
instead of reopening it.

## 7. Implementation

### 7.1 Bundle format and location

The bundle lives at `script.ezmaintenanceplusplus/resources/profiles/`, INSIDE the
add-on directory. `tools/build.py` walks `ADDON_DIR` and nothing else, so a
`profiles/` at repository root would ship a reader with no data.

```text
resources/profiles/house/
  profile.json                       # name, bundle version, schema version
  settings.d/20-services.xml         # class A fragments, glob-ordered
  settings.d/30-addons.xml
  settings.d/40-media.xml
  settings.d/50-language.xml
  sources.xml                        # class C ENTRIES only (§4.3)
  addons.list                        # class D: id plus method
  addon_data/<id>/settings.xml       # class D config, device-neutral leaves only
  overlays/fireos/...                # per-class, merged last
  overlays/tvos/...
  overlays/bench/...
```

Rules:

- Fragments are standalone `<settings version="2">` documents merged in glob
  order, as `lib/kodi-settings.sh` does today.
- **Precedence order and apply order are two axes, not one.** `kodi_merge_dir`
  concatenates children and relies on Kodi's last-wins parse; it never dedupes.
  If an overlay overrides `services.esenabled`, a naive last-wins dedupe moves
  that id to the END of the apply order, after `services.esallinterfaces`, and
  silently breaks the parent-before-dependent rule 4.1 calls load-bearing. The
  rule is: **value from the last occurrence, position from the first.**
- The class C document carries source entries only. It is never copied onto a box.
- **Device-scoped leaves are overlay-only and absent from the base.** The backup
  path is the live example: `settings/addon_data/script.ezmaintenanceplusplus/settings.xml`
  in `bootstrapper` hardcodes the `fireos/` leaf and its own comment says a tvOS
  transport means a per-class variant, not an edit in place. Under a plain
  base-plus-overlay shape a box whose class does not resolve would silently
  inherit `fireos/`, and an Apple TV would write its backups into the Fire TV
  folder with nothing to notice. Validation FAILS when the running device class
  has no overlay.
- Three device classes exist, not two: `fireos`, `tvos`, `bench`. The bench is a
  real consumer and phase 2's gate cannot be met without it.
- The whole bundle is validated before anything is applied. Validation failure
  applies NOTHING; there is no partial-bundle mode. Validation is STRUCTURAL and
  offline: it parses, it checks the rules above, and it needs no running Kodi.
- **No `addon_data` document may contain a comment node.** Enforced at load, for
  the `CAddonSettings::Load` SIGABRT in 3.1 step 11. Under the apply order in 7.4
  this crash would land INSIDE the flow: step 2 writes the file, step 3 enables
  the add-on, and the add-on dies on its first `getSetting()`.
- **Unknown setting ids are caught at bundle-authoring time, not at runtime.** An
  earlier revision validated every id against the live `Settings.GetSettings`
  catalog inside `load()`. That is wrong twice over: it destroys `load()`'s purity
  and takes the most valuable unit test with it (7.3, 8.1), and it makes one
  renamed id in a future Kodi abort the ENTIRE apply, sources and add-ons
  included, on a box where the launcher's behaviour is the opposite (write the
  XML, let Kodi ignore what it does not know). The check belongs in ezmpp CI
  against a captured catalog. At runtime an unknown id is a per-item `unknown-id`
  outcome (7.5), not a bundle failure.
- **Device class detection:** tvOS is `System.Platform.TVOS`, as `nsud._is_tvos`
  already does; Fire TV is `System.Platform.Android`; the bench is neither. Worth
  writing down now that an unresolved class is a hard validation failure.
- `overlays/bench/` is authored DELIBERATELY. The bench is seeded with the
  `fireos/` backup leaf today, so phase 1 changes what the bench gets unless the
  bench overlay reproduces it on purpose.
- `overlays/tvos/` has NO launcher consumer: `bin/seed-kodi` is adb-only and
  therefore Fire TV only. The Apple TV hardware run in 8.7 is its only proof.

### 7.2 One copy, not two

An earlier revision had `bootstrapper` vendor a generated copy with a
consumer-side drift check. That does not hold up: when this was written `bootstrapper` had **no git
remote and no CI**, and its only entry point was `npm run validate`, typed by
hand. It gained a remote on 2026-08-06 (`moquette/kodi-bootstrapper`, private)
and still has no CI, so the conclusion stands until one exists. A check nobody runs is not a guard, and this project has already lost weeks
to hand-synced copies drifting.

**Remove the second copy.** `bin/reset-kodi` and `bin/seed-kodi` resolve the
bundle at run time from the ezmpp checkout (`EZMPP_BUNDLE`, defaulting to the
known path) and hard-fail when it is absent. There is then nothing to drift.

Two copies of DATA drift invisibly: a value differs and nothing surfaces it until
a box misbehaves. One source with two consumers drifts BEHAVIOURALLY, and 8.6
measures behaviour. The data is what carries the LAN paths, the credentials and
the trailing slashes Kodi dedupes on, so the data is the thing to make
un-drift-able.

**Resolution and validation happen in the argument-parsing phase**, above
`bin/reset-kodi:286`. As the script stands, the confirm is at `:296-306`, the
wipe at `:335`, and nothing touches the payload until `kodi_merge_dir` at
`:352-353`, with the sources existence check as late as `:389-397`. A moved ezmpp
checkout would therefore leave the bench WIPED AND UNSEEDED, a failure mode that
cannot exist today because the payload lives in the same repo. `bin/seed-kodi`
happens to be safe by layout, which is luck rather than a rule, so state the rule
for both.

**One overlay resolver, in ezmpp.** `tools/resolve_profile.py --device-class <c>`
emits a FLATTENED payload; bash consumes flattened output and never merges.
Otherwise value-from-last / position-from-first and the fail-on-unresolved-class
rule would exist in three places and two languages, including the rule whose
failure mode is a silent one: an Apple TV writing its backups into the Fire TV
folder.

The launcher then needs a thin adapter, not a vendored tree, because it consumes
the same flattened data in a different form: it writes a whole `sources.xml`
(correct against an empty profile) where the add-on merges entries, and it writes
`installed` rows directly where the add-on enables through Kodi. Those two
transforms are the adapter's entire surface, both one-way, both exercised by the
differential. It becomes drift only if it grows into a second implementation of
bundle SEMANTICS, which the flattening rule prevents.

**Consequences to accept deliberately, not discover on the first run:** the
launcher's `npm run validate` xmllints `settings/*.d/*.xml`, `settings/sources.xml`
and `settings/addon_data/*/settings.xml` by literal path, and `npm run preview`
reads `settings/defaults.d/*.xml`. Both break when those files stop existing
there. `package.json`'s `files:` and `bin:` entries also stop describing a
self-contained tool, and `bin/reset-kodi:88-90` exists specifically so the script
works when invoked as `~/.local/bin/reset-kodi`. The trade is worth it, but it is
a trade.

If the owner later wants the launcher to work without an ezmpp checkout, the
fallback is a generated tree committed INSIDE ezmpp with a determinism test in
ezmpp CI (the `./build.sh --check` precedent), and the launcher's copy reduced to
a literal `cp -R` that `npm run validate` diffs. Not the reverse.

### 7.3 Module layout

New module `resources/lib/modules/profile.py`.

**Correction to an earlier revision:** the chokepoint lint does NOT need
`profile.py` added by name. `test_no_raw_userdata_writer._py_files()` rglobs every
`.py` under the add-on, and the only by-name sets are `CHOKEPOINT = {"nsud.py"}`
and `ALLOWLIST`, which is an EXEMPTION list. Adding `profile.py` to `ALLOWLIST`
would be the exact opposite of the intent. `profile.py` is covered the moment it
exists.

**What the lint genuinely cannot see must be fixed before `profile.py` is
written.** `_is_write_call` matches only callables named `open` or `File`
(`tests/test_no_raw_userdata_writer.py:85-103`). `_kodisettings.write_guisetting`
persists with `tree.write(...)` (`_kodisettings.py:155`) and calls no nsud
function, so it is invisible to the lint today. A `profile.py` that does its class
A file half through an ElementTree helper and forgets the vector passes the lint,
passes all 670 tests, and fails only on Apple TV. That is the 2026-07-13
`boxsetup._write_weather_settings` shape with a different verb.

So: extend `_is_write_call` to recognise `ElementTree` and `tree.write`, or add a
companion rule that any function calling `write_guisetting` must also call an
`NSUD_CALLS` member. Ship that extension with a failing-first test before
`profile.py` lands.

Public surface, kept small:

- `load(bundle_dir, device_class, known_ids=None)` returns a validated bundle or
  raises. The device class is a PARAMETER, not read from the platform inside, or
  the overlay merge is not a pure function and the most valuable unit test is
  unavailable. `known_ids` is injected the same way if a runtime catalog check is
  ever wanted; `load()` never reads it from Kodi itself.
- `plan(bundle)` returns the ordered operation list without performing anything.
- `apply(plan, on_step)` performs them and returns a result record. `on_step(i, n,
  text)` is a callback, not a progress object, so `profile.py` stays importable
  without `xbmcgui`, matching the `rlog` seam `wiz` already uses.
- `verify(plan)` re-reads live state and returns per-item outcomes.

Marker helpers join the other three in `tools.py` rather than being invented in
`service.py`.

`default.py` gains one route. `service.py` gains the boot check in 7.7.

### 7.4 Apply order

1. Load and validate the whole bundle: every document parses, no id in the
   imported never-apply set, no `default="true"`, no unknown setting id, an
   overlay exists for this device class, and every source path is port-free with a
   trailing slash.
2. a. THIRD-PARTY `addon_data`: file write plus `persist_one`, comment-free,
   BEFORE the add-ons that own them are enabled, with the already-enabled case
   handled per 4.4. Note this is unexercised writer code on day one: the current
   payload has no third-party `addon_data` at all, which is exactly the shape the
   chokepoint lint exists for.
   b. THIS add-on's own settings: `setSetting()` only. EZ Maintenance++ is
   already enabled and running, so "before enablement" is meaningless for it and
   a file write is wrong. These two must not be read as one rule.
3. Class D staging, `UpdateLocalAddons`, and enablement, with a bounded poll on
   `Addons.GetAddonDetails`. The T7B repository is enabled LAST, at step 6.
   **Steps 3 and 4 may have to swap, pending E3.** If Kodi refuses to enable a
   hand-staged add-on while `addons.unknownsources` is false, then that one class
   A id has to be live-set before any staging, and the sequence gains an exception
   that must be written down rather than discovered.
4. Class A: all live sets in fragment order, settle, re-materialize
   `guisettings.xml` from the VFS, one merged write, one `persist_one`.
   "Settle" needs a definition before this is coded: Kodi's own save after a
   `SetSettingValue` is asynchronous, so it is either a bounded wait or an
   explicit acknowledgement that Kodi's save is authoritative and our write is
   belt-and-braces for the unclean kill. With class B dropped both writers carry
   the same values, so the risk is low, but an undefined step in a sequence this
   load-bearing is how the last one went wrong.
5. Class C: read the live `sources.xml` through the VFS, merge the bundle's
   entries with name-and-path dedupe and same-URL consolidation, write, one
   `persist_one`.
6. Enable the T7B repository.
7. Verify, report, offer the restart.

**Correction to an earlier revision:** the reason for putting class C after class A
is NOT that a live `SetSettingValue` could interleave with it. That save writes
`guisettings.xml`, a different file with a different owner, and cannot touch
`sources.xml`. A misleading rationale is what armed defect A (`wiz.py:1060`), so
the real rule is stated instead: **class B, if it ever ships, must come after all
class A live sets**, because each such save rewrites `guisettings.xml` wholesale
from memory and would erase a file-only `<general>` block written earlier.

Each step is guarded and logged. A failure never aborts the box and never leaves a
step silently half-applied; it is recorded and surfaces in the result.

### 7.5 The result record

`_kodisettings.apply_guisettings` cannot produce what §6.2 promises. It
silently skips ids Kodi does not know (`_kodisettings.py:107`), silently skips
values already equal (`:115`), and returns a bare count (`:123`). Under the
reporting contract a count is not a result, and an idempotent re-run legitimately
returns zero. The applier is therefore NEW code with a new test surface, not a
rename, and it returns per-id outcomes:

`applied` / `already-correct` / `refused` / `unknown-id` / `timeout` / `error`

`already-correct` being distinct from `applied` is what makes the idempotency
claim in §2 testable at all. `unknown-id` is reachable at runtime by
design (7.1): the authoring gate catches it in CI, and a live catalog that has
moved under a validated bundle reports per-item rather than aborting the apply.

`timeout` exists because E3 explicitly anticipates a hang. `xbmc.executeJSONRPC`
is in-process and blocking, and 7.4 step 4 runs every live set in a loop with a
progress dialog owning the screen, which is the atv2 configuration that produced
the `lookandfeel.skin` finding. Either every set carries a bound, or class A does
not ship as a loop until E3 comes back clean.

**The vocabulary is per-ITEM, not per-id**, because class C and class D have no
ids. Class C reports `already-correct` when `if added or renamed` is false, which
is the outcome that makes 4.3's fifth property observable rather than merely
asserted. Class D reports per add-on and per `addon_data` leaf.

Note for open item 1: `filelists.showparentdiritems` is currently set to a value
that may equal Kodi's own default, in which case `GetSettingValue` alone cannot
distinguish "applied" from "never ran". The three-state record is what covers
that.

### 7.6 In-flow verification

- **Class A:** read every applied id back with `Settings.GetSettingValue` and
  compare against the value sent, coerced to the same type.
- **Class D:** `Addons.GetAddonDetails` with the `enabled` property, on a bounded
  poll. Kodi's own view, not the state we wrote.
- **Class C: NOT verifiable in-flow.** `Files.GetSources` returns Kodi's
  in-memory list, populated at startup, so a perfectly correct write reads back
  as ABSENT and the by-path comparison would emit a false PARTIAL in the one
  place the contract forbids a false verdict. That is the same species as the
  documented `Skin.HasSetting` trap (`service.py:306-308`, `wiz.py:969`): a probe
  that does not measure what it appears to. In-flow, class C is checked by
  consuming `persist_one`'s RETURN VALUE rather than re-implementing the
  read-back: it already re-reads through the VFS on tvOS and returns False on an
  unconfirmed vector. `_apply_boot_skin` (`wiz.py:1120-1131`) shows how to weigh
  that False as a tvOS-only warning rather than a general failure. Live
  confirmation moves to 7.7.
- **Class B:** not applicable while it is dropped.

### 7.7 The boot check

Reuse the right precedent. `_read_stale_purge_marker` and
`_write_stale_purge_marker` (`service.py:377-396`) are hard-wired to one marker
constant and take no path; the reusable pattern is `tools.RESTORE_CHECK_MARKER`
with `mark_restore_check_pending` and `clear_restore_check_marker`
(`tools.py:425-490`).

Constraints, all of which come from bugs this project has already paid for:

- The marker is a DOT-FILE in this add-on's `addon_data`, never `setSetting` and
  never `settings.xml`. `service.py:364-370` states why, and `nsud._should_vector`
  (`nsud.py:169-171`) vectors `addon_data/<id>/settings.xml` on tvOS while leaving
  a dot-file alone.
- **The marker is STAMPED with the box that wrote it, and the reader validates
  the stamp.** A full backup captures everything under this add-on's `addon_data`
  except its own `settings.xml`, so a backup taken between apply and restart would
  otherwise make a SECOND box run this box's pending check. Excluding it by name
  was the other option and is rejected: it contradicts the "Full means full"
  bullet in CLAUDE.md, whose only sanctioned exclusions are this add-on's
  `settings.xml` and `special://home/temp` at the root, and would need a third
  contract amendment. The stamp needs none. Note the same exposure already exists
  for `tools.RESTORE_CHECK_MARKER` and `PVR_PAUSE_MARKER`; this plan does not fix
  those, it just declines to add a third.
- The GUI wait sits OUTSIDE the try/finally so an aborted boot does not burn the
  one-shot marker (`service.py:286-289`, consumed at `:346-350`), and the whole
  check is abort-gated throughout (`service.py:192-221` documents the five-second
  shutdown kill).
- **The marker is cleared on the first boot REGARDLESS of outcome**, as
  `_maybe_restore_check` does. That is what bounds the case 5.3 leaves open: an
  owner who picks "Later" in `ask_restart` (which returns False and does nothing,
  `ui.py:637-654`), changes one of these settings by hand, and reboots later would
  otherwise be told repeatedly that the profile did not stick. Read-only nagging
  is a weaker version of defect A3 and "cleared either way" closes it.
- **A failed marker write is logged loudly**, as `_maybe_purge_stale_nsud_keys`
  does (`service.py:455-459`). Section 6.3 promises the owner that sources take
  effect after the reopen; if the marker write fails silently, nothing ever checks
  that promise.
- Class C confirmation is `Files.GetSources` by path. `Files.GetDirectory` is NOT
  a verdict-bearing check here: two of the three entries are
  `nfs://<SHARE_HOST>/...` (the mini, `sharehost.SHARE_HOST`), so when the mini is powered off that call blocks on the
  mount timeout inside a check that must stay within the five-second shutdown
  budget, and it would turn "the media server is off tonight" into a "needs
  attention". Bounded and log-only, or dropped. It belongs in E1 and 8.6, where a
  human is watching.

- Any file read is through the VFS, never plain `open()`. On tvOS `persist_one`
  may have dropped the POSIX copy, and a plain read returns "missing", producing
  exactly the false "needs attention" this section exists to avoid.
- **Silence on success.** Speak only if something did not stick. That rule ended
  the false "needs attention" era on Apple TV and is not negotiable.
- The check reads and reports. It never installs, never stages, never enables and
  never re-applies. If it finds a problem it tells the owner to run the command
  again.

## 8. Test plan

Cheapest first. "Fixed" means verified on the affected device class.

1. **Unit, on `load()` and `plan()`.** Bundle reading, overlay merge, the
   value-from-last / position-from-first rule (with an explicit test that an
   overlay override of `services.esenabled` does NOT move it after
   `services.esallinterfaces`), never-apply rejection, `default="true"` rejection,
   unknown-id rejection, unresolved-device-class rejection, and the class C merge:
   name dedupe, path dedupe, same-URL consolidation, and the two path rules. All
   pure functions of the bundle; no Kodi needed.
2. **The two-layer tvOS storage fake is `tests/fake_kodi_storage.py`**, with
   `platform="tvos"`, plus `test_fake_kodi_storage.py`. NOT
   `fake_kodi_sandbox_io.py`, which its own docstring says models a DIFFERENT bug
   family: the App Sandbox cross-layer read quirk for local and `special://temp`
   files, which are not under userdata. `fake_kodi_storage.py` is the one whose
   `state()` returns `key-only` / `disk-only` / `both` / `absent`, and "key
   exists, disk file gone" is precisely the state 4.1 warns about.
   **`ezmpp/CLAUDE.md:186-188` and `ezmpp/README.md:83-86` currently name the
   wrong fake and must be corrected in the same change.** Both fakes' docstrings
   are right; the two docs pointing at them are wrong.
3. **The lint extension** from 7.3, with its own failing-first test, landed BEFORE
   `profile.py`.
4. **Adversarial cases.** The house standard is explicit in
   `test_tvos_sandbox_io_contract.py:9-16`: a test that does not fail on the
   pre-fix code proves nothing. Two are mandatory: a per-item `persist_one` loop
   must FAIL against the storage fake (4.1), and a whole-document class C write
   must FAIL a test asserting pre-existing sources survive (4.3).
5. **The positive counterpart, which the adversarial cases do not provide.**
   Execute `apply()` against `fake_kodi_storage.py` at `platform="tvos"` and
   assert that all thirteen class A ids are present in the final artifact and that
   exactly ONE vector was taken. The adversarial case guards the FAKE; this guards
   the shipped code, and it is the difference between `_offenders()` and
   `test_the_known_good_pattern_is_present` in the chokepoint lint. The fake
   already stubs `getCondVisibility` for `System.Platform.TVOS`, so this needs no
   new plumbing.
6. **Projection differential on the bench.** NOT a profile diff. Two profiles are
   irreducibly different: `.setup_complete` is seeded only inside the sources
   block (`bin/reset-kodi:389-402`), so a `--no-sources` run lets macOS
   `preflight` write the three desktop defaults; `Addons33.db` is fabricated in
   one run and Kodi-authored in the other, with different rowids, `installDate`,
   `origin`, `update_rules` and `addonlinkrepo`; and `Textures13.db`, Thumbnails,
   packages, temp and logs all differ. Compare the PROJECTION instead, which is
   what 3.3 says the feature is:
   - class A id to value via `Settings.GetSettingValue`
   - the source name to path set, with `preflight` suppressed by pre-touching
     `.setup_complete` on the stock box
   - the enabled add-on set via `Addons.GetAddonDetails`
   - `addon_data` values for the listed ids only

   `bin/reset-kodi --verify` (`bin/reset-kodi:179-249,277-284`) already implements
   the source and add-on halves and exists to be reused rather than
   reimplemented. The exclusion set is finite, enumerated here, and asserted
   mechanically. A per-run judgement call about what to write down is a rubber
   stamp, not a gate.
7. **Hardware.** One Fire TV and one Apple TV, wiped to a genuine first run,
   before any release. The Apple TV run is where class A's vectoring and the
   `tvos/` overlay are actually proven.

One asymmetry to state before the next reader trips on it: 5.3 uses tvOS marker
non-durability as a kill on the deferred WRITE, while 7.7 depends on a marker.
That is defensible and not a contradiction: a lost marker in 7.7 means an unrun
read-only check, not an unapplied value the owner was promised. It is only
confusing when left unsaid.

Release gate, unchanged: `../bin/check-all ezmpp`.

## 9. Contract amendments needed in CLAUDE.md

Two lines in the backup and restore contract, not one:

1. "Boot NEVER installs, stages, or enables an add-on the box did not already have
   enabled, and restore never installs or stages add-ons."
2. "The ONLY sanctioned add-on toggle anywhere is the restore-scoped PVR pause."

Class D installs, stages, enables AND toggles. This command is a THIRD actor:
user-invoked, foreground, explicit confirm, validated bundle only. Both lines must
name it explicitly, or a future session reads them as absolute and either deletes
the feature or bends the rule quietly. Both have happened before.

Boot and restore stay exactly as restricted as they are now.

## 10. Phasing

| Phase | Deliverable | Gate to pass |
| ----- | ----------- | ------------ |
| 0 | Kodi source read, then E1, E2 and E3 on the bench; results recorded; class B and C outcomes decided | The `sources.xml` question is answered in writing with all four arms |
| 0.5 | The CLAUDE.md and README fake correction (8.2), as its OWN commit | Landed before phase 1 starts |
| 1 | Bundle at `resources/profiles/house/`, `tools/resolve_profile.py`, run-time resolution from `bin/reset-kodi` and `bin/seed-kodi`, the launcher adapter, the lint extension | The adapter reproduces `bootstrapper/settings/` BYTE-FOR-BYTE from the bundle, checked through `bin/seed-kodi --dry-run` |
| 2 | `profile.py` (load/plan/apply/verify), one menu row, three-state result record, in-flow verification | Projection differential is clean on the bench |
| 3 | Boot check, silent unless something failed | One Fire TV and one Apple TV, wiped to first run |
| 4 | "Save this box as a profile": capture the same key set to the backup folder | Optional. It is what makes this a feature rather than a fleet script |

Phases 1 through 3 are one release. Phase 4 is a later one.

Phase 1's gate exists because `profile.py` does not exist until phase 2, so the
projection differential cannot cross-check the adapter when the adapter ships,
and the tool being modified is the one that currently works in 18 seconds. The
byte-for-byte check is free: the bundle is authored FROM the current `settings/`
tree, so reproducing that tree is the definition of a correct adapter.

## 11. Open items

1. `settings/defaults.d/40-media.xml` in `bootstrapper` currently seeds
   `filelists.showparentdiritems` as `true`, which SHOWS the ".." entry, while the
   comment directly above it and `defaults.txt` both say parent items are off. Two
   documents agree against the value, so the recommendation is `false`; the bundle
   cannot be authored until the owner confirms, because the bundle becomes the
   source of truth for it.
2. Whether class B ships at all, pending E2. Default is no.
3. Whether class C ships in phase 2, is deferred, or leaves the add-on, pending
   E1's four arms.
4. What `addons.unknownsources` does under a live set, and whether enablement
   works while it is false, pending E3. The second answer may reorder 7.4.
5. The exact confirm and result wording, which is owner-gated vocabulary.
6. For an already-enabled third-party add-on, whether `addon_data` is written
   behind a bounded disable/re-enable (which needs a third contract amendment in
   §9) or reported as not applied (4.4). No payload depends on this today.
7. Whether `general.addonupdates` belongs in the bundle (4.4).

## 12. The checklist

Ordered. Nothing in a block starts until the block above it is done, because each
one gates the next. Owner decisions are marked; everything else is work.

### Before anything (blocks phase 1)

- [ ] OWNER: settle `filelists.showparentdiritems`. Recommendation is `false`
      (open item 1). The bundle cannot be authored without it.
- [ ] Land the test-fake correction in `CLAUDE.md` and `README.md` as its own
      commit. Phase 0.5, ahead of the feature, because CLAUDE.md is what the next
      session reads first and it currently points at the wrong fake.

### Phase 0: the experiments (blocks everything)

- [ ] Read `MediaSourceSettings.cpp` and `Application.cpp` for who calls
      `CMediaSourceSettings::Save()` and when. Source first, bench second.
- [ ] Check whether `CViewStateSettings` participates in `CSettings::Save`.
- [ ] E1, all four arms, from inside Kodi, ending at `Files.GetDirectory` (5.1).
- [ ] E2: does any live path to the settings level exist (5.2).
- [ ] E3: does a live set of `addons.unknownsources` prompt, hang or fail, AND
      does `Addons.SetAddonEnabled` work on a staged directory while it is false
      (5.4).
- [ ] Write the results to `docs/settings-profile-experiments-<date>.md`,
      whatever they say. E1 closes a contradiction between two repos and is worth
      recording on its own.
- [ ] DECIDE from the results: class C ships, defers or leaves the add-on; class B
      ships or stays dropped; whether 7.4 steps 3 and 4 swap.

### Phase 1: the bundle (no user interface)

- [ ] Extend `test_no_raw_userdata_writer._is_write_call` to see
      `ElementTree`/`tree.write`, with a failing-first test. Before `profile.py`.
- [ ] Author `resources/profiles/house/` from the current
      `bootstrapper/settings/` tree, including `overlays/bench/` deliberately.
- [ ] `tools/resolve_profile.py --device-class <c>`, emitting a flattened payload.
- [ ] Bundle-authoring gate in ezmpp CI: every setting id checked against a
      captured `Settings.GetSettings` catalog.
- [ ] `bootstrapper`: resolve and validate the bundle in the argument-parsing
      phase, above `bin/reset-kodi:286`, never after the wipe.
- [ ] The launcher adapter: entries to a whole `sources.xml`, add-on list to
      `installed` rows.
- [ ] GATE: the adapter reproduces `bootstrapper/settings/` byte-for-byte,
      checked through `bin/seed-kodi --dry-run`.
- [ ] Accept the consequences: the launcher's `npm run validate` and
      `npm run preview` both need repointing.

### Phase 2: the engine and the one row

- [ ] OWNER: the already-enabled `addon_data` question (open item 6) and
      `general.addonupdates` (open item 7).
- [ ] OWNER: the confirm and result wording.
- [ ] `profile.py`: `load` / `plan` / `apply` / `verify`, with the six-state
      result record.
- [ ] Unit tests on `load()` and `plan()`, including the overlay override that
      must not move `services.esenabled` after `services.esallinterfaces`.
- [ ] The two adversarial tests: a per-item `persist_one` loop must FAIL, a
      whole-document class C write must FAIL.
- [ ] The positive test: `apply()` against `fake_kodi_storage.py` at
      `platform="tvos"`, all class A ids present, exactly one vector taken.
- [ ] One menu row in `default.py`, one confirm, one restart.
- [ ] Amend both contract lines in `CLAUDE.md` (§9).
- [ ] GATE: projection differential clean on the bench.

### Phase 3: the boot check

- [ ] Stamped one-shot marker in `tools.py`, cleared regardless of outcome.
- [ ] Read-only check in `service.py`, abort-gated, VFS reads, silent on success.
- [ ] GATE: one Fire TV and one Apple TV, wiped to a genuine first run.

### Phase 4: later release

- [ ] "Save this box as a profile".

## Appendix A: the payload as it stands today

Class A, from `bootstrapper/settings/defaults.d/`:

| Setting id | Value |
| ---------- | ----- |
| `services.webserver` | true |
| `services.webserverport` | 8080 |
| `services.webserverauthentication` | true |
| `services.webserverusername` | kodi |
| `services.webserverpassword` | kodi |
| `services.esenabled` | true |
| `services.esallinterfaces` | true |
| `addons.unknownsources` | true (pending E3) |
| `addons.updatemode` | 1 |
| `epg.selectaction` | 1 |
| `filelists.showparentdiritems` | see open item 1 |
| `locale.audiolanguage` | English |
| `locale.subtitlelanguage` | none |

Class B: `general/settinglevel` of 3 with the `<viewstates />` stub. Dropped
unless E2 finds a live path.

Class C entries, from `bootstrapper/settings/sources.xml`:

| Name | Path |
| ---- | ---- |
| `.T7B` | `https://tony7bones.github.io/` |
| `KodiShare` | `nfs://@SHARE_HOST@/Users/moquette/Kodi/Share/` |
| `KodiBackup` | `nfs://@SHARE_HOST@/Users/moquette/Kodi/Backup/` |

The `@SHARE_HOST@` token is rendered from `resources/lib/modules/sharehost.py`
(the mini's tailnet address `100.121.59.123` since 2026-09-26; before that the
LAN address `192.168.7.2` was written literally).

Class D:

| Entry | Method |
| ----- | ------ |
| `repository.tony7bones` | stage, `UpdateLocalAddons`, enable LAST |
| `script.image.resource.select` | stage, `UpdateLocalAddons`, enable |
| `script.ezmaintenanceplusplus` `download.path` and `restore.path` | `setSetting()` on self, leaf from the device-class overlay |

## Appendix B: payload items that are deliberately unclassified

Named so the next reader does not re-derive them:

- `.setup_complete`, the macOS `preflight` marker. Bench-only.
- The NSUserDefaults window frame and `settings/windowed.d/`. Bench-only.
- The `origin` and `disabledReason` columns `lib/kodi-addons-db.sh` writes. Kodi
  populates them itself on a live enable.

## Appendix C: review record

Two review rounds, both by a QA specialist and a systems architect reading the
plan against both trees.

**Round 1 blocked revision 1.** Six blocking findings from QA, five from
architecture. Every blocking claim was verified against the tree before being
accepted, and all of them held.

**Round 2 approved revision 2 with changes.** QA: all 14 required changes
discharged, no new blocking finding, seven changes to fold in before phase 2 code
lands. Architecture: all 12 discharged, three of them landed stronger than asked
(failing-first adversarial tests rather than prose, `InstallAddon` dropped rather
than gated, the second copy removed rather than guarded), nine further changes
required before phase 1 starts. Revision 3 is those sixteen changes plus the
lesser items from both.

Round 1's blocking findings and where they landed:

| Finding | Where it landed |
| ------- | --------------- |
| Per-item `persist_one` silently no-ops every class A file write after the first on tvOS | 4.1, 7.4 step 4 |
| Class C as a whole document destroys existing sources; the correct merge already shipped in `boxsetup` | 4.3, 7.4 step 5 |
| Class B and C omitted `persist_one`, and the chokepoint lint cannot see a `tree.write` | 4.2, 4.3, 7.3 |
| The deferred boot-write mechanism cannot work: Kodi reads both files at startup | 5.3 |
| `profiles/` at repository root would never ship, per `tools/build.py` | 7.1 |
| The drift guard had no enforcement point: `bootstrapper` had no remote and no CI (it gained a remote 2026-08-06, still no CI) | 7.2 |
| `Files.GetSources` cannot verify class C in-flow and would emit a false PARTIAL | 7.6 |
| The lint covers every `.py` already; "add by name" had exactly one literal implementation and it was the wrong one | 7.3 |
| `fake_kodi_sandbox_io.py` is the wrong fake; CLAUDE.md and README name it wrongly | 8.2 |
| The bench differential compared two profiles that cannot converge | 8.5 |
| Class D `addon_data` written after enablement is restore defect A with a different owner | 4.4, 7.4 step 2 |
| Enabling the T7B repo mid-apply lets Kodi update this add-on while it runs | 4.4, 7.4 step 6 |
| `apply_guisettings` returns a count, which cannot feed an honest partial report | 7.5 |
| Precedence order and apply order collapse into one axis and break the `esenabled` dependency | 7.1 |
| Device-scoped leaves in the base silently send Apple TV backups to the Fire TV folder | 7.1 |
| The contract amendment named one line and needed two | 9 |
| The marker rides the backup onto other boxes, and is not durable across a tvOS power-off | 5.3, 7.7 |
| `InstallAddon` can prompt, is async, and cannot resolve before a repo index fetch | 4.4 |
| The step-5 ordering rationale was unfounded; the real rule is about class B | 7.4 |
| The root menu cannot carry a label2 detail the way the Backup rows do | 6.1 |

Round 2's findings and where they landed:

| Finding | Where it landed |
| ------- | --------------- |
| A live-catalog check inside `load()` destroys its purity and aborts the whole apply on one renamed id | 7.1, 7.3, 7.5 |
| Phase 1 shipped the launcher adapter with no cross-check until phase 2 | phase 1 gate, 10 |
| `bin/reset-kodi` would resolve the bundle AFTER wiping the bench | 7.2 |
| Overlay resolution would exist in three places and two languages | 7.2 |
| The `addon_data` comment-strip and `persist_one` rules were lost between revisions | 4.4, 7.1 |
| Writing `addon_data` "before enablement" is unachievable on a configured box | 4.4, open item 6 |
| Class C's `if added or renamed` idempotency guard was not carried across | 4.3, 7.5 |
| `Files.GetDirectory` in the boot check is a blocking NFS call on the abort-gated service thread | 7.7 |
| Excluding the marker from backups collides with "Full means full" | 7.7 |
| No positive `apply()` test against the storage fake, only adversarial ones | 8.5 |
| E3 anticipates a hang that the result vocabulary had no state for | 7.5 |
| E3 also answers whether enablement works while `unknownsources` is false | 5.4, 7.4 step 3 |
| Enabling the repo last bounds the update window rather than closing it | 4.4, open item 7 |
| Removing the launcher's copy breaks its own `validate` and `preview` scripts | 7.2 |
| The marker must be cleared regardless of outcome, or it nags | 7.7 |
| A failed marker write was not surfaced | 7.7 |
| "Settle" was undefined in a load-bearing sequence | 7.4 step 4 |
| The `service.py:107-119` citation did not support its sentence | 4.2 |
| 7.6 should consume `persist_one`'s return rather than re-implement the read-back | 7.6 |
| The fake correction should land as its own commit, ahead of the feature | phase 0.5 |
| The bench detection predicate was unstated | 7.1 |
