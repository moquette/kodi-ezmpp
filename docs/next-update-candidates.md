# EZ Maintenance++ - candidates for the next update

Forward-looking queue of investigated-but-unshipped work. Opened 2026-07-18
because no such document existed: `ezmpp/docs/` held only completed plans and a
defect writeup, and `repo/docs/OPTIMIZATION-BACKLOG.md` covers the hub tooling,
not this add-on.

Every item below states what was PROVEN, what was INFERRED, and what the
recommendation actually is. Several investigations concluded "change nothing" -
those are recorded deliberately so the question is not re-opened from scratch.

Nothing here is approved. The owner requires independent QA agent and
architecture agent review before any phase is declared done.

---

## 1. Video cache `readfactor` - INVESTIGATED, RECOMMENDATION: DO NOT RAISE

**Question asked (2026-07-18):** would the fleet benefit from raising
`filecache.readfactor` above its current value, or from EZM++ writing a
`readfactor` into `advancedsettings.xml`?

**Answer: no on both counts.** Three independent agent investigations (Kodi
source internals, fleet workload characterization, external Omega-era evidence)
converged.

### What EZM++ does today

`tools.py:37-41` - EZM++ tunes exactly one cache setting, `filecache.memorysize`,
over JSON-RPC. `readfactor`, `buffermode`, and `chunksize` are never touched.
`tools.py:139-149` (`_clean_stale_advancedsettings`) DELETES any
`advancedsettings.xml` containing a `<cache>` block.

**That deletion is correct behavior and must not be "fixed."** In Kodi 21 Omega
the `advancedsettings.xml` `<cache>` section no longer exists: grepping
`AdvancedSettings.cpp` for `cacheReadFactor|cachemembuffersize` returns nothing.
The settings moved to the GUI in commit `8d58560b` (2023-11-01, "[FileSystem]
Move File Cache settings to GUI settings"). Any `advancedsettings.xml` cache
block deployed on a box is silently inert on 21.3.

### Fleet's actual values (read from base backup images, 2026-07-18)

| Setting | Apple TV | Fire TV | Kodi default |
| --- | --- | --- | --- |
| `filecache.buffermode` | 4 (`default="true"`) | 4 (`default="true"`) | 4 |
| `filecache.memorysize` | 200 | 166 | 20 |
| `filecache.readfactor` | 400 (`default="true"`) | 400 (`default="true"`) | 400 |
| `filecache.chunksize` | 131072 (`default="true"`) | 131072 (`default="true"`) | 131072 |

`memorysize` is the only value ever tuned. Kodi's own `default="true"` marker
confirms `readfactor` has never been modified on any box.

### Why raising it would not help (PROVEN from source)

- **Units.** `FileCache.cpp:242` - `GetInt(SETTING_FILECACHE_READFACTOR) / 100.0f`.
  So 400 = 4.0x. Confirmed independently by the option filler
  (`ServicesSettings.cpp:84`, `list.emplace_back("4x", 400)`).
- **It is a throttle, not a booster.** The fill loop (`FileCache.cpp:304-321`)
  uses the value twice: as an unthrottled head-start threshold in bytes, and as a
  sustained rate CAP in bytes/sec. It never causes Kodi to read more than the
  source delivers.
- **It is not in the stall path.** The re-buffer state is entered when the A/V
  queues drain (`VideoPlayer.cpp:1932-1965`); the low-rate report fires only when
  the source under-delivers versus the stream's own bitrate
  (`VideoPlayer.cpp:1853-1856`, `FileCache.cpp:433-441`). That is a throughput
  deficit a larger multiplier cannot fix.
- **No memory cost, real CPU/network cost.** Allocation comes solely from
  `memorysize` (`FileCache.cpp:118-189`, `CircularCache.cpp:21-54`); readfactor
  appears in no allocation. Raising it widens the unthrottled window, costing
  syscalls, TCP/NFS RPC, TLS decrypt and memcpy on the same SoC as the decoder.
  String #37108 names this outright: "Large multiples may cause CPU spikes on
  some devices, saturate the connection and worsen performance."

### Why it is moot for this fleet's workload (OBSERVED)

- **842 of the fleet's channels are live MPEG-TS over plain HTTP** (Streamvision
  492, Network 24 350). A cache cannot fill ahead of content that does not exist
  yet; the origin sets the rate.
