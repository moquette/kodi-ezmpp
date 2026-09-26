"""Make a tvOS BACKUP complete by capturing the userdata files that live ONLY in
NSUserDefaults (invisible to a POSIX walk).

Why this exists (the mirror image of nsud.py's restore bug):
On Apple TV (tvOS) Kodi stores userdata *.xml in the app's NSUserDefaults (gzip-compressed).
A key SHADOWS the on-disk file - Kodi reads the key first and NEVER copies a key back to
disk - so files like guisettings.xml, profiles.xml, RssFeeds.xml, peripheral_data/* and many
addon_data/<id>/settings.xml are NOT on disk at all. EZM's backup (CreateZip) enumerates
with `os.walk` and reads with plain `zipfile` (POSIX), so it SILENTLY OMITS every
NSUserDefaults-only file - the backup zip is missing exactly the settings the owner cares
about (weather, skin, region, remote/keyboard, RSS, IPTV). A full backup captures
EVERYTHING, including the addon_data/pvr.iptvsimple subtree (owner decision 2026-07-16,
reversing the 2026.07.08.5 backup-side IPTV exclusion); the restore-side instance-settings
sweep in wiz.py is what guarantees no duplicate IPTV instances. Proven on hardware: a tvOS
backup zip has no userdata/guisettings.xml; a Fire TV zip does. Without this, even a perfect
restore-side fix (nsud.py) cannot restore settings the backup never captured.

How it captures them - read Kodi's NSUserDefaults plist DIRECTLY, not through xbmcvfs.
An earlier design read each candidate back through `xbmcvfs.File`, but that FAILS on
hardware: for a key a currently-running add-on wrote, `xbmcvfs` reports the file exists yet
`readBytes()` returns 0 bytes (verified on atv-1: instance-settings-1.xml, sporthdme and
pvr.artwork all read empty via xbmcvfs while decoding fine from the plist - the same class as
docs/playbooks/kodi-vfs-cannot-read-foreign-local-files.md, a VFS read silently returning
empty for content a different writer produced). Kodi's NSUserDefaults store is just an
on-disk binary plist at `<sandbox>/Library/Preferences/<bundle-id>.plist`; reading it with
plistlib + gunzip yields EVERY `/userdata/*` key with full content and needs no enumeration
guesswork (no listdir - which on tvOS does not surface NSUserDefaults keys anyway - and no
per-add-on probing). Injected at the tail of CreateZip after the POSIX walk.

Two load-bearing safety properties:
- ADDITIVE + IDEMPOTENT. A path the POSIX walk already captured (its arcname is in
  `already_arcs`) is skipped. On Fire TV / desktop the sandbox has no such plist (the path
  `<home>/../../Preferences/*.plist` with `/userdata/*` keys only exists on tvOS), so the
  whole pass is a pure no-op - it cannot regress a platform that was already fine.
- The SECRET stays out. The add-on's own settings.xml (source box download/restore paths +
  dropbox_refresh_token) is excluded - by SUFFIX, so a per-profile copy
  (profiles/<name>/addon_data/.../settings.xml) is excluded too. It is never embedded in a
  backup via this route.

Fully guarded; never raises; never breaks a backup.
"""

import gzip
import os

import xbmcvfs
from resources.lib.modules import addon_id as _addon_id

ADDON_ID = _addon_id.get()

# userdata-relative tail (forward-slash) this capture must NEVER embed: the add-on's own
# settings.xml carries the SOURCE box's download/restore paths AND its dropbox_refresh_token
# (a secret). Matched by SUFFIX so a per-profile copy is excluded too.
_SECRET_TAIL = "addon_data/%s/settings.xml" % ADDON_ID

# FULL BACKUP POLICY (owner decision 2026-07-16): the pvr.iptvsimple addon_data subtree IS
# captured like any other userdata content, top-level AND per-profile. The 2026.07.08.5
# backup-side IPTV exclusion is REVERSED - duplicate-instance safety now lives on the
# RESTORE side (the instance-settings sweep in wiz.py), where it belongs. The only
# exclusion left in this capture is _SECRET_TAIL above.

_USERDATA_PREFIX = "/userdata/"


