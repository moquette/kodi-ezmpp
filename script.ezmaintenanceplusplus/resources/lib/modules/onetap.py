# -*- coding: utf-8 -*-
"""
Shared wipe engine for EZ Maintenance++.

This module owns the destructive wipe (`_wipe`) and its two-layer tvOS contract, plus
the "keep across a wipe" helpers (`keep_addon_db`, `keep_source_files`,
`repository_addon_names`) and the `infer_type` backup-name hint. Its consumers are Fresh
Start (`default.py` FRESHSTART), the normal Backup/Restore flow (`wiz.py`), and the
post-restore verification (`restorecheck.py`).

This file was formerly "One-Tap Restore" (pin a backup, restore it in one tap). That
user-facing feature was removed in v2026.07.21.x; only its wipe engine remains. The
module keeps the name `onetap` deliberately so the storage-contract fingerprint stays
stable and the tvOS two-layer-wipe references (`onetap._wipe_nsud_keys`) that the docs
and tests name do not drift.

Imports only xbmc / xbmcaddon / xbmcvfs, so the engine is fully unit-testable off-device.
"""

import xbmc
import xbmcaddon
import xbmcvfs
from resources.lib.modules import addon_id as _addon_id


def _log(msg):
    try:
        xbmc.log("EZMpp OneTap: %s" % msg, xbmc.LOGINFO)
    except Exception:
        pass


def infer_type(filename):
    """Best-effort backup type from the file name (informational)."""
    n = (filename or "").lower()
    # The default userdata backup name is "kodi_settings" (wiz.backup), which has neither
    # "userdata" nor "full"/"backup" - map settings-style names to userdata so the restore
    # anchor hint is right. (Non-load-bearing: the zip's own layout is authoritative.)
    if "userdata" in n or "kodi_settings" in n or "settings" in n:
        return "userdata"
    if "full" in n or "backup" in n:
        return "full"
    return "unknown"


# --------------------------------------------------------------------------- #
# The wipe: remove everything under special://home except the add-on, its runtime
# deps, temp/, and any caller-specified keep set. Two layers on tvOS (POSIX files +
# NSUserDefaults keys); a strict POSIX-only pass everywhere else.
# --------------------------------------------------------------------------- #
_ADDON_ID = _addon_id.get()


def _wipe_excludes():
    keep = {
        "temp",  # the staged, already-validated snapshot lives here - must survive
        "backupdir",
        "backup.zip",
        "script.module.requests",
        "script.module.urllib3",
        "script.module.chardet",
        "script.module.idna",
        "script.module.certifi",
    }
    try:
        keep.add(xbmcaddon.Addon().getAddonInfo("id") or _ADDON_ID)
    except Exception:
        keep.add(_ADDON_ID)
    return keep


# Kodi holds a persistent CDatabase connection on every userdata/Database/*.db for as
# long as it runs. Unlinking one leaves Kodi writing to an unlinked inode: the next
# write fails SQLITE_READONLY_DBMOVED, every write after that fails SQLITE_MISUSE, and
# on Android the storm aborts the process. A SINGLE unlinked Textures13.db is what
# killed the office Fire TV on 2026-07-21 (see maintenance._purge_texture_cache).
#
# So the rule is NOT "never delete a database" - it is: delete them only if this
# process will not survive the deletion.
#
#   Fresh Start   deletes them, then ALWAYS hard-exits (ui.terminate, os._exit). The
#                 slate has to be genuinely clean, so the databases must go; safety
#                 comes from the process not outliving them. There is deliberately no
#                 "do it later" path - see default.py FRESHSTART.
#   restore       CANNOT exit: it keeps Kodi alive for the whole zip extract. It must
#                 therefore PRESERVE the databases, which costs nothing because the
#                 archive re-supplies them. wiz.py _wipe_pass adds DB_DIR_NAME.
def keep_live_databases():
    """Absolute paths of everything in userdata/Database, for callers that keep Kodi UP.

    A KEEP-LIST, deliberately, not a name added to the exclude set. _wipe prunes
    excluded directory NAMES at ANY depth, so excluding "Database" would also preserve
    `addon_data/<any add-on>/Database/` from the OLD install - files the archive does
    not carry, surviving a clean-clone restore that exists precisely to make the box
    equal the archive. This anchors to the one real directory instead.

    The glob is deliberately `*`, not `*.db`: a `-journal`/`-wal` sidecar must stay with
    its database. Preserving the .db while deleting its hot journal leaves SQLite to
    roll back - or to find a stale journal beside a freshly restored database and roll
    THAT back, silently corrupting restored state.
    """
    import glob
    import os

    try:
        db_dir = xbmcvfs.translatePath("special://home/userdata/Database")
        return set(glob.glob(os.path.join(db_dir, "*")))
    except Exception:
        return set()