- **POV debrid VOD is the only genuine fill-ahead lane**, and the 4x ceiling
  (~200-320 Mbps on a 4K remux) already exceeds what the boxes' Wi-Fi or WAN
  delivers. The transport binds first.
- **No local media library exists.** `sources.xml` has empty video/music/pictures
  defaults; the only sources are the repo URL and two NFS mounts carrying zips
  and backups, not video.
- **Travel sticks are not a counter-example.** Only lightweight IPTV artifacts
  cross the tailnet; video goes device to provider directly
  (`docs/static-repo-and-tailscale.md:265-268`).
- **No recorded playback complaint exists.** A repo-wide grep for
  stutter/rebuffer/stall/freeze/choppy/dropped-frames returns zero hits, and
  `readfactor|filecache|buffermode` returns nothing repo-wide.

### The only defensible change, if any: 400 -> 0 (Adaptive)

Adaptive is enum value `0` (`ServicesSettings.cpp:76`), selected by
`readFactor < 1.0f` (`FileCache.cpp:244`). Its formula
(`FileCache.cpp:297-302`) is `level * -2.5 + 4.0`: **4.0x when the buffer is
empty, tapering to 1.5x when full.**

So Adaptive is exactly as aggressive as the current fixed 400 at the only moment
fill speed matters (refilling a drained buffer) and strictly gentler otherwise.
Across 7+ boxes sharing one AP and one NFS server that reduces contention at no
cost to stall recovery. It is new in Omega (commit `37b3a904`, 2024-02-03), so no
pre-21 advice references it.

**Recommendation: leave `readfactor` alone.** If anyone insists on a change,
Adaptive is the only direction with a defensible argument, and it should be
A/B tested with the rest of the fleet BOTH idle and streaming concurrently. A
single-box test cannot see the shared-AP/NFS cost that is the actual risk.

### Two Omega semantics worth knowing before touching any cache setting

- **`readfactor` is latched once per playback.** Omega reads it at cache-thread
  start (`FileCache.cpp:242`, outside the loop); Kodi 19/20 re-read it every
  iteration. Changing it mid-playback has no effect; it applies from the next
  file opened.
- **The GUI `chunksize` is silently ignored when the source reports its own**
  (`File.cpp:436-441`). On NFS sources the fleet's 131072 is likely dead.

### CLOSED 2026-08-30: no stakes left - readfactor is settled do-not-raise and v2026.07.31.1 resets the buffer to Kodi's default on restore/Fresh Start, so no tuning decision depends on the answer. (Originally: open item, NOT verified - does the IPTV path even use CFileCache?)

`inputstream.adaptive` and PVR add-ons are believed to do their own HTTP fetching
and never touch `CFileCache`, which would make every cache setting irrelevant to
live IPTV regardless of value. **This was inferred, not proven** (those live in
separate add-on repos and were not read). Testable read-only: look for
`CFileCache::Open - <path> using single memory cache sized N bytes`
(`FileCache.cpp:180`) in `kodi.log` while playing an IPTV channel. Absent means
readfactor is irrelevant for that path, full stop.

Related live-stream trap, PROVEN: if `GetLength()` or `GetStreamLength()` returns
<= 0 (unbounded live streams), `SetReadRate` is never called
(`VideoPlayer.cpp:859`) and `m_writeRate` stays at its `Open()` initializer of
1 MiB/s (`FileCache.cpp:208`). readfactor then multiplies a fixed 8 Mbit/s
fiction rather than the real bitrate.

---

## 2. CLOSED 2026-08-30: shipped in v2026.07.19.3 (76a0dd4) - `_recommended_mb()` snaps to a size Kodi's GUI actually offers (`_snap_to_kodi_size`, tools.py). (Originally: `memorysize` is set to values Kodi does not offer - VERIFY BEFORE CHANGING)