def _find_nsud_plist():
    """Locate Kodi's NSUserDefaults store: the on-disk binary plist at
    `<sandbox>/Library/Preferences/<bundle-id>.plist`. Resolve it WITHOUT hardcoding the
    bundle id (it differs across re-signed builds): from special://home (which is
    .../Library/Caches/Kodi on tvOS) walk up to Library/Preferences and pick the .plist that
    actually holds `/userdata/*` keys. Returns the (path, loaded_dict) or (None, None).

    This path shape only resolves to a real, `/userdata`-bearing plist on tvOS; on Fire
    TV/desktop the dir does not exist (or holds no such plist), so the caller no-ops.
    """
    try:
        import plistlib

        home = xbmcvfs.translatePath("special://home")
    except Exception:
        return (None, None)
    prefs = os.path.normpath(os.path.join(home, "..", "..", "Preferences"))
    try:
        names = [n for n in os.listdir(prefs) if n.endswith(".plist")]
    except OSError:
        return (None, None)
    # Probe a kodi-named plist first, but confirm by CONTENT (a /userdata/* key), never by
    # name alone - so an odd bundle id still resolves and a foreign plist never matches.
    names.sort(key=lambda n: ("kodi" not in n.lower(), n))
    for name in names:
        path = os.path.join(prefs, name)
        try:
            with open(path, "rb") as fh:
                data = plistlib.load(fh)
        except Exception:
            continue
        if any(isinstance(k, str) and k.startswith(_USERDATA_PREFIX) for k in data):
            return (path, data)
    return (None, None)


def _decode_value(v):
    """An NSUserDefaults plist value -> the real file bytes. Kodi gzip-compresses the value
    (small ones may be stored raw). Returns bytes, or None if empty/undecodable."""
    try:
        raw = bytes(v)
    except Exception:
        return None
    if not raw:
        return None
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        try:
            return gzip.decompress(raw)
        except Exception:
            return None
    return raw


def _arc_context(source_root):
    """From CreateZip's `folder` (special://home OR special://userdata, translated), return
    the arc_prefix so arcnames match CreateZip's own convention (relative to `folder`):
    full-home backup -> 'userdata/<rel>', userdata-only backup -> '<rel>'. Returns None if
    `folder` is neither (nothing to augment)."""
    root = os.path.normpath(source_root)
    if os.path.basename(root) == "userdata":
        return ""
    if os.path.isdir(os.path.join(root, "userdata")):
        return "userdata/"
    return None


def capture_nsud_userdata(zip_file, source_root, already_arcs, log=None):
    """After CreateZip's POSIX walk populated `zip_file`, add every userdata file that the
    walk missed because it lives only in NSUserDefaults (tvOS), by reading Kodi's
    NSUserDefaults plist directly and gunzipping each `/userdata/*` value into the open zip.
    Additive + idempotent (a path already in `already_arcs` is skipped); a pure no-op where
    no such plist exists (Fire TV / desktop). Returns (added, skipped, failed). Never raises.

    zip_file      : an open zipfile.ZipFile (mode 'w') that CreateZip is still filling.
    source_root   : CreateZip's `folder` (the abspath of special://home or .../userdata).
    already_arcs  : set of arcnames the POSIX walk already wrote (skip these).
    """
    added = skipped = failed = 0
    try:
        arc_prefix = _arc_context(source_root)
        if arc_prefix is None:
            return (0, 0, 0)  # not a home/userdata backup - nothing to augment
        _path, store = _find_nsud_plist()
        if not store:
            return (0, 0, 0)  # no NSUserDefaults store (non-tvOS) - nothing to augment
        have = set(already_arcs or ())
        for key in store:
            if not (isinstance(key, str) and key.startswith(_USERDATA_PREFIX)):
                continue  # e.g. the 'UserdataMigrated' bookkeeping key
            rel = key[len(_USERDATA_PREFIX) :].replace("\\", "/").lstrip("/")
            if not rel:
                continue
            if rel == _SECRET_TAIL or rel.endswith("/" + _SECRET_TAIL):
                skipped += 1  # never embed the add-on's own settings (secret)
                continue
            arc = arc_prefix + rel
            if arc in have:
                skipped += 1  # POSIX already captured this on-disk file
                continue
            data = _decode_value(store[key])
            if not data:
                failed += 1  # empty / undecodable value
                if log:
                    # Name the key: an anonymous count leaves the owner with an
                    # unbackupable box and no way to find the offending key.
                    log("nsub: capture FAILED (empty/undecodable value): %s" % key)
                continue
            try:
                zip_file.writestr(arc, data)
                have.add(arc)
                added += 1
            except Exception as e:
                failed += 1
                if log:
                    log(
                        "nsub: capture FAILED (%s writing %s)" % (type(e).__name__, arc)
                    )
        if log:
            log(
                "nsub: NSUserDefaults capture (plist): %d added, %d skipped, %d failed"
                % (added, skipped, failed)
            )
    except Exception:
        pass
    return (added, skipped, failed)