def _wipe(home, excludes, keep_files=None, progress=None):
    """Remove everything under `home` except any entry named in `excludes` (matched at any
    depth - protects addons/<this add-on> and temp/) and any absolute path in `keep_files`
    (e.g. Kodi's add-on state DB, so the surviving add-on stays ENABLED).

    TWO-LAYER CONTRACT: the POSIX pass above is followed by _wipe_nsud_keys, which on
    tvOS (and ONLY tvOS - hard-gated by _is_tvos) also drops the NSUserDefaults key of
    every non-excluded userdata path. On Apple TV Kodi vectors userdata *.xml into
    NSUserDefaults and reads the KEY FIRST - a key SHADOWS the disk file and Kodi never
    copies a key back to disk (apple-tv SKILL.md section 8, TVOSFile.cpp:113-122) - so a
    POSIX-only wipe leaves every key alive to shadow whatever the subsequent restore
    writes. On Fire TV / Android / desktop the key pass is a strict no-op.

    Returns (files_removed, keys_removed, failed, leftovers): failed counts BOTH file
    removals that raised AND NSUserDefaults keys that survived the key pass; the
    breakdown is logged. `leftovers` names every failure as ("file"|"key", home-relative
    forward-slash path) so the post-restore verification (restorecheck.py) can TRIAGE
    them against the archive instead of surfacing a raw count as a fear.

    `progress(removed, total)` is called (throttled, every 100 files) so a long wipe shows
    a moving bar instead of a dead screen. It is passed COUNTS ONLY - never a per-file name:
    rapid changing-text redraws crash Kodi's text renderer on some devices (the exact bug
    wiz.ExtractWithProgress avoids), so the wipe UI must stay count-based."""
    import os

    keep_files = keep_files or set()

    # Pre-count so the bar shows a real percent. Cheap: it is just the walk, no per-file I/O.
    total = 0
    if progress is not None:
        for root, dirs, files in os.walk(home, topdown=True):
            dirs[:] = [d for d in dirs if d not in excludes]
            total += sum(1 for f in files if os.path.join(root, f) not in keep_files)
        try:
            progress(0, total)
        except Exception:
            pass

    _UPDATE_EVERY = 100
    removed = 0
    file_leftovers = []
    for root, dirs, files in os.walk(home, topdown=True):
        dirs[:] = [d for d in dirs if d not in excludes]
        for fname in files:
            path = os.path.join(root, fname)
            if path in keep_files:
                continue
            try:
                os.remove(path)
                removed += 1
            except Exception:
                try:
                    rel = os.path.relpath(path, home).replace("\\", "/")
                except ValueError:
                    rel = path
                file_leftovers.append(rel)
            if progress is not None and removed % _UPDATE_EVERY == 0:
                try:
                    progress(removed, total)
                except Exception:
                    pass
    if progress is not None:
        try:
            progress(total, total)
        except Exception:
            pass
    for root, dirs, _files in os.walk(home, topdown=False):
        for dname in dirs:
            if dname in excludes:
                continue
            try:
                os.rmdir(os.path.join(root, dname))  # only removes if now empty
            except Exception:
                pass

    # Layer 2 (tvOS only, hard-gated inside): drop the NSUserDefaults keys, else every
    # vectored userdata *.xml survives the "clean clone" and shadows the restore.
    keys_removed, key_survivors = _wipe_nsud_keys(home, excludes, keep_files)

    _log(
        "wipe: %d files removed (%d file failures), %d NSUserDefaults keys removed "
        "(%d keys survived)"
        % (removed, len(file_leftovers), keys_removed, len(key_survivors))
    )
    for rel in file_leftovers:
        _log("wipe: file survived the wipe: %s" % rel)
    leftovers = [("file", rel) for rel in file_leftovers] + [
        ("key", "userdata/" + rel) for rel in key_survivors
    ]
    return (removed, keys_removed, len(leftovers), leftovers)