The fleet runs `memorysize` 200 (Apple TV) and 166 (Fire TV). **Neither is a
selectable value.** Kodi's list (`ServicesSettings.cpp:45-70`) is 16, 20, 24, 32,
48, 64, 96, 128, 192, 256, 384, 512, 768, 1024, plus 0.

**The values ARE honored - EZM++'s retune works.** Verified in
`Setting.cpp`, `CSettingInt::CheckValidity`: validation runs only against STATIC
option lists (`m_translatableOptions` / `m_options`). When a dynamic options
filler is present the code explicitly skips validation
(`else if (m_optionsFillerName.empty() && m_optionsFiller == nullptr && ...)`).
`filecache.memorysize` uses the `filecachememorysizes` filler, so arbitrary
integers pass and `FileCache.cpp:118-189` reads the int directly.

**But there is a real UX hazard.** Because 200 is not in the GUI list, opening
that setting in Kodi's own Settings screen may snap it to a neighbouring listed
value. Anyone who merely "looks at" the buffer setting could silently retune the
box.

**Candidate for next update:** decide whether `_recommended_mb()` should snap to
the nearest listed value (192 instead of 200, 128 or 192 instead of 166) so the
GUI and the stored value agree. This is a small, low-risk change with a real
robustness payoff. It has NOT been agreed.

**Also note:** `memorysize` carries the fleet's real memory risk, not readfactor.
The buffer is allocated up front, resident, per open file
(`CircularCache.cpp:38-54`), and split front/back 3:1 (`FileCache.cpp:118-189`).
200 MB on a box with 1 to 1.5 GB total is a substantial commitment. Per-box RAM
figures for the five fixed Fire TVs and the Apple TV model generations are NOT
recorded anywhere in this project and should be, before anyone tunes further.

---

## 3. Credentials in cleartext inside backup zips - OWNER DECISION REQUIRED

**Not an EZM++ coding error. A consequence of the stated backup contract.**

Both current base backup images contain, in
`userdata/addon_data/plugin.video.pov/settings.xml`, live-looking secrets in
cleartext: Real-Debrid `rd.token` / `rd.refresh` / `rd.secret`, an Easynews
username and password, Trakt tokens (expiry 1784795842, future-dated), an mdblist
token, and a TMDB bearer JWT. The tvOS and fireos images carry DIFFERENT
Real-Debrid and Trakt pairs.

Those zips live under `~/Kodi/Backup/` on the mini, exported over NFS as
`KodiBackup` to the LAN, and `~/Kodi/Share` additionally carries a read-only
tailnet export to `100.64.0.0/10`.

Each decision is individually defensible: "full means full" is the stated backup
contract (`ezmpp/CLAUDE.md`), POV stores its own tokens in cleartext, and the
share exists so boxes can restore. Together they publish live credentials to
every box on the LAN.

**Status: UNRESOLVED, raised with the owner 2026-07-18, no action taken.** The
export scope of the Backup path specifically was NOT verified. Per the vault
write-trigger rule this warrants reconciliation into VAULT.md once the owner
decides. Options not yet weighed: excluding POV settings from backup (breaks
"full means full"), tightening the export, or accepting the risk explicitly.

---

## 4. CLOSED 2026-08-30: both shipped - the skin-settings clobber was fixed in v2026.07.19.3 (be31322 re-applies restored skin settings in memory; only A3, making the restored skin live, stays open on an owner decision) and the post-restore prompt was fixed by DELETION in v2026.07.19.4 (the prompts no longer exist). (Originally: Two OPEN restore defects)

Not restated here. See `restore-defects-2026-07-18.md` for the full record: the
skin-settings clobber (root cause empirically confirmed, fleet-wide, not
tvOS-only) and the post-restore prompt that discards typed input (mechanism
proven, trigger unidentified). That document carries the proposed fix plan and
the task breakdown.

---

## 5. Deferred from an earlier plan

`ui-consistency-plan.md` Stage D - removal of `buildInstaller` / `BUILDS` /
`install_build` - was explicitly deferred to a separate PR (that document, lines
4-5, 18, 242). Stages A, B and C are built. This is the only pre-existing
deferred EZM++ item found in the tree.

---

## References

### Project doctrine (governs anything in this queue)

