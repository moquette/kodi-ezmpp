# Rolling back EZ Maintenance++

Read this BEFORE you publish anything, not after. It is written from measured
facts about the live fleet, and the headline is uncomfortable:

**A box cannot downgrade from the repo. The setting toggle is the only fast
rollback. Everything else is a multi-step operation that undoes itself if you
stop halfway.**

## The one-line summary

| Situation | What you actually do |
| - | - |
| Home > Movies redirect misbehaves | Toggle the setting OFF. Done, instantly, per box. |
| The whole release is bad | Toggle OFF everywhere, then git revert AND republish. |
| A box must run the old build | Install the preserved zip AND republish the old version, or it re-upgrades. |

## Why "just reinstall the old zip" does not work

Three measured facts, each verifiable in under a minute:

1. **The repo only ever serves one version.**
   `repo/_tools/generate_repo.py` lines 150-156 delete superseded zips on every
   generate:

   ```
   # Prune superseded versions so old zips never accumulate in the tree
   # (only the current version is committed; /static/ serves the fleet).
   ```

   So there is no older version in the catalog for a box to pick, and Kodi's
   per-add-on version picker in the add-on info dialog has nothing to offer.

2. **The fleet auto-installs updates.** The office Fire TV reports
   `general.addonupdates` = `0`, and Kodi's own
   `system/settings/settings.xml` documents that value as `AUTO_UPDATES_ON`
   (`1` is NOTIFY, `2` is NEVER). Verify any box with:

   ```sh
   curl -s -u kodi:kodi -X POST -H 'Content-Type: application/json' \
     -d '{"jsonrpc":"2.0","method":"Settings.GetSettingValue","params":{"setting":"general.addonupdates"},"id":1}' \
     http://192.168.7.162:8080/jsonrpc
   ```

   Do not confuse this with `addons.updatemode`, which is the unknown-sources
   policy (`0` OFFICIAL_ONLY, `1` ANY_REPOSITORY) and has nothing to do with
   updating.

3. **Therefore a zip downgrade silently re-upgrades itself** on the next repo
   check. You get a rollback that looks like it worked and quietly reverts,
   which is worse than no rollback at all.

Conclusion: **any zip rollback MUST be immediately followed by a git revert and
a republish of BOTH ezmpp and the hosted mirror.** If you are not going to do
the republish, do not bother with the zip; toggle the setting off instead.

## Path 1: the toggle (primary, and the only fast one)

The Home > Movies redirect ships behind an EZ Maintenance++ setting that
DEFAULTS TO OFF. A box that never opted in is already unaffected.

GUI: Add-ons > My add-ons > Program add-ons > EZ Maintenance++ > Configure, and
turn the Home Movies redirect off. It takes effect without a restart, because
the service reacts to `onSettingsChanged`.

Read the current value off a box without touching the GUI:

```sh
adb -s 192.168.7.162:5555 shell "grep -n 'home_movies_pov' \
  /storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/userdata/addon_data/script.ezmaintenanceplusplus/settings.xml"
```

Confirm the id against `script.ezmaintenanceplusplus/resources/settings.xml`
before trusting a grep that returns nothing; an id that was renamed reads
exactly like a setting that is off.

## Path 2: reinstall the preserved build on one box

The pre-change build is preserved on the mini, off this Mac, because
`ezmpp/dist/` is gitignored (`.gitignore:5`) and for a while this Mac held the
only copy in existence:

```
mini:~/Kodi/Share/rollback/script.ezmaintenanceplusplus-2026.07.31.1.zip
sha256 17b59cedf546c2f0ca055e63debcd13372108532452ed93c4b9f856fac1eb2c5
```

Verify it before relying on it:

```sh
ssh mini 'shasum -a 256 ~/Kodi/Share/rollback/script.ezmaintenanceplusplus-2026.07.31.1.zip'
```

Every box already carries the NFS source `nfs://192.168.7.2/Users/moquette/Kodi/`
named "Kodi" in its `sources.xml`, so the zip is reachable with no new source
and no adb push, at:

```
nfs://192.168.7.2/Users/moquette/Kodi/Share/rollback/
```

On the box: Add-ons > Install from zip file > Kodi > Share > rollback > the zip.

**Then immediately do Path 3, or the box re-upgrades itself.**

## Path 3: unpublish, which is the only durable rollback

Two separate git repos. Both must move, in this order.

```sh
# 1. ezmpp: revert the change and cut the previous version again
cd /Users/moquette/Code/kodi/ezmpp
git revert --no-edit <commit>            # or: git revert --no-edit <first>..<last>
/opt/homebrew/bin/python3 -m pytest tests/ -q     # must be green before anything else
./build.sh --check                                # deterministic zip, builds twice
tools/release.sh                                  # refuses to tag unless CI is completed/success

# 2. repo: bump the hosted mirror, regenerate, republish
cd /Users/moquette/Code/kodi/repo
# edit addons/hosted/script.ezmaintenanceplusplus/addon.xml:
#   the version="" attribute AND the <news> line, together
python3 _tools/generate_repo.py                   # commit the generated output
python3 -m pytest _tools/ -q
python3 _tools/release.py --dry-run
python3 _tools/release.py

# 3. prove the fleet can actually see it
bin/check-all
```

Step 2 is the one that gets forgotten. **Releasing is not publishing.** Tagging
a release puts it on no box; `/static/` is what the boxes read, and
`skin.estuary7` 1.0.71 was released, CI-green and unreachable by every box for
15 hours because the hosted mirror was never bumped. `repo/_tools/check_hosted_release_sync.py`
and `ezmpp/tools/check_unreleased_changes.py` exist to catch exactly this, and
`bin/check-all` surfaces both. An UNRELEASED CHANGES notice is information, not
a failure, but after a rollback it usually means you stopped one step early.

Note also that `repo/addons/hosted/script.ezmaintenanceplusplus/` contains no
zip at all, only `addon.xml`, `icon.png` and `fanart.jpg`. The zip the fleet
installs comes from the GitHub release via `/static/`, so verifying the mirror's
`addon.xml` is not the same as verifying a box can download the bytes.

## Verifying a rollback actually landed

```sh
# what the box is running now
curl -s -u kodi:kodi -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"Addons.GetAddonDetails","params":{"addonid":"script.ezmaintenanceplusplus","properties":["version","enabled"]},"id":1}' \
  http://192.168.7.162:8080/jsonrpc

# and that Home > Movies is back to stock behaviour
curl -s -u kodi:kodi -X POST -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","method":"XBMC.GetInfoLabels","params":{"labels":["Container.FolderPath"]},"id":1}' \
  http://192.168.7.162:8080/jsonrpc
```

Stock behaviour for a box with an empty movie library is
`ActivateWindow(Videos,sources://video/,return)`, because stock Estuary's
`Home.xml` fires that branch when `Library.HasContent(movies)` is false. If the
library is populated the stock target is `videodb://movies/titles/` instead.

Check the log with the LOWERCASE pattern. Kodi 22 writes levels in lower case,
so `grep ERROR` returns zero and reads falsely clean:

```sh
adb -s 192.168.7.162:5555 shell \
  "cat /storage/emulated/0/Android/data/org.xbmc.kodi/files/.kodi/temp/kodi.log" \
  | grep -acE " (error|fatal) <"
```