def _is_tvos():
    """True ONLY on Apple TV (tvOS). Same hard safety gate as nsud._is_tvos: the key layer
    only exists on tvOS, and on every other platform special://home/userdata IS the plain
    POSIX tree, so the key pass must never run there. Detected via Kodi's own platform
    condition; defaults to False (the safe answer) on any error, so the NSUserDefaults
    layer is only ever touched when tvOS is positively confirmed."""
    try:
        import xbmc

        return bool(xbmc.getCondVisibility("System.Platform.TVOS"))
    except Exception:
        return False


# The key naming Kodi uses for vectored userdata files (nsub._USERDATA_PREFIX): a file at
# special://home/userdata/<rel> is stored under the NSUserDefaults key "/userdata/<rel>".
_NSUD_USERDATA_PREFIX = "/userdata/"


def _nsud_plist_store():
    """The loaded NSUserDefaults store (a dict of key -> value), read straight off Kodi's
    on-disk plist via nsub._find_nsud_plist - the mechanism this codebase already proved
    for the backup capture. Reading the plist DIRECTLY is the only reliable enumeration:
    keys are invisible to os.walk/listdir, and xbmcvfs reads of another writer's keys
    silently return empty (see nsub's module docstring). Returns None where no such store
    exists (every non-tvOS platform) or on any error."""
    try:
        from resources.lib.modules import nsub

        _path, store = nsub._find_nsud_plist()
        return store or None
    except Exception:
        return None


def _nsud_userdata_rels():
    """Every userdata-relative path (forward-slash) currently held as an NSUserDefaults
    key. [] when there is no store (Fire TV / desktop) or on any error."""
    store = _nsud_plist_store()
    if not store:
        return []
    rels = []
    for key in store:
        if not (isinstance(key, str) and key.startswith(_NSUD_USERDATA_PREFIX)):
            continue  # e.g. the 'UserdataMigrated' bookkeeping key
        rel = key[len(_NSUD_USERDATA_PREFIX) :].replace("\\", "/").lstrip("/")
        if rel:
            rels.append(rel)
    return rels


def _key_excluded(rel, excludes, keep_files, home):
    """Mirror of the POSIX walk's exclusion rule, for a userdata-relative key path: the
    walk prunes any DIRECTORY named in `excludes` at any depth, so a key is excluded when
    ANY of its path components matches (this covers this add-on's own addon_data/<id>/,
    the requests deps, temp, backupdir). A key whose on-disk twin is a `keep_files` path
    (Addons*.db) is also excluded - belt and braces only, since non-xml is never vectored
    into NSUserDefaults."""
    import os

    parts = [p for p in rel.split("/") if p]
    if any(p in excludes for p in parts):
        return True
    if keep_files:
        twin = os.path.normpath(os.path.join(home, "userdata", *parts))
        if twin in keep_files:
            return True
    return False


def _wipe_nsud_keys(home, excludes, keep_files=None):
    """tvOS ONLY (a strict, hard-gated no-op everywhere else): drop the NSUserDefaults key
    of every non-excluded userdata path, so the wipe clears BOTH layers.

    WHY: on tvOS a key SHADOWS the disk file (Kodi reads the key first and never copies a
    key back to disk), and after nsud's confirmed-vector-then-drop-POSIX flow many userdata
    files exist ONLY as keys - so they never even appear to the POSIX walk above. Skipping
    this pass leaves the whole old configuration alive to shadow the restored files.

    MECHANISM: keys are enumerated straight from Kodi's NSUserDefaults plist
    (nsub._find_nsud_plist, the codebase's proven tvOS access). Each key is dropped with
    xbmcvfs.delete() on its special:// path: on tvOS that call is dispatched to
    CTVOSFile::Delete, which drops EXACTLY the NSUserDefaults key and never touches the
    POSIX file (TVOSFile.cpp:101-111; the POSIX branch is unreachable for vectored paths).
    That is the same engine behavior that made a POSIX-only wipe incomplete - here it is
    used deliberately as the key-layer eraser, paired with the os.remove pass above for
    the file layer. Its boolean return is TRUE whether or not a key existed
    (TVOSNSUserDefaults.mm:188-202), so it is NEVER trusted: success is verified by
    re-reading the plist (CTVOSFile::Delete synchronizes the store to disk before
    returning) and every surviving key is counted as a failure and logged by name.

    Returns (keys_removed, survivors): survivors is the sorted list of userdata-relative
    keys that outlived the pass - named, so the post-restore verification can triage
    each one against the archive instead of reporting a bare count."""
    if not _is_tvos():
        return (0, [])
    keep_files = keep_files or set()

    def _targets():
        return [
            rel
            for rel in _nsud_userdata_rels()
            if not _key_excluded(rel, excludes, keep_files, home)
        ]

    before = _targets()
    if not before:
        return (0, [])
    for rel in before:
        try:
            # Drops ONLY the key on tvOS (see MECHANISM above); return value untrustworthy.
            xbmcvfs.delete("special://home/userdata/" + rel)
        except Exception:
            pass  # counted below by the survivor re-read
    survivors = set(_targets())
    removed = sum(1 for rel in before if rel not in survivors)
    for rel in sorted(survivors):
        _log(
            "wipe: NSUserDefaults key SURVIVED the wipe and will shadow a restore: %s"
            % rel
        )
    return (removed, sorted(survivors))