> **Sibling-repo paths.** Everything prefixed `repo/` below lives in
> `tony7bones/tony7bones.github.io` (local checkout `~/Code/moquette/kodi/repo`),
> a DIFFERENT git repo. A standalone clone of this add-on cannot reach them; the
> critical parts are inlined in this repo's `TASKS.md`.

| Document | Why it matters |
| --- | --- |
| `repo/docs/playbooks/kodi-settings-clobber.md` | Names the settings-clobber bug class; instance 1 is the skin settings file. Mechanism A/B + decision guide. |
| `repo/docs/plans/atv-every-boot-settings-reassert.md` | REJECTED every-boot re-assert design (do not re-propose) plus the corrected fix that became `nsud.rewrite_userdata_xml`, with its open caveats. |
| `repo/docs/playbooks/ezm-restore-hardening.md` | The 2026.07.07.x restore hardening and the Fire OS 8 progress-text SIGSEGV lesson. |
| `repo/docs/playbooks/kodi-vfs-cannot-read-foreign-local-files.md` | Why stash/marker I/O uses plain Python, not `xbmcvfs`. |
| `~/Code/moquette/kodi/.claude/skills/kodi-storage-map/SKILL.md` | Exhaustive per-OS file map. |
| `~/Code/moquette/kodi/.claude/skills/ezm-backup-doctor/SKILL.md` | Backup/restore triage guide. |
| `repo/docs/agent-postmortem-do-not-repeat.md` | Process failures not to repeat. |
| `ezmpp/CLAUDE.md` | Backup/restore contract, tvOS storage rules, the three mechanical guards. |
| `ezmpp/docs/restore-defects-2026-07-18.md` | The two open defects, root cause, fix plan, task breakdown. |

Relevant incident record in `repo/docs/`: `incident-2026-07-07-ezmpp-wrong-device-buffer-after-restore.md`
(why the post-restore buffer prompt exists at all, which is the feature item 2 touches),
`incident-2026-07-08-ezmpp-atv-settings-nsuserdefaults.md`,
`incident-2026-07-16-ezmpp-full-backup-was-not-full.md` (origin of "full means full",
which is what puts the credentials in item 3 inside the archive).

### Kodi Omega source (github.com/xbmc/xbmc, branch `Omega`)

