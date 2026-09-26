import xbmc
import xbmcaddon
import xbmcgui
import os
import glob
import json
import sqlite3
import xbmcvfs
import math
import time
import shutil
from resources.lib.modules.backtothefuture import PY2
from resources.lib.modules import addon_id as _addon_id
from resources.lib.modules import ui

# Code to map the old translatePath
if PY2:
    translatePath = xbmc.translatePath
    loglevel = xbmc.LOGNOTICE
else:
    translatePath = xbmcvfs.translatePath
    loglevel = xbmc.LOGINFO

thumbnailPath = translatePath("special://thumbnails")
cachePath = os.path.join(translatePath("special://home"), "cache")
tempPath = translatePath("special://temp")
databasePath = translatePath("special://database")
THUMBS = translatePath(os.path.join("special://home/userdata/Thumbnails", ""))

# Must match <default>4</default> for autoCleanHour in resources/settings.xml. Kodi's
# settings UI renders that declared default for an absent setting, so a fallback that
# disagrees with it would run maintenance at an hour the user was never shown.
DEFAULT_AUTOCLEAN_HOUR = 4

addon_id = _addon_id.get()
fanart = translatePath(os.path.join("special://home/addons/" + addon_id, "fanart.jpg"))
iconpath = translatePath(os.path.join("special://home/addons/" + addon_id, "icon.png"))

# Names never deleted by the cache clean. The dir names are KEPT as directories
# but their CONTENTS are still cleaned (same rules), matching what the old
# os.walk pass did by recursing into them.
KEEP_FILES = (
    "xbmc.log",
    "xbmc.old.log",
    "kodi.log",
    "kodi.old.log",
    "archive_cache",
    "commoncache.db",
    "commoncache.socket",
    "temp",
)
KEEP_DIRS = ("archive_cache", "temp")

# JSON-RPC "Invalid params." Kodi returns this for Textures.RemoveTexture against an
# id that no longer exists, which is the benign enumerate/remove race, not a failure.
_RPC_INVALID_PARAMS = -32602


def _clean_tree(path, keep_files=(), keep_dirs=(), remove_dirs=True):
    """Empty a directory in one top-level pass: unlink files, rmtree subdirs.

    A subdir named in keep_dirs is kept but its contents are cleaned with the
    same rules. remove_dirs=False keeps the whole directory skeleton and only
    unlinks files, recursively. Every per-entry error is swallowed - this runs
    against live Kodi caches where entries can vanish mid-scan.

    (Replaces per-caller os.walk loops that nested the rmtree pass inside an
    `if file_count > 0:` gate, so a level holding subdirectories but no loose
    files was never cleaned at all.)"""
    try:
        entries = list(os.scandir(path))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                if entry.name in keep_dirs or not remove_dirs:
                    _clean_tree(entry.path, keep_files, keep_dirs, remove_dirs)
                else:
                    shutil.rmtree(entry.path, ignore_errors=True)
            elif entry.name not in keep_files:
                os.unlink(entry.path)
        except OSError:
            pass


def clearCache(mode="verbose"):
    _clean_tree(cachePath, KEEP_FILES, KEEP_DIRS)
    _clean_tree(tempPath, KEEP_FILES, KEEP_DIRS)

    if mode == "verbose":
        ui.notify("Clean Completed", icon=iconpath, time_ms=3000)


def _purge_texture_cache():
    """Empty Kodi's texture cache THROUGH Kodi. Never by unlinking Textures13.db.

    `os.unlink(Textures13.db)` under a live Kodi is a delayed-action crash, not a
    clean. Kodi keeps an open handle on the now-unlinked inode and carries on; the
    NEXT texture-cache write fails `SQLITE_READONLY_DBMOVED`, and every write after
    that fails `SQLITE_MISUSE` on the poisoned handle. On Android that storm aborts
    the process. It killed the office Fire TV on 2026-07-21: a backup at 09:55 did
    the unlink (this function runs first in every backup), the box ran on happily,
    and it took SIGABRT at 10:01:13 the moment a skin change touched the texture DB
    six minutes later. Reproduced on the macOS bench with the unlink as the sole
    action - no skin change, no add-on code - so the trigger is any texture write,
    not anything specific to that session.

    `Textures.RemoveTexture` is the sanctioned emptier: Kodi deletes the row AND the
    cached image file it points at, with the database still its own. Returns
    (removed, failed); (0, 0) when the cache is already empty or cannot be
    enumerated.

    KNOWN LIMIT, measured on the macOS bench 2026-07-21: `Textures.GetTextures`
    enumerates a JOIN against the `sizes` table, not the `texture` table. With 200
    texture rows and 0 sizes rows it returned 0 textures; adding one sizes row per
    texture and changing nothing else returned all 200. So a row whose caching began
    and never finished is invisible here and survives the purge. That is acceptable
    only because Kodi evicts such a row itself the next time it is used: a cached
    file that is gone logs "Direct texture file loading failed" and Kodi's very next
    statement is "DELETE FROM texture WHERE url=..." (observed on the office Fire TV,
    2026-07-21 10:01:09). Do NOT "fix" this by going back to unlinking the file.
    """
    got = _jsonrpc("Textures.GetTextures", {})
    # `(x or {})`, never `x.get("result", {})`: a "result": null reply would make the
    # second .get() raise, and in the backup path wiz.py swallows it with a bare
    # except, silently skipping the rest of the pre-backup clean.
    textures = (got.get("result") or {}).get("textures") or []
    removed = 0
    failed = 0
    for texture in textures:
        tid = texture.get("textureid")
        if tid is None:
            continue
        result = _jsonrpc("Textures.RemoveTexture", {"textureid": tid})
        if result.get("result") == "OK":
            removed += 1
        elif result.get("error", {}).get("code") == _RPC_INVALID_PARAMS:
            # The row went away between the enumerate and the remove - Kodi re-caches
            # constantly, so this is routine on a live box and is NOT a failure. It
            # was counted as one at first, which made a fully successful purge of
            # 5007 rows report "3 failed" and log a warning nobody should act on.
            removed += 1
        else:
            failed += 1
    # No silent partial: rows that genuinely refused to go, or a cache that could not
    # be enumerated at all, are stated in the log rather than reported as a clean sweep.
    if failed or (not textures and got.get("error")):
        xbmc.log(
            "EZ Maintenance++ : texture cache purge incomplete - %d removed, %d "
            "failed, enumerate error %r" % (removed, failed, got.get("error")),
            level=xbmc.LOGWARNING,
        )
    return removed, failed