def keep_addon_db():
    """Absolute paths of Kodi's add-on state database (Addons*.db). Preserving it through a
    wipe keeps EZ Maintenance++ ENABLED on the restart instead of coming back disabled
    (which is what made it look 'gone' after a wipe)."""
    import glob
    import os

    try:
        db_dir = xbmcvfs.translatePath("special://home/userdata/Database")
        return set(glob.glob(os.path.join(db_dir, "Addons*.db")))
    except Exception:
        return set()


def keep_source_files():
    """Absolute paths of the file-manager source files to preserve through a Fresh Start
    when the user opts in ('Keep file manager sources'): userdata/sources.xml (the
    NFS/SMB/local sources themselves) AND userdata/passwords.xml (their saved
    credentials). Keeping sources.xml WITHOUT passwords.xml is a half-fix: Kodi stores
    NFS/SMB credentials as path-substitution entries in passwords.xml, so without it the
    sources come back but cannot authenticate. Per-profile copies are included for
    multi-profile setups (a no-op on the single-default-profile appliances).

    TWO-LAYER, and this is load-bearing on tvOS: `_wipe` protects a vectored
    NSUserDefaults key by twin-matching `_key_excluded`'s reconstructed absolute path
    against this set, so the path must be emitted even when NO POSIX file exists. On
    tvOS both of these files are routinely vectored into NSUserDefaults with the POSIX
    copy dropped (nsud._should_vector covers every top-level userdata/*.xml, and
    rewrite_userdata_xml drops the POSIX twin), which is the normal state after any
    restore. A POSIX-only existence test therefore returned an empty set exactly there,
    the twin-match had nothing to match, and the key was wiped: "Keep file manager
    sources" silently destroyed the sources and their saved credentials on the one
    platform the option matters most. So the key layer is enumerated too."""
    import glob
    import os

    keep = set()
    try:
        ud = xbmcvfs.translatePath("special://home/userdata")
        for name in ("sources.xml", "passwords.xml"):
            p = os.path.join(ud, name)
            if os.path.exists(p):
                keep.add(p)
            keep.update(glob.glob(os.path.join(ud, "profiles", "*", name)))
        # Key-only copies (tvOS). _nsud_userdata_rels() is [] off tvOS, so this whole
        # block is a strict no-op on Fire TV / desktop. The path is built exactly as
        # _key_excluded rebuilds its twin, so the match cannot drift.
        for rel in _nsud_userdata_rels():
            parts = [seg for seg in rel.replace("\\", "/").split("/") if seg]
            if parts and parts[-1] in ("sources.xml", "passwords.xml"):
                keep.add(os.path.normpath(os.path.join(ud, *parts)))
    except Exception:
        pass
    return keep


def repository_addon_names():
    """Directory NAMES of installed repositories (repository.*) to preserve through a
    Fresh Start when the user opts in ('Keep repositories'). Injected into the wipe's
    `excludes`, which match by name at any depth, so ONE name protects BOTH
    addons/<repo> and userdata/addon_data/<repo>. Kodi's kept Addons*.db keeps them
    ENABLED after the wipe; a repo needs no further deps (the requests/urllib3/chardet/
    idna/certifi stack is already kept), so the user can reinstall add-ons without
    re-adding repo sources."""
    import glob
    import os

    names = set()
    try:
        for base in ("special://home/addons", "special://home/userdata/addon_data"):
            root = xbmcvfs.translatePath(base)
            for p in glob.glob(os.path.join(root, "repository.*")):
                names.add(os.path.basename(p))
    except Exception:
        pass
    return names