| Claim | Citation |
| --- | --- |
| readfactor units (value / 100.0f), latched once per playback | `xbmc/filesystem/FileCache.cpp:242` |
| Adaptive selected by `readFactor < 1.0f` | `xbmc/filesystem/FileCache.cpp:244` |
| Adaptive formula `level * -2.5 + 4.0`, bounds 4.0x to 1.5x | `xbmc/filesystem/FileCache.cpp:297-302` |
| Throttle loop: head-start threshold + sustained rate cap | `xbmc/filesystem/FileCache.cpp:304-321` |
| Cache size derived solely from `memorysize`; front/back split | `xbmc/filesystem/FileCache.cpp:118-189` |
| `m_writeRate` 1 MiB/s initializer (live-stream trap) | `xbmc/filesystem/FileCache.cpp:208` |
| `lowrate` set only when source under-delivers | `xbmc/filesystem/FileCache.cpp:433-441` |
| `CFileCache::Open` log line (is the cache engaged) | `xbmc/filesystem/FileCache.cpp:180` |
| Single up-front allocation, no readfactor term | `xbmc/filesystem/CircularCache.cpp:21-54`, `:38-54` |
| readfactor option list, `"4x" = 400`, Adaptive = 0 | `xbmc/settings/ServicesSettings.cpp:71-92`, `:84`, `:76` |
| memorysize option list (16..1024 plus 0) | `xbmc/settings/ServicesSettings.cpp:45-70` |
| buffermode option list / enum | `xbmc/settings/ServicesSettings.cpp:34-43`, `xbmc/filesystem/IFileTypes.h:61-68` |
| No validation when a dynamic options filler is present | `xbmc/settings/lib/Setting.cpp`, `CSettingInt::CheckValidity` |
| buffermode gate on whether FileCache is used at all | `xbmc/filesystem/File.cpp:284-318` |
| GUI chunksize ignored when source reports its own | `xbmc/filesystem/File.cpp:436-441` |
| `SetReadRate` = filesize/duration, skipped when length <= 0 | `xbmc/cores/VideoPlayer/VideoPlayer.cpp:857-860`, `:859` |
| 1.1x inflation of measured rate | `xbmc/cores/VideoPlayer/DVDInputStreams/DVDInputStreamFile.cpp:158-168` |
| Stall / re-buffer decision path | `xbmc/cores/VideoPlayer/VideoPlayer.cpp:1810-1862`, `:1932-1965`, `:1853-1856` |
| Setting definitions and defaults (buffermode 4, memorysize 20, readfactor 400, chunksize 131072) | `system/settings/settings.xml:2516-2566` |
| Setting labels/help (#37105-37108), Adaptive label (#37116) | `addons/resource.language.en_gb/resources/strings.po` |
| Skin settings flushed on clean shutdown (defect 4 context) | `xbmc/application/Application.cpp:2130-2131`, `:2139-2141` |

### Kodi commits

| Change | Commit |
| --- | --- |
| File cache settings moved from advancedsettings.xml to GUI | `8d58560b` (2023-11-01) |
| Adaptive read factor based on cache level | `37b3a904` (2024-02-03) |
| Read Factor algorithm adjustment | `bad29939` (2024-01-20) |

Verified absent in Omega: `cacheReadFactor`, `readfactor`, `cachechunksize`,
`cachemembuffersize` in `xbmc/settings/AdvancedSettings.cpp`.

### This project

| Claim | Citation |
| --- | --- |
| EZM++ tunes only `filecache.memorysize`, via JSON-RPC | `script.ezmaintenanceplusplus/resources/lib/modules/tools.py:37-41`, `:62-76` |
| EZM++ deletes advancedsettings.xml with a `<cache>` block | `script.ezmaintenanceplusplus/resources/lib/modules/tools.py:139-149`, invoked `:188` |
| Buffer menu, 400 MB warning threshold | `script.ezmaintenanceplusplus/resources/lib/modules/tools.py:152-193`, `:179-185` |
| Backup contract ("full means full", PVR pause, two-layer tvOS capture) | `ezmpp/CLAUDE.md` |
| Only IPTV artifacts cross the tailnet; video goes direct to provider | `docs/static-repo-and-tailscale.md:265-268` |
| Home upload would bind only under exit-node streaming (unmeasured) | `docs/static-repo-and-tailscale.md:107-108` |
| Travel sticks: Fire TV Stick 4K Max, AFTKRT, Fire OS on Android 11 | `docs/static-repo-and-tailscale.md:308-332` |
| `max_connections=1` named the dominant IPTV failure mode | `iptv/docs/iptv-stream-troubleshooting.md:37`, `:271` |
| Automatic mirror failover listed as an unimplemented improvement | `iptv/docs/iptv-stream-troubleshooting.md:291` |
| Stage D deferral | `ezmpp/docs/ui-consistency-plan.md:4-5`, `:18`, `:242` |
| Two open restore defects, root cause and fix plan | `ezmpp/docs/restore-defects-2026-07-18.md` |

### Fleet data read (2026-07-18)

Backup images on the mini, `~/Kodi/Backup/`, extracted read-only to `/tmp` and
removed afterwards. `~/Kodi` was not modified.

- `tvos/base_202607180333.zip` - `userdata/guisettings.xml`, `sources.xml`,
  `addon_data/{pvr.iptvsimple,plugin.video.pov,plugin.video.the-loop}`
- `fireos/base_202607180132.zip` - same fields
- `tvos/base/base_202607171629.zip`, `fireos/base/base_202607171626.zip` -
  `guisettings.xml` only
- Playlists on the mini: `~/Kodi/Share/iptv/{Streamvision,Network24}.m3u`
  (492 and 350 channels, all raw `.ts` over plain HTTP)

Local reference copy of Kodi 21.3 used for setting definitions and option lists:
`/Applications/Kodi.app/Contents/Resources/Kodi/system/settings/settings.xml`
and the bundled `resource.language.en_gb` `strings.po`. Reported build:
`21.3 (21.3.0) Git:20251031-a3a448d26b`.