def deleteThumbnails(mode="verbose"):
    # Drop the cached rows through Kodi FIRST (it deletes each cached file with its
    # row), then sweep the disk for orphans the database never knew about - the bench
    # held 8 thumbnail files against 4 rows, so the disk pass is load-bearing and runs
    # unconditionally. A row left behind by a failed purge is self-healing (see
    # _purge_texture_cache), so a partial purge is not a reason to skip the sweep.
    _purge_texture_cache()
    # special://thumbnails: keep Kodi's 0-f/ bucket skeleton, drop the images.
    _clean_tree(thumbnailPath, remove_dirs=False)
    # On a real box special://thumbnails ALIASES userdata/Thumbnails - the two
    # paths are the SAME directory, and a dir-removing second pass would rmtree
    # the bucket skeleton the first pass just preserved. (The old walk got this
    # right only by accident: its rmtree was gated behind `if file_count > 0`,
    # and pass 1 had already emptied every level.) Only a genuinely separate
    # legacy dir is removed whole.
    try:
        aliased = os.path.realpath(THUMBS) == os.path.realpath(thumbnailPath)
    except OSError:
        aliased = True  # fail safe: never risk the live skeleton
    if not aliased:
        _clean_tree(THUMBS)

    if mode == "verbose":
        ui.notify("Clean Thumbs Completed", icon=iconpath, time_ms=3000)


def purgePackages(mode="verbose"):
    purgePath = translatePath("special://home/addons/packages")
    _clean_tree(purgePath)
    if mode == "verbose":
        ui.notify("Clean Packages Completed", icon=iconpath, time_ms=3000)


def _jsonrpc(method, params):
    try:
        return json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                )
            )
        )
    except Exception:
        return {}


def _pvr_databases():
    """The CURRENT TV and Radio PVR databases (highest-numbered schema of each).

    Kodi migrates the PVR DB across versions as TV<N>.db / Radio<N>.db and uses
    the highest number; older ones are stale and left alone.
    """
    out = []
    for prefix in ("TV", "Radio"):

        def _num(p):
            digits = "".join(c for c in os.path.basename(p) if c.isdigit())
            return int(digits) if digits else 0

        cands = sorted(
            glob.glob(os.path.join(databasePath, prefix + "[0-9]*.db")), key=_num
        )
        if cands:
            out.append(cands[-1])
    return out


def _count_recent_channels(dbs):
    total = 0
    for db in dbs:
        try:
            con = sqlite3.connect(db)
            try:
                total += con.execute(
                    "SELECT COUNT(*) FROM channels WHERE iLastWatched > 0"
                ).fetchone()[0]
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return total


def _reset_recent_channels(dbs):
    cleared = 0
    for db in dbs:
        try:
            con = sqlite3.connect(db)
            try:
                cur = con.execute(
                    "UPDATE channels SET iLastWatched = 0, iLastWatchedGroupId = 0 "
                    "WHERE iLastWatched > 0"
                )
                cleared += cur.rowcount
                con.commit()
            finally:
                con.close()
        except sqlite3.Error:
            pass
    return cleared


def clearRecentChannels(mode="verbose"):
    """Clear the PVR 'recently played channels' list by resetting iLastWatched in
    the current TV/Radio databases. No-op (with a notice) when none are found.

    Two things are needed and both are hardware-proven:

    1. The write happens inside a PVR-disabled window so the running client
       cannot clobber the reset (the Kodi settings-clobber class).
    2. Kodi must be RESTARTED afterward for the change to show. The home widget
       reads ``pvr://channels/tv/*?view=lastplayed``, which Kodi serves from the
       PVR manager's IN-MEMORY channel state - not the disk DB. A skin reload and
       a pvrmanager pause/resume both leave the stale channel on screen; only a
       full restart reloads lastplayed from the (now-cleared) database. So in
       verbose mode we offer a restart after clearing.
    """
    dbs = _pvr_databases()
    if _count_recent_channels(dbs) == 0:
        if mode == "verbose":
            ui.notify("No recently played channels", icon=iconpath, time_ms=3000)
        return 0

    r = _jsonrpc("Settings.GetSettingValue", {"setting": "pvrmanager.enabled"})
    was_on = bool((r.get("result") or {}).get("value"))
    try:
        if was_on:
            _jsonrpc(
                "Settings.SetSettingValue",
                {"setting": "pvrmanager.enabled", "value": False},
            )
            xbmc.sleep(2000)  # let the manager flush its in-memory state + release
        cleared = _reset_recent_channels(dbs)
    finally:
        if was_on:
            _jsonrpc(
                "Settings.SetSettingValue",
                {"setting": "pvrmanager.enabled", "value": True},
            )
    if mode == "verbose" and cleared:
        ui.ask_restart(
            "Cleared %d recently played channel(s). Kodi must reload for the "
            "home screen to update." % cleared
        )
    return cleared


def clearAll(mode="verbose"):
    """One action that runs every clean: cache, packages, thumbnails, and (if any
    are found) the recently played channels. Offers a restart when channels were
    cleared, since the home widget only reflects that after a Kodi reload."""
    clearCache(mode="silent")
    purgePackages(mode="silent")
    deleteThumbnails(mode="silent")
    cleared = clearRecentChannels(mode="silent")
    if mode == "verbose":
        if cleared:
            ui.ask_restart(
                "All cleaned, including %d recently played channel(s). Kodi must "
                "reload for the home screen to update." % cleared
            )
        else:
            ui.notify("All Cleaned", icon=iconpath, time_ms=3000)


def determineNextMaintenance():
    getSetting = xbmcaddon.Addon().getSetting

    # Kodi's getSetting returns "" (never None) for an absent or blank setting, so the old
    # `is None` guards were dead code and int("") raised. This runs on the SERVICE thread at
    # boot (service.py), where an uncaught ValueError kills the scheduler for the session -
    # so degrade to "no schedule" instead, same as getNextMaintenance below.
    try:
        days = int(getSetting("autoCleanDays"))
    except (TypeError, ValueError):
        # Loud: this silently turns auto-clean OFF for the session, so it must not be
        # invisible. A user who configured a schedule would otherwise never learn it
        # stopped running.
        xbmc.log(
            "ezmaintenanceplusplus: autoCleanDays is not a number (%r); "
            "auto-clean scheduling is DISABLED for this session"
            % (getSetting("autoCleanDays"),),
            level=xbmc.LOGWARNING,
        )
        days = 0

    t1 = 0

    if days > 0:
        # A bad hour must not cost us the whole schedule. Fall back to the value
        # settings.xml DECLARES as the default (4 AM, deliberately off-hours) - Kodi's
        # settings UI shows that default for an absent setting, so falling back to
        # anything else would run maintenance at an hour the user was never shown.
        try:
            hour = int(getSetting("autoCleanHour"))
        except (TypeError, ValueError):
            xbmc.log(
                "ezmaintenanceplusplus: autoCleanHour is not a number (%r); "
                "falling back to the declared default of %02d:00, schedule preserved"
                % (getSetting("autoCleanHour"), DEFAULT_AUTOCLEAN_HOUR),
                level=xbmc.LOGWARNING,
            )
            hour = DEFAULT_AUTOCLEAN_HOUR

        t0 = int(math.floor(time.time()))

        t1 = t0 + (days * 24 * 60 * 60)  # days * 24h * 60m * 60s

        x = time.localtime(t1)

        t1 += (hour - x.tm_hour) * 60 * 60 - x.tm_min * 60 - x.tm_sec
        while t1 <= t0:
            t1 += 24 * 60 * 60  # add days until we are in the future

        # t1 = t0 + 1 * 60 # for testing - every minute

    win = xbmcgui.Window(10000)
    win.setProperty("ezmaintenance.nextMaintenanceTime", str(t1))

    logMaintenance("setNextMaintenance: %s" % str(t1))


def getNextMaintenance():
    # Read from the PLUGIN process too (default.py's Maintenance submenu), where
    # nothing guarantees the service has set the property yet - default to 0
    # (no schedule) instead of blowing up the listing on int("").
    win = xbmcgui.Window(10000)
    try:
        t1 = int(win.getProperty("ezmaintenance.nextMaintenanceTime"))
    except (TypeError, ValueError):
        t1 = 0

    logMaintenance("getNextMaintenance: %s" % str(t1))

    return t1


def logMaintenance(message):
    #    xbmc.log("ezmaintenanceplus: %s" % message, level=loglevel)
    return
