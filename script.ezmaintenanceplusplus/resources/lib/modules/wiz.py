"""
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
import json
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from resources.lib.modules import control, maintenance, tools, ui, versiongate
from datetime import datetime
from resources.lib.modules.backtothefuture import unicode, PY2

if PY2:
    from io import open as open

    translatePath = xbmc.translatePath
else:
    translatePath = xbmcvfs.translatePath
    unicode = str

dialog = xbmcgui.Dialog()
addonInfo = xbmcaddon.Addon().getAddonInfo

AddonTitle = "EZ Maintenance++"
AddonID = "script.ezmaintenanceplusplus"


# VfsCopyError now lives in ui.py (one definition for the whole add-on); alias it here so
# `wiz.VfsCopyError` and `except VfsCopyError` keep working. A FAILED ship raises this so
# backup() reports an error and SKIPS rotation (a cancel instead returns canceled=True),
# and the prior good backup is never pruned behind a ship that never landed.
VfsCopyError = ui.VfsCopyError


class BackupCaptureError(Exception):
    """A tvOS NSUserDefaults capture failure. On Apple TV the owner's settings live
    ONLY in NSUserDefaults, so a failed capture means the backup is MISSING exactly
    the data the owner cares about: the backup must FAIL loudly (error dialog, log,
    no success report, no rotation) rather than land silently incomplete."""


# The self-describing metadata member every backup zip carries (read back by
# restore()'s manifest verification and by default.py's analyze_backup_zip).
MANIFEST_NAME = "backup_manifest.json"

# EZM's own settings.xml (userdata-relative, forward-slash) - the ONE addon_data
# file a backup must never embed: it carries the source box's download/restore
# paths and the dropbox_refresh_token. Matched by suffix so a per-profile copy is
# covered too. Kept in lockstep with nsub._SECRET_TAIL (the tvOS capture path).
_SECRET_TAIL = "addon_data/%s/settings.xml" % AddonID

# EZM's own add-on tree inside a HOME-anchored archive. A restore must never write
# this: the extract would overwrite the RUNNING add-on with whatever version the
# archive happens to carry, so a box restoring last week's backup silently boots the
# older EZ Maintenance++ and quietly undoes every fix shipped since. The add-on the
# box already has is by definition the one that just ran the restore, and it comes
# from the Kodi repo, so there is nothing to recover here.
_SELF_ADDON_PREFIX = "addons/%s/" % AddonID


def _is_secret_arc(arc):
    """True iff `arc` (a home- OR userdata-anchored zip arcname) is EZM's own
    settings.xml, in the top-level or any per-profile userdata."""
    a = (arc or "").replace("\\", "/").lstrip("/")
    return a.endswith(_SECRET_TAIL)


def _is_regenerable_cache_arc(arc):
    """True iff `arc` is Kodi's texture-cache database, which a backup must never carry.

    It used to be absent by accident: maintenance.deleteThumbnails() ran first in every
    backup and UNLINKED the file, so the walk never saw it. That unlink crashed the
    office Fire TV on 2026-07-21 (see maintenance._purge_texture_cache) and is gone, so
    the file is now present at walk time and has to be excluded on purpose.

    Excluding it is right on its own merits, not just to hold the old archive size.
    SQLite never returns freed space: measured on the bench, a Textures13.db emptied of
    all 5007 rows still occupied 1.2 MB with 286 of 295 pages on the freelist and
    auto_vacuum off, and the freed pages keep their old contents, so it does not
    compress away either (225 KB deflated of pure dead weight). It is also a live
    database being copied with no quiesce. Nothing is lost by dropping it: the archive
    already carries an emptied Thumbnails tree, so a restored box rebuilds its texture
    cache from scratch exactly as it does today.
    """
    a = (arc or "").replace("\\", "/").lstrip("/")
    # ANCHORED to the real database directory. A bare "/Database/" match at any depth
    # also swallowed userdata/addon_data/<add-on>/Database/TexturesAnything.db - real
    # third-party user data, dropped from a backup that promises "full means full",
    # with nothing in the manifest to say so. Both anchors, because the walk root is
    # special://home for a full backup and userdata itself for a userdata-only one.
    if not (a.startswith("userdata/Database/") or a.startswith("Database/")):
        return False
    base = a.rsplit("/", 1)[-1]
    # `.db*`, not `.db`: a stray Textures13.db-journal restored beside a surviving
    # database is the silent-rollback hazard keep_live_databases() exists to avoid.
    return base.startswith("Textures") and ".db" in base


def _source_os():
    """'tvos' | 'android' | 'other', via Kodi's own platform conditions (the same
    detection nsud._is_tvos uses). Defaults to 'other' on any error."""
    try:
        if xbmc.getCondVisibility("System.Platform.TVOS"):
            return "tvos"
        if xbmc.getCondVisibility("System.Platform.Android"):
            return "android"
    except Exception:
        pass
    return "other"


class ZipResult(object):
    """CreateZip's truthful outcome: whether the user canceled, how many entries
    landed in the zip, and exactly which paths could NOT be captured."""

    def __init__(self, canceled=False, entries=0, failed=None):
        self.canceled = bool(canceled)
        self.entries = int(entries)
        self.failed = list(failed or [])


def _as_zip_result(res):
    """Normalize a CreateZip return. Tolerates a bare canceled bool (older callers
    and test stubs) by wrapping it with no failure information."""
    if isinstance(res, ZipResult):
        return res
    return ZipResult(canceled=bool(res))


class ExtractResult(object):
    """ExtractWithProgress's truthful outcome: what actually landed on disk, what
    was intentionally skipped, and exactly which members failed to extract.
    extracted == -1 means 'unknown' (a bare-bool return was normalized)."""

    def __init__(self, canceled=False, extracted=0, skipped=0, total=0, failed=None):
        self.canceled = bool(canceled)
        self.extracted = extracted
        self.skipped = skipped
        self.total = total
        self.failed = list(failed or [])


def _as_extract_result(res):
    """Normalize an extract return. Tolerates a bare canceled bool (older callers
    and test stubs), which carries no per-member counts (extracted == -1)."""
    if isinstance(res, ExtractResult):
        return res
    return ExtractResult(canceled=bool(res), extracted=-1)


def _failed_lines(failed, limit=10):
    """Human-readable bullet lines for a failure list, truncated for a dialog."""
    lines = [" - %s" % f for f in failed[:limit]]
    if len(failed) > limit:
        lines.append(" - ... and %d more (see the log)" % (len(failed) - limit))
    return lines


def _write_manifest(zip_file, entries, failed):
    """Embed MANIFEST_NAME into the (still open) backup zip: when it was made, on
    which OS and which Kodi major, how many entries it holds, and EXACTLY which
    paths it could not capture. A backup that is missing something must say so -
    restore() verifies the archive against this and the owner tools display it."""
    manifest = {
        "created": datetime.now().replace(microsecond=0).isoformat(),
        "source_os": _source_os(),
        # The Kodi MAJOR this backup was made on (0 = could not be read).
        # restore()'s version gate compares it with the running major before
        # letting the archive's addons/ tree extract - an Omega addons/ tree
        # laid over a Piers install un-rebuilds the box. See versiongate.py.
        "kodi_version": versiongate.major(get_Kodi_Version()),
        "entries": int(entries),
        "failed": [unicode(f) for f in failed],
    }
    zip_file.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True))


def _read_manifest(zip_path):
    """The embedded MANIFEST_NAME as a dict, or None. Older backups have no
    manifest - tolerated; an unreadable/foreign-shaped manifest is treated as
    absent rather than failing the restore."""
    try:
        with zipfile.ZipFile(zip_path) as z:
            if MANIFEST_NAME not in z.namelist():
                return None
            data = z.read(MANIFEST_NAME)
        manifest = json.loads(data.decode("utf-8"))
        return manifest if isinstance(manifest, dict) else None
    except Exception:
        return None


def _manifest_problems(manifest, namelist):
    """Verify the archive against its own manifest. Returns human-readable problem
    strings ([] when everything checks out): a member-count mismatch means the
    archive does not hold what the backup said it wrote, and a non-empty 'failed'
    list means the BACKUP itself already knew it was missing those paths."""
    problems = []
    member_names = [n for n in namelist if n != MANIFEST_NAME]
    members = len(member_names)
    raw = manifest.get("entries", -1)
    if isinstance(raw, (list, tuple)):
        # A name-list manifest: strictly better diagnostics - report exactly
        # which promised entries the archive does not hold.
        missing = sorted(set(unicode(e) for e in raw) - set(member_names))
        if missing:
            problems.append(
                "manifest mismatch: %d entrie(s) promised by the manifest are "
                "missing from the archive: %s" % (len(missing), ", ".join(missing[:5]))
            )
    else:
        try:
            expected = int(raw)
        except (TypeError, ValueError):
            expected = -1
        if expected >= 0 and expected != members:
            problems.append(
                "manifest mismatch: the archive holds %d entries but its manifest "
                "recorded %d" % (members, expected)
            )
    failed = manifest.get("failed")
    if isinstance(failed, (list, tuple)) and failed:
        shown = ", ".join(unicode(f) for f in list(failed)[:5])
        if len(failed) > 5:
            shown += ", and %d more" % (len(failed) - 5)
        problems.append(
            "the backup itself could not capture %d item(s): %s" % (len(failed), shown)
        )
    return problems


# Kodi's own network-browse dialog (used to pick download.path/restore.path -
# both "type=folder" settings, browse-only, no manual text entry) bakes an
# explicit port into the nfs:// URL it hands back, e.g.
# nfs://192.168.7.2:2049/export/path. That explicit-port form breaks Kodi's
# own NFS client write path - proven live, independently, on two different
# boxes (a VfsCopyError / 0-byte copy every time) - while the port-free form
# (nfs://host/export/path) works. Since the destination setting can only ever
# be set via that same browse dialog, this can recur on every future box;
# strip the port defensively wherever the setting is read rather than relying
# on a one-off manual edit each time.
_NFS_PORT_RE = re.compile(r"^(nfs://[^/]+?):\d+(/|$)")


def _strip_nfs_port(path):
    """Strip an explicit port from an nfs:// VFS path; anything else (smb://,
    a local path, empty/None) passes through unchanged."""
    if not path:
        return path
    return _NFS_PORT_RE.sub(r"\1\2", path)


def configured_path(setting_id):
    """The path a path-taking action will ACTUALLY use, or "" when unconfigured.

    ONE definition of "configured", shared by the actions and by the menu row that
    reports them (default._path_detail). They used to disagree twice over:

      * Whitespace. The row stripped and said "Not set"; the actions compared the
        RAW value to "" and so treated "   " as a configured folder, walked past the
        bail-to-settings guard, and failed later with something the owner could not
        act on. The row told the truth and the action did not.
      * The nfs:// port. Both actions run the setting through _strip_nfs_port (Kodi's
        browse dialog bakes :2049 in and that form breaks Kodi's own NFS writes), but
        the row printed the raw setting, so it showed a port the add-on discards. The
        live boxes do carry :2049 in that setting, so this was on screen every day.

    Returns "" for None, an unset setting, and a whitespace-only setting alike, so
    every caller can test one thing."""
    try:
        raw = control.setting(setting_id)
    except Exception:
        return ""
    return _strip_nfs_port((raw or "").strip()) or ""


# Backup filenames carry a trailing _YYYYMMDDHHMM stamp before ".zip".
_STAMP_RE = re.compile(r"_(\d{12})\.zip$", re.IGNORECASE)


def _name_stamp(name):
    """Parse the trailing _YYYYMMDDHHMM stamp from a backup filename.

    Returns the 12-digit string (lexically == chronologically sortable) or ""
    when the file has no tool stamp (e.g. a user-renamed backup).
    """
    m = _STAMP_RE.search(name or "")
    return m.group(1) if m else ""


# A "keep" token anywhere in a name marks a protected backup even if it still
# carries a stamp (rescues a user who renamed but left the date on).
_KEEP_TOKEN_RE = re.compile(r"keep", re.IGNORECASE)


def _is_rolling(name):
    """True only for a tool-created ROLLING backup that keep-N may prune.

    A rolling backup matches the tool's own naming contract: a trailing
    _YYYYMMDDHHMM.zip stamp (always appended at backup time) AND no explicit
    'keep' token. Every other file - a user rename, a manual copy, a foreign zip -
    is PROTECTED: rotation never counts it toward keep-N and never deletes it.
    Renaming is exactly how users protect a golden backup, so this fails SAFE:
    an unrecognized name is always protected, never a rotation victim.
    """
    if not _name_stamp(name):
        return False
    if _KEEP_TOKEN_RE.search(name or ""):
        return False
    return True


def get_Kodi_Version():
    try:
        KODIV = float(xbmc.getInfoLabel("System.BuildVersion")[:4])
    except:
        KODIV = 0
    return KODIV


def FIX_SPECIAL():

    HOME = translatePath("special://home")
    dp = xbmcgui.DialogProgress()
    dp.create(AddonTitle, "Renaming paths...")
    url = translatePath("special://userdata")
    for root, dirs, files in os.walk(url):
        for file in files:
            if file.endswith(".xml"):
                if PY2:
                    dp.update(0, "Fixing", "[COLOR dodgerblue]" + file + "[/COLOR]")
                else:
                    dp.update(
                        0, "Fixing" + "\n" + "[COLOR dodgerblue]" + file + "[/COLOR]"
                    )
                try:
                    a = open((os.path.join(root, file)), "r", encoding="utf-8").read()
                    b = a.replace(HOME, "special://home/")
                    f = open((os.path.join(root, file)), mode="w", encoding="utf-8")
                    f.write(unicode(b))
                    f.close()
                except:
                    try:
                        a = open((os.path.join(root, file)), "r").read()
                        b = a.replace(HOME, "special://home/")
                        f = open((os.path.join(root, file)), mode="w")
                        f.write(unicode(b))
                        f.close()
                    except:
                        pass


def _destination():
    # 0 = Local, 1 = Network (SMB/NFS), 2 = Dropbox. Default 0.
    try:
        return int(control.setting("destination") or "0")
    except:
        return 0


def _device_backup_prefix():
    """Sanitized services.devicename for backup filenames, or "" for no prefix.

    The bootstrap names each box with its fleet alias (ts1, atv2, office...), so
    a backup made there is "<devicename>_kodi_backup_<stamp>.zip" and you can
    tell whose archive it is from the listing. A box with NO real name - the
    setting empty, unreadable, or still Kodi's stock default "Kodi" - gets no
    prefix and keeps the plain historical name, so nothing ever writes a
    meaningless "kodi_kodi_backup". Sanitized for filenames: lowercased, spaces
    to underscores, everything outside [a-z0-9_-] stripped. The prefix only
    changes the SUGGESTED name: every consumer (restore's listing, rotation,
    Dropbox ordering, the type hint) matches on the trailing stamp or a
    substring, never on this prefix, so old plain-named archives stay listed
    and restorable forever.
    """
    try:
        name = tools._get_devicename()
    except Exception:
        name = ""
    name = (name or "").strip().lower()
    name = re.sub(r"\s+", "_", name)
    name = re.sub(r"[^a-z0-9_-]", "", name)
    # A name that was ONLY separators/symbols must not leave a bare "_" prefix.
    name = name.strip("_-")
    if not name or name == "kodi":
        return ""
    return name


def _default_backup_name(base):
    """The suggested backup filename: '<devicename>_<base>', or plain <base>."""
    prefix = _device_backup_prefix()
    return "%s_%s" % (prefix, base) if prefix else base


# BACKUP ZIP
def backup(mode="full"):
    dest = _destination()

    if mode == "full":
        defaultName = _default_backup_name("kodi_backup")
        BACKUPDATA = control.HOME
        getSetting = xbmcaddon.Addon().getSetting
        if getSetting("BackupFixSpecialHome") == "true":
            FIX_SPECIAL()
    elif mode == "userdata":
        defaultName = _default_backup_name("kodi_settings")
        BACKUPDATA = control.USERDATA
    else:
        return

    if not os.path.exists(BACKUPDATA):
        return

    if dest == 2:
        _backup_dropbox(mode, defaultName, BACKUPDATA)
        return

    # Local / Network (SMB/NFS): path-based CreateZip (VFS copy seam handles nfs://, smb://).
    backupdir = configured_path("download.path")
    if backupdir == "":
        control.infoDialog("Please Setup a Path for Downloads first")
        # ON the Backup/Restore tab, where download.path actually is. A plain
        # openSettings() opens on the FIRST category, Maintenance: she is told to set
        # a path and then dropped on the wrong tab, with nothing saying which one.
        control.openSettingsTab(control.SETTINGS_TAB_BACKUP_RESTORE)
        return

    name = tools._get_keyboard(
        default=defaultName, heading="Name your Backup", cancel="-"
    )
    if name == "-":
        return
    today = datetime.now().strftime("%Y%m%d%H%M")
    today = re.sub("[^0-9]", "", str(today))
    zipDATE = "_%s.zip" % today
    name = re.sub(" ", "_", name) + zipDATE
    # Confirm the final filename (with the auto timestamp) BEFORE committing to the
    # zip build - parity with restore's confirm. The name also decides rotation
    # protection (see _rotate_vfs), so a fat-fingered name is worth catching here.
    if not ui.confirm(
        "Create this backup?\n\n%s" % name,
        heading=AddonTitle,
        yeslabel="Back up",
        nolabel="Cancel",
    ):
        return
    backup_zip = translatePath(os.path.join(backupdir, name))
    if not _confirm_destination_survives_a_purge(backup_zip):
        return
    exclude_database = [".pyo", ".log"]

    try:
        maintenance.clearCache(mode="silent")
        maintenance.deleteThumbnails(mode="silent")
        maintenance.purgePackages(mode="silent")
    except:
        pass

    # Only special://home/temp AT THE WALK ROOT (transient + self-referential);
    # a nested dir that merely happens to be named "temp" is real content.
    exclude_dirs = ["temp"]
    try:
        result = _as_zip_result(
            CreateZip(
                BACKUPDATA,
                backup_zip,
                "Creating Backup",
                "Backing up files",
                exclude_dirs,
                exclude_database,
                prune_home_root=(mode == "full"),
            )
        )
    except VfsCopyError as e:
        # The ship to the share/path FAILED. Never report success and never
        # rotate - the prior good backup stays untouched.
        xbmc.log(
            "%s : backup copy failed: %s" % (AddonTitle, type(e).__name__),
            level=xbmc.LOGERROR,
        )
        dialog.ok(
            AddonTitle,
            "Backup failed: could not write to the Backup Location. "
            "Your previous backup was not touched.",
        )
        return
    except BackupCaptureError as e:
        # tvOS settings capture FAILED: the zip would silently miss the owner's
        # settings. Fail loudly - no success dialog, no rotation, no partial zip.
        xbmc.log("%s : backup failed: %s" % (AddonTitle, e), level=xbmc.LOGERROR)
        try:
            xbmcvfs.delete(backup_zip)  # VFS-safe: handles nfs:// and local
        except Exception:
            pass
        dialog.ok(
            AddonTitle,
            "Backup FAILED: this device's settings could not be captured (%s). "
            "No backup was created. Your previous backup was not touched." % e,
        )
        return
    if result.canceled:
        try:
            xbmcvfs.delete(backup_zip)  # VFS-safe: handles nfs:// and local
        except:
            pass
        dialog.ok(AddonTitle, "Backup canceled")
    else:
        # Only rotate AFTER a confirmed-landed backup - and NEVER when this backup is
        # itself incomplete: rotating on a swiss-cheese zip can delete the last COMPLETE
        # backup in favor of one that is missing files (with backup.keep=1 that loss is
        # total). An incomplete backup is kept, reported honestly, and rotation waits
        # for the next clean run.
        if not result.failed:
            _rotate_vfs(backupdir, protect={name})
        _report_backup_done(result)


def _backup_dropbox(mode, defaultName, BACKUPDATA):
    from resources.lib.modules import dropbox_remote

    if not control.setting("dropbox_refresh_token").strip():
        control.infoDialog("Sign in to Dropbox first (Settings -> Backup/Restore)")
        return

    name = tools._get_keyboard(
        default=defaultName, heading="Name your Backup", cancel="-"
    )
    if name == "-":
        return
    today = datetime.now().strftime("%Y%m%d%H%M")
    today = re.sub("[^0-9]", "", str(today))
    name = re.sub(" ", "_", name) + ("_%s.zip" % today)
    # Confirm the final filename before committing (parity with the local/network
    # path and with restore's confirm).
    if not ui.confirm(
        "Create this Dropbox backup?\n\n%s" % name,
        heading=AddonTitle,
        yeslabel="Back up",
        nolabel="Cancel",
    ):
        return

    try:
        maintenance.clearCache(mode="silent")
        maintenance.deleteThumbnails(mode="silent")
        maintenance.purgePackages(mode="silent")
    except:
        pass

    # Keep the box awake for the whole backup - zip build + upload can run for several
    # minutes, and an idle/screensaver suspension was stalling the upload. Released in
    # the finally below. (On tvOS the OS screensaver can still appear, but the upload's
    # per-chunk resume rides out a brief stall instead of restarting.)
    xbmc.executebuiltin("InhibitIdleShutdown(true)")

    # Build the zip LOCALLY in special://temp (no "://" so CreateZip skips its VFS copy
    # branch), then ship it to Dropbox. The prior remote backup stays untouched unless
    # this upload confirms, so a failed run never destroys the last good backup.
    staged = "special://temp/" + name
    # Only special://home/temp AT THE WALK ROOT (transient + self-referential);
    # a nested dir that merely happens to be named "temp" is real content.
    exclude_dirs = ["temp"]
    exclude_database = [".pyo", ".log"]
    try:
        try:
            result = _as_zip_result(
                CreateZip(
                    BACKUPDATA,
                    translatePath(staged),
                    "Creating Backup",
                    "Backing up files",
                    exclude_dirs,
                    exclude_database,
                    prune_home_root=(mode == "full"),
                )
            )
        except BackupCaptureError as e:
            # tvOS settings capture FAILED: the zip would silently miss the owner's
            # settings. Fail loudly - nothing is uploaded, nothing is rotated.
            xbmc.log("%s : backup failed: %s" % (AddonTitle, e), level=xbmc.LOGERROR)
            dialog.ok(
                AddonTitle,
                "Backup FAILED: this device's settings could not be captured (%s). "
                "Nothing was uploaded. Your previous backup was not touched." % e,
            )
            return
        if result.canceled:
            dialog.ok(AddonTitle, "Backup canceled")
            return
        try:
            # One uniform upload gauge. The cancel is handled by the adapter
            # (as_dropbox_callback returns not cancelled()), so a canceled upload raises
            # DropboxCanceled and never touches the last good backup. The context manager
            # closes the dialog even if upload raises.
            with ui.Progress("Uploading to Dropbox...", heading=AddonTitle) as sp:
                dropbox_remote.upload(staged, name, progress=sp.as_dropbox_callback())
        except dropbox_remote.DropboxCanceled:
            dialog.ok(
                AddonTitle, "Backup canceled. Your previous backup was not touched."
            )
            return
        except Exception as e:
            xbmc.log(
                "%s : Dropbox upload failed: %s" % (AddonTitle, type(e).__name__),
                level=xbmc.LOGERROR,
            )
            dialog.ok(
                AddonTitle,
                "Dropbox upload failed. Your previous backup was not touched.",
            )
            return
        # Confirmed landed -> safe to rotate the Dropbox folder, but ONLY when this
        # backup is complete: an incomplete backup never rotates out the last good one.
        if not result.failed:
            _rotate_dropbox(dropbox_remote, protect={name})
        _report_backup_done(result, suffix=" (Dropbox)")
    finally:
        xbmc.executebuiltin("InhibitIdleShutdown(false)")
        try:
            os.remove(translatePath(staged))
        except:
            pass


def _report_backup_done(result, suffix=""):
    """Honest completion dialog: a backup that could not capture something says
    EXACTLY what is missing before claiming success; a clean backup says so plainly."""
    if result.failed:
        dialog.ok(
            AddonTitle,
            "Backup complete%s, but %d item(s) could NOT be captured:\n%s"
            % (suffix, len(result.failed), "\n".join(_failed_lines(result.failed))),
        )
    else:
        dialog.ok(AddonTitle, "Backup complete" + suffix)


def _keep_n():
    try:
        return int(control.setting("backup.keep") or "0")
    except:
        return 0


def _purgeable_destination(backup_zip):
    """True when this backup would be written inside tvOS's purgeable Caches tree.

    On tvOS everything Kodi owns lives under
    ``<container>/Library/Caches/Kodi`` (verified on hardware 2026-07-18). iOS and
    tvOS may evict Library/Caches under storage pressure, and Apple excludes it
    from iCloud and Finder backups - which is precisely the event a backup exists
    to survive. A backup stored there is destroyed by the same purge that destroys
    what it was protecting, and no platform backup covers it either.

    This is a real hazard rather than a theoretical one because the destination is
    browsed: `download.path` defaults to empty, and on tvOS the browse dialog's
    home IS special://home, i.e. inside Caches. Picking the offered default is the
    easiest thing a user can do.

    Path-based and deliberately not tvOS-gated: any destination under the Kodi
    home is self-defeating on every platform (a wipe or reinstall takes it), and
    on tvOS it is additionally purgeable. Network and Dropbox destinations do not
    resolve here and are unaffected."""
    try:
        dest = os.path.normpath(str(backup_zip or "")).replace("\\", "/")
    except Exception:
        return False
    if not dest or "://" in dest:
        return False  # unresolved VFS path (nfs://, smb://) - not a local file
    lower = dest.lower()
    if "/library/caches/" in lower:
        return True
    try:
        home = os.path.normpath(translatePath("special://home/")).replace("\\", "/")
    except Exception:
        return False
    if not home or home in (".", "/"):
        return False
    return lower.startswith(home.rstrip("/").lower() + "/")


def _confirm_destination_survives_a_purge(backup_zip):
    """Warn before writing a backup somewhere it cannot survive. True = proceed.

    Warns rather than refuses: a deliberate scratch backup before a risky change is
    legitimate, and this add-on's contract is truthful reporting, not paternalism.
    But it must never be SILENT - the failure mode is discovering at restore time
    that the backup is gone, which is exactly when it cannot be re-made."""
    try:
        if not _purgeable_destination(backup_zip):
            return True
        return bool(
            ui.confirm(
                "This backup would be saved inside Kodi's own storage.\n\n"
                "Apple TV can clear that area to free space, and a wipe or "
                "reinstall removes it - so this backup may not be there when you "
                "need it. A USB or network location, or Dropbox, is safer.\n\n"
                "Save it here anyway?",
                heading=AddonTitle,
                yeslabel="Save anyway",
                nolabel="Pick another",
            )
        )
    except Exception:
        return True  # never block a backup on a guard failure


def _rotate_vfs(backupdir, protect=None):
    # Keep-N for Local/Network: prune ONLY tool-created ROLLING backups (a trailing
    # _YYYYMMDDHHMM.zip stamp). A user-renamed / protected backup is never counted
    # toward N and never deleted - renaming is how users keep a golden backup.
    n = _keep_n()
    if n <= 0:
        return
    protect = protect or set()
    try:
        _dirs, files = xbmcvfs.listdir(backupdir)
        # Only rolling (stamped) files are prune candidates, so every entry HAS a
        # stamp and the oldest-first sort is fully defined.
        rolling = sorted(
            [
                f
                for f in files
                if f.endswith(".zip") and _is_rolling(f) and f not in protect
            ],
            key=lambda f: (_name_stamp(f), f),
        )
        for old in rolling[: max(0, len(rolling) - n)]:
            try:
                xbmcvfs.delete(translatePath(os.path.join(backupdir, old)))
            except Exception:
                pass
    except Exception as e:
        xbmc.log(
            "%s : backup rotation skipped: %s" % (AddonTitle, type(e).__name__),
            level=xbmc.LOGWARNING,
        )


def _rotate_dropbox(dropbox_remote, protect=None):
    # Keep-N for Dropbox: prune ONLY tool-created ROLLING backups; a user-renamed /
    # protected backup is never counted toward N and never deleted.
    n = _keep_n()
    if n <= 0:
        return
    protect = protect or set()
    try:
        names = dropbox_remote.list_backups()  # newest-first
        rolling = [nm for nm in names if _is_rolling(nm) and nm not in protect]
        for old in rolling[n:]:
            try:
                dropbox_remote.delete(old)
            except Exception:
                pass
    except Exception as e:
        xbmc.log(
            "%s : Dropbox rotation skipped: %s" % (AddonTitle, type(e).__name__),
            level=xbmc.LOGWARNING,
        )


def restoreFolder():
    if _destination() == 2:
        _restore_dropbox()
        return

    names = []
    links = []
    zipFolder = configured_path("restore.path")
    if zipFolder == "":
        control.infoDialog("Please Setup a Zip Files Location first")
        # ON the Backup/Restore tab, where restore.path actually is (see backup()).
        control.openSettingsTab(control.SETTINGS_TAB_BACKUP_RESTORE)
        return
    try:
        _dirs, _files = xbmcvfs.listdir(
            zipFolder
        )  # VFS list works on nfs:// / smb:// too
    except:
        _files = []
    for zipFile in _files:
        if zipFile.endswith(".zip"):
            url = translatePath(os.path.join(zipFolder, zipFile))
            names.append(zipFile)
            links.append(url)
    select = control.selectDialog(names)
    if select != -1:
        # How to restore: a self-describing MENU instead of a yes/no, so the choice is
        # unambiguous. Each line is a full instruction (no "Yes/No" to map onto Wipe/Merge
        # buttons), the safe "add on top" is FIRST and highlighted, and Back/Cancel aborts
        # instead of silently merging. The wipe itself is still deferred to restore(), which
        # ONLY wipes after the chosen zip is staged and validated (a bad zip never wipes).
        how = control.selectDialog(
            [
                "Keep what's on this device and add the backup on top",
                "Erase this device first, then restore a clean copy",
                "Cancel - don't restore anything",
            ],
            heading="Restore backup: how should I do it?",
        )
        # Corroborating hint from the file name (defense-in-depth; the zip's own layout is
        # the authoritative anchor). Maps a "kodi_settings"/userdata name -> userdata anchor.
        # Lazy import breaks the onetap<->wiz cycle (onetap imports wiz inside apply()).
        hint = None
        try:
            from resources.lib.modules import onetap

            hint = {"full": "home", "userdata": "userdata"}.get(
                onetap.infer_type(names[select])
            )
        except Exception:
            pass
        if how == 0:
            restore(links[select], wipe=False, anchor_hint=hint)  # merge
        elif how == 1:
            restore(links[select], wipe=True, anchor_hint=hint)  # clean clone
        # how == 2 (Cancel) or -1 (Back): do nothing.


def _restore_dropbox():
    from resources.lib.modules import dropbox_remote

    if not control.setting("dropbox_refresh_token").strip():
        control.infoDialog("Sign in to Dropbox first (Settings -> Backup/Restore)")
        return
    try:
        names = dropbox_remote.list_backups()
    except Exception as e:
        xbmc.log(
            "%s : Dropbox list failed: %s" % (AddonTitle, type(e).__name__),
            level=xbmc.LOGERROR,
        )
        dialog.ok(AddonTitle, "Could not list Dropbox backups.")
        return
    if not names:
        dialog.ok(AddonTitle, "No backups found in Dropbox.")
        return
    select = control.selectDialog(names)
    if select == -1:
        return
    chosen = names[select]
    local = None
    try:
        # One uniform download gauge. The adapter enables cancel (download honors a
        # False return by dropping the partial and raising DropboxCanceled), where the
        # old hand-rolled callback returned None and the cancel was a silent no-op.
        with ui.Progress("Downloading from Dropbox...", heading=AddonTitle) as dp2:
            special = dropbox_remote.download(
                chosen, progress=dp2.as_dropbox_callback()
            )
        local = translatePath(special)
    except dropbox_remote.DropboxCanceled:
        # User canceled the download - the partial was already removed; nothing changed.
        return
    except Exception as e:
        xbmc.log(
            "%s : Dropbox download failed: %s" % (AddonTitle, type(e).__name__),
            level=xbmc.LOGERROR,
        )
        dialog.ok(AddonTitle, "Could not download that backup from Dropbox.")
        return
    # Hand the LOCAL staged path to restore(): it has no "://", so restore() extracts it
    # directly (the unchanged extractor). restore() removes the temp on the remote branch
    # only, so clean it up here after the user confirms (or declines) the restore.
    try:
        restore(local)
    finally:
        try:
            os.remove(local)
        except:
            pass


# --------------------------------------------------------------------------- #
# The LOCKED restore vocabulary (owner-edited 2026-07-17). These four strings -
# plus the silent boot check - are the ONLY restore messages a user ever sees.
# Counts, paths, and phase vocabulary belong in the log. Do not reword.
# --------------------------------------------------------------------------- #
MSG_COMPLETE = "Restore Complete"
MSG_PROBLEM = (
    "Restore Problem\n"
    "Some of your backup couldn't be restored, so this box may not work "
    "the way it did before."
)
# Names actions the owner can actually take. The old text ("open EZ
# Maintenance++") pointed at a menu whose only relevant entry was "Purge stale
# tvOS keys", removed in 2026.07.19.5 - so it sent her to a screen that no
# longer had the fix on it. This is the SAME sentence service.py's boot check
# already shows for the same condition; both named actions re-run the purge on
# their own. Replaced, not added: the vocabulary is still four strings.
MSG_NEEDS_ATTENTION = (
    "Restore Problem\n"
    "Your restore finished, but one setting may not have applied. "
    "Restore again, or restart the box."
)


def _read_target_skin(guisettings_path):
    """Read lookandfeel.skin from a guisettings.xml on disk.

    Returns the stripped skin id, or None if the file is missing / unparseable / the
    setting is absent or empty (in which case there is no skin to assert). Pure read;
    never raises. Called on the FRESHLY EXTRACTED guisettings.xml, before apply_guisettings
    can rewrite the file (see restore())."""
    try:
        root = ET.parse(guisettings_path).getroot()
    except Exception:
        return None
    for node in root.iter("setting"):
        if node.get("id") == "lookandfeel.skin":
            return (node.text or "").strip() or None
    return None


# Skin setting ids that are safe to interpolate into a Kodi builtin. Kodi's own
# skin setting ids are plain identifiers; anything outside this set cannot be
# emitted safely, because CUtil::SplitParams treats "(" and '"' as structure.
_SAFE_SETTING_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


def _quote_builtin_arg(value):
    """Quote a value for a Kodi builtin parameter (CUtil::SplitParams, Util.cpp:1211).

    SplitParams splits on commas, honors double quotes, and unescapes `\\"` and `\\\\`
    inside them. So the inverse - escape backslash and quote, then wrap in quotes -
    round-trips any value, including one containing commas, quotes or parentheses.

    Without this, a restored value like "Movies, HD" is truncated at the comma and the
    truncated form is what the shutdown flush persists."""
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % escaped


def _read_skin_settings(settings_path):
    """Read a restored addon_data/<skin>/settings.xml into [(id, type, value)].

    Kodi writes this file as `<settings>` with per-setting `type="bool"|"string"`
    (CSkinInfo::SettingsToXML). CSkinInfo::SettingsFromXML ignores unknown types, so
    mirror that and drop anything else. Pure read; never raises; returns [] on a
    missing / unparseable / empty file, which means "nothing to re-apply"."""
    out = []
    try:
        root = ET.parse(settings_path).getroot()
    except Exception:
        return out
    for node in root.iter("setting"):
        sid = (node.get("id") or "").strip()
        stype = (node.get("type") or "").strip()
        if not sid or stype not in ("bool", "string"):
            continue
        if not _SAFE_SETTING_ID.match(sid):
            # The id is interpolated into a Kodi builtin, and CUtil::SplitParams
            # (Util.cpp:1211) suppresses the separating comma inside "(" or '"'.
            # An id carrying either collapses Skin.SetString(id,value) to a SINGLE
            # parameter, which is the one-argument form: it opens a KEYBOARD, and
            # executebuiltin(..., True) BLOCKS, so the restore hangs unrecoverably.
            # This id comes from a restored archive (possibly fetched from Dropbox),
            # so it is untrusted input. Drop it rather than emit an unsafe builtin.
            continue
        # Do NOT strip the value: skin string settings hold user-typed labels, and
        # leading/trailing whitespace is part of the archived value.
        out.append((sid, stype, node.text or ""))
    return out


def _apply_skin_settings(rlog, target_skin, settings):
    """Re-apply restored skin settings IN MEMORY so the shutdown flush cannot undo them.

    This is defect A. Restore writes addon_data/<skin>/settings.xml correctly, then
    ui.restart() -> Quit -> CApplication::Stop() -> g_SkinInfo->SaveSettings()
    serializes the PRE-RESTORE in-memory skin settings back over it
    (Application.cpp:2141, unconditional but for a null check). The restored values are
    destroyed on the way out. NOT tvOS-only: the flush goes through the VFS, so on
    Apple TV it overwrites the NSUserDefaults key too, which is why nsud vectoring
    gives no protection here.

    The documented fix for this class is BOTH mechanisms, reconciled
    (repo/docs/playbooks/kodi-settings-clobber.md): the file write is the extract, and
    this is the in-memory half. Both derive from the same archive content.

    ONE-SHOT, inside the restore. A recurring or per-boot re-assert was REJECTED by
    unanimous adversarial review 2026-07-08 because it fights the user's own later
    edits (repo/docs/plans/atv-every-boot-settings-reassert.md). Do not re-propose it.

    Guarded on the live skin: g_SkinInfo is the CURRENTLY LOADED skin and it flushes to
    its own addon_data dir, so when the restored skin is not the live one the flush
    cannot touch the restored file and the builtins would write into the WRONG skin.

    Never raises. Returns a short status string, also published as a window property so
    the outcome can be read off a live box at all: there is no read-only JSON-RPC way
    to read a skin setting (Skin.HasSetting / GetInfoBooleans MUTATE:
    CSkinInfo::TranslateBool inserts a default-false bool AND schedules a save)."""
    status = "none"
    try:
        if not settings:
            return status
        try:
            live = (xbmc.getSkinDir() or "").strip()
        except Exception:
            live = ""
        if not target_skin or live != target_skin:
            # Do NOT return here: fall through so the diagnostic window property is
            # published on this path too: "why did nothing get applied" is exactly
            # the case worth being able to see from outside.
            status = "skipped:not-live"
            rlog(
                "skin settings: %s (live=%s target=%s)"
                % (status, live or "?", target_skin or "?")
            )
            settings = []
        applied = 0
        for sid, stype, value in settings:
            try:
                if stype == "bool":
                    # TWO-argument form. Skin.SetBool(id) alone can only set true.
                    cmd = "Skin.SetBool(%s,%s)" % (
                        sid,
                        "true" if str(value).lower() == "true" else "false",
                    )
                else:
                    # TWO-argument form is mandatory: Skin.SetString(id) with one
                    # argument opens a KEYBOARD and would hang the restore.
                    #
                    # The value MUST be quoted. CUtil::SplitParams splits on commas,
                    # so an unquoted "Movies, HD" arrives as just "Movies" - silently
                    # TRUNCATED, then set live, then serialized over the correctly
                    # restored file by the shutdown flush. Unquoted, this fix becomes
                    # the very clobber it exists to prevent.
                    cmd = "Skin.SetString(%s,%s)" % (sid, _quote_builtin_arg(value))
                xbmc.executebuiltin(cmd, True)
                applied += 1
            except Exception:
                continue
        if status != "skipped:not-live":
            status = "applied:%d/%d" % (applied, len(settings))
            rlog(
                "skin settings: re-applied %d of %d for %s"
                % (applied, len(settings), target_skin)
            )
    except Exception as e:  # noqa: BLE001 - never break a restore over this
        status = "failed:%s" % type(e).__name__
        try:
            rlog("skin settings: %s" % status)
        except Exception:
            pass
    try:
        xbmcgui.Window(10000).setProperty("ezm_skin_reapply", status)
    except Exception:
        pass
    return status


def _apply_boot_skin(rlog, target):
    """Write the RESTORED skin to disk. No live switch, no keep-skin dialog.

    This does NOT guarantee the reopen boots that skin, and this docstring used to say
    it did. Defect A3, bench-disproved 2026-07-19: Kodi's clean shutdown serializes
    guisettings from LIVE memory over the file afterwards (Application.cpp:2131), so
    when the restored skin differs from the live one this write LOSES and the box
    reopens on the old skin. A negative control (SIGKILL, no flush) kept the written
    value, isolating the cause to the flush.

    It is still the right thing to do - it is the only half available. Kodi offers no
    way to set the skin live without arming the 10-second keep-skin countdown, and any
    non-Yes, INCLUDING a destroyed dialog, reverts (ApplicationSkinHandling.cpp:394-401);
    that is how atv2 was corrupted to stock on 2026-07-17. The mismatch is therefore
    DETECTED at the next boot and reported, not prevented (service._maybe_restore_check).
    The accepted next-cycle fix is to terminate rather than Quit so the flush never runs.

    Note the restored skin's own settings are NOT at risk on this path: the flush writes
    into the LIVE skin's addon_data dir (Application.cpp:2141, Addon.cpp:375-396), so it
    cannot reach the restored skin's file. That is why _apply_skin_settings correctly
    returns skipped:not-live here.

    Why persistence, not a live switch: on the appliances (tvOS AND Fire TV) Kodi's
    "restart" is a FORCE-QUIT (RestartApp is desktop-only; ask_restart only quits), which
    RUNS CApplication::Stop() in full - RestartApp being desktop-only means Quit does not
    RELAUNCH, NOT that it skips the flush. Stop() saves guisettings from live memory
    (Application.cpp:2131) and then flushes the live skin's settings over
    addon_data/<skin>/settings.xml (:2141), through the VFS, so on tvOS it overwrites
    the NSUserDefaults key too. A disk-only write made before the quit is the value that
    LOSES. This docstring previously claimed the opposite and armed defect A; the
    in-memory half now lives in _apply_skin_settings. The reopen boots whatever was LAST
    on disk (and on tvOS, in its NSUserDefaults key). The old mechanism live-set
    lookandfeel.skin and tried to answer Kodi's "keep this skin?" dialog via SendClick /
    navigation; that was flaky on the Apple TV (worked 3 of 4 hardware runs) and, when the
    confirm missed, Kodi REVERTED and corrupted the skin to stock in memory and on disk
    (atv2, 2026-07-17). A write that never touches the live skin can never raise that dialog.

    `target` is the skin captured from the EXTRACTED guisettings.xml BEFORE apply_guisettings
    ran: apply_guisettings' live SetSettingValue calls make Kodi save guisettings.xml,
    stamping the current in-memory (stock) skin over the archive's value. This runs as the
    restore's LAST userdata write (after apply_guisettings, the purge, and the tvOS
    re-vector), so nothing re-saves over it.

    Writes the value two ways so it survives every platform: (a) into guisettings.xml on disk
    via _kodisettings.write_guisetting (the durable path on Fire TV / desktop), and (b)
    vectored into NSUserDefaults via nsud.persist_one (the durable path on tvOS; a no-op
    rewrite of identical bytes elsewhere). Publishes Window(10000) property "ezm_boot_skin"
    (written:<skin> / unconfirmed:<skin> / none / failed:<Error>) for off-box inspection
    over JSON-RPC. Fully guarded: any failure is logged and NEVER breaks the restore.

    RETURNS that same status string, so the caller can weigh it in the restore verdict.
    "unconfirmed:<skin>" is the one the caller must act on: tvOS wrote the skin to disk
    but the read-back did not prove NSUserDefaults holds it, and on tvOS a stale key
    SHADOWS the disk file - the box would reopen on the PREVIOUS skin. Reporting that as
    Complete is exactly the false success the locked contract forbids."""
    prop = "none"
    try:
        target = (target or "").strip()
        if not target:
            rlog("boot-skin: no restored skin to assert; nothing to do")
            return prop
        from resources.lib.modules import _kodisettings, nsud

        guisettings_path = os.path.join(control.USERDATA, "guisettings.xml")
        # On tvOS the durability re-vector above (nsud.rewrite_userdata_xml) vectored
        # guisettings.xml into NSUserDefaults and DROPPED the redundant POSIX copy, so
        # there is no file left for write_guisetting to edit or for persist_one to read
        # back. Re-materialize the FULL current content from the VFS (the NSUD key on
        # tvOS) first - never a stub, which would wipe every OTHER setting when
        # persist_one re-vectors the file. A no-op on every platform that kept the file.
        if not os.path.exists(guisettings_path):
            try:
                f = xbmcvfs.File("special://home/userdata/guisettings.xml")
                try:
                    data = f.readBytes()
                finally:
                    f.close()
                data = bytes(data) if data else b""
                if data:
                    with open(guisettings_path, "wb") as fh:
                        fh.write(data)
                    rlog("boot-skin: re-materialized guisettings.xml from the VFS")
            except Exception as e:  # noqa: BLE001 - fall through; write_guisetting reports
                rlog("boot-skin: could not re-materialize guisettings.xml (%s)" % e)
        ok = _kodisettings.write_guisetting(
            guisettings_path, "lookandfeel.skin", target
        )
        # Vector guisettings.xml into NSUserDefaults on tvOS (the durable store there); a
        # harmless no-op rewrite of identical bytes on Fire TV / Android / desktop.
        vectored = nsud.persist_one("guisettings.xml", log=rlog)
        if not ok:
            prop = "failed:write_guisetting"
            rlog("boot-skin: write_guisetting could not persist %s" % target)
        elif not vectored and _source_os() == "tvos":
            # tvOS ONLY. persist_one kept the POSIX copy (nothing is lost) but could not
            # prove NSUserDefaults holds the same bytes - and on tvOS a key SHADOWS the
            # disk file, so the reopen this restore asks the user to perform would come
            # back on the OLD skin. Off tvOS there is no shadowing layer, so a False here
            # is not this failure mode and never becomes a warning.
            prop = "unconfirmed:%s" % target
            rlog(
                "boot-skin: %s written to disk but the tvOS vector was NOT confirmed; "
                "a stale NSUserDefaults key may shadow it" % target
            )
        else:
            prop = "written:%s" % target
            # "written", NOT "persisted": on a skin-CHANGING restore the shutdown
            # flush overwrites this from live memory (A3), so claiming persistence
            # here would assert a success the code cannot deliver - the same pattern
            # that armed defect A. The boot check reports the actual outcome.
            rlog(
                "boot-skin: wrote restored skin %s to disk (no live switch); the "
                "shutdown flush decides what actually boots - verified at next start"
                % target
            )
    except Exception as e:
        prop = "failed:%s" % type(e).__name__
        rlog("boot-skin: failed (%s: %s); restore stands" % (type(e).__name__, e))
    finally:
        try:
            xbmcgui.Window(10000).setProperty("ezm_boot_skin", prop)
        except Exception:
            pass
    return prop


def _preserve_device_settings(rlog, preserved):
    """Settle THIS box's hardware-scoped settings over the archive's - one PRESERVED,
    one RESET.

    The other half of `_kodisettings._BOOT_STATE_ONLY`. Skipping the live-apply keeps
    the archive's device name and cache buffer out of Kodi's running memory; this
    decides what stands in their place. Without both halves the archive's values sit in
    the restored guisettings.xml and win at the next boot - the box silently answers to
    the source box's name again, one restart after the restore reported success.

    ONE shared principle, TWO DIFFERENT resolutions. Collapsing them back into a single
    rule is the regression this docstring exists to prevent:

      * `services.devicename` is PRESERVED. `preserved` is {setting_id: value}, captured
        LIVE before the extract (tools.capture_device_identity), so the box keeps the
        name it answers to on the network. A value that could not be read is absent, and
        an absent value writes nothing: the archive's name is a worse answer than this
        box's own, but it beats a guess.
      * `filecache.memorysize` is RESET to `tools.KODI_DEFAULT_MB`, never preserved and
        never cloned. The fleet mixes device classes whose right buffer differs, so no
        inherited number is allowed to survive a restore - not the archive's, and not
        this box's own previous one either (owner decision 2026-07-31). The per-device
        recommendation is offered on demand in the add-on's Video Cache Buffer menu
        (tools.advancedSettings); the buffer only moves off Kodi's default when the user
        accepts it or types his own. There is deliberately NO prompt: the post-restore
        popup was deleted 2026-07-19 and does not come back for this.

    The reset depends on nothing captured, so it runs even when the capture came back
    EMPTY - which is why this function no longer returns early on an empty `preserved`.

    Runs immediately before _apply_boot_skin, for the same reasons that function runs
    last: after apply_guisettings, the stale-key purge and the tvOS re-vector, so
    nothing re-saves over it. Same tvOS dance too - the re-vector drops the POSIX copy,
    so the file is re-materialized from the VFS (never a stub, which would wipe every
    other setting) before editing, and ONE persist_one at the end vectors the result
    back into NSUserDefaults, where a stale key would otherwise SHADOW the disk file.
    That single vector is why the reset is asked not to vector for itself.

    Publishes Window(10000) property "ezm_preserved" (a comma-joined id:value list, or
    "none" / "failed:<Error>") so a hardware verification can read the outcome over
    JSON-RPC rather than infer it. Returns that same string. Fully guarded: any failure
    is logged and NEVER breaks the restore."""
    prop = "none"
    try:
        from resources.lib.modules import _kodisettings, nsud

        guisettings_path = os.path.join(control.USERDATA, "guisettings.xml")
        if not os.path.exists(guisettings_path):
            try:
                f = xbmcvfs.File("special://home/userdata/guisettings.xml")
                try:
                    data = f.readBytes()
                finally:
                    f.close()
                data = bytes(data) if data else b""
                if data:
                    with open(guisettings_path, "wb") as fh:
                        fh.write(data)
                    rlog("preserve: re-materialized guisettings.xml from the VFS")
            except Exception as e:  # noqa: BLE001 - fall through; the writes report
                rlog("preserve: could not re-materialize guisettings.xml (%s)" % e)
        written = []
        failed = []
        for sid in sorted(preserved or {}):
            if sid == tools.CACHE_SETTING:
                # The buffer is RESET below, never written back. Skipping it here means
                # a capture that still carries it - an older caller, a hand-built dict -
                # cannot smuggle an inherited number past the reset.
                continue
            value = preserved[sid]
            if _kodisettings.write_guisetting(guisettings_path, sid, value):
                written.append("%s:%s" % (sid, value))
            else:
                failed.append(sid)
        # RESET, not preserve. vector=False because the single persist_one below covers
        # every write in this function; a vector taken inside the reset would, on tvOS,
        # drop the POSIX copy out from under the device-name write above.
        reset = tools.reset_cache_buffer(guisettings_path, vector=False, log=rlog)
        if reset.get("file"):
            written.append("%s:%d" % (tools.CACHE_SETTING, tools.KODI_DEFAULT_MB))
        else:
            failed.append(tools.CACHE_SETTING)
        # Vector guisettings.xml into NSUserDefaults on tvOS (the durable store there);
        # a harmless no-op rewrite of identical bytes on Fire TV / Android / desktop.
        nsud.persist_one("guisettings.xml", log=rlog)
        if failed:
            prop = "failed:write_guisetting"
            rlog("preserve: could not write %s" % ", ".join(failed))
        elif written:
            prop = ",".join(written)
            rlog("preserve: settled %s" % ", ".join(written))
    except Exception as e:
        prop = "failed:%s" % type(e).__name__
        rlog("preserve: failed (%s: %s); restore stands" % (type(e).__name__, e))
    finally:
        try:
            xbmcgui.Window(10000).setProperty("ezm_preserved", prop)
        except Exception:
            pass
    return prop


def restore(
    zipFile,
    confirm=True,
    post_wipe=False,
    wipe=False,
    anchor_hint=None,
    wipe_leftovers=None,
):
    """Extract a backup zip over special://home and offer a restart.

    wipe_leftovers: named ("file"|"key", home-rel) leftovers from a wipe the CALLER
    already ran (the One-Tap path). They are triaged by the post-restore verification,
    never surfaced raw.

    post_wipe=True is the One-Tap path: the box has ALREADY been wiped and the snapshot
    ALREADY fully validated by the caller before the wipe. In that mode the extract is a
    single UNINTERRUPTIBLE unit (no cancel) and we NEVER early-return - a wiped box must
    always be driven to the restart prompt, never left silent on a partial/empty extract.

    wipe=True is the normal-restore "clean clone" path (chosen in restoreFolder()): this
    function does the wipe ITSELF, but ONLY under the mandatory safe ordering - the zip is
    staged locally AND validated (size>0 + a real zip) FIRST; only THEN is the box wiped;
    then the extract runs as the same UNINTERRUPTIBLE unit as post_wipe. If the zip is
    missing/short/corrupt the function ABORTS with the box UNTOUCHED (it never wipes on a
    bad zip). The wipe reuses the PROVEN One-Tap wipe (onetap._wipe / _wipe_excludes),
    which preserves this add-on, its runtime deps, and special://temp (the staged zip).
    """
    # Capture THIS box's own identity settings FIRST, before anything can overwrite
    # them: the device name it answers to on the network. A restore clones the SOURCE
    # box's guisettings, so without this the box comes back wearing another box's name.
    # Read from Kodi's LIVE settings, which is why it is still correct on the One-Tap
    # path where the CALLER already wiped the box before calling us - a wipe removes
    # files, not the running process's memory. This is the first statement in the
    # function so no later edit can slip a wipe or an extract in front of it. Written
    # back by _preserve_device_settings after the extract.
    #
    # The cache buffer is NOT captured: it is RESET to Kodi's default rather than
    # preserved (see _preserve_device_settings), so there is nothing here to keep.
    preserved = {}
    try:
        preserved = tools.capture_device_identity()
    except Exception:
        preserved = {}

    if confirm and not post_wipe:
        if not ui.confirm(
            "This will overwrite all your current settings ... Are you sure?",
            heading=AddonTitle,
            yeslabel="Yes",
            nolabel="No",
        ):
            return

    # Stage a remote (VFS) zip locally first; a plain local path extracts directly. The
    # staging copy now has a gauge + cancel (it used to be a silent xbmcvfs.copy).
    local = zipFile
    staged = False
    if "://" in zipFile:
        local = translatePath(os.path.join("special://temp", os.path.basename(zipFile)))
        try:
            with ui.Progress("Preparing the backup...", heading=AddonTitle) as sp:
                if (
                    ui.copy_with_progress(zipFile, local, progress=sp)
                    == ui.COPY_CANCELLED
                ):
                    try:
                        os.remove(local)
                    except OSError:
                        pass
                    return
            staged = True
        except Exception:
            # Staging failed; the validation below reports the missing/short zip.
            pass

    # Validate BEFORE extracting so we never report success on a missing / empty / bad
    # zip. After a wipe the snapshot was already validated by the caller BEFORE the box was
    # touched, so post_wipe does NOT early-return here (that would strand a wiped box).
    # NOTE: wipe=True still validates here (it is NOT yet post_wipe), because on the
    # clean-clone path THIS is the validation that MUST pass before we are allowed to wipe.
    try:
        size = os.path.getsize(local)
    except OSError:
        size = 0
    if not post_wipe and (size == 0 or not zipfile.is_zipfile(local)):
        if staged:
            try:
                os.remove(local)
            except OSError:
                pass
        dialog.ok(
            AddonTitle,
            "Restore failed: the backup is missing or not a valid zip (%d bytes). "
            "Nothing was changed." % size,
        )
        return

    # Clean-clone path: the zip is now staged + validated, so it is finally safe to wipe.
    # Ensure the validated zip lives in special://temp (which the wipe preserves) so the
    # source survives the wipe, wipe with the PROVEN One-Tap wipe, then flip to post_wipe
    # semantics (uninterruptible extract that always reaches the restart prompt).
    if wipe and not post_wipe:
        try:
            temp_dir = translatePath("special://temp")
            os.makedirs(temp_dir, exist_ok=True)
            if os.path.dirname(os.path.abspath(local)) != os.path.abspath(temp_dir):
                import shutil

                staged_local = os.path.join(temp_dir, os.path.basename(local))
                if os.path.abspath(staged_local) != os.path.abspath(local):
                    shutil.copyfile(local, staged_local)
                local = staged_local
                staged = True
        except Exception:
            # Could not park the validated zip somewhere the wipe preserves; ABORT
            # rather than risk wiping the box out from under its own restore source.
            if staged:
                try:
                    os.remove(local)
                except OSError:
                    pass
            dialog.ok(
                AddonTitle,
                "Restore failed: could not stage the backup for a clean-clone restore. "
                "Nothing was changed.",
            )
            return
        # From here on this IS a post-wipe restore: uninterruptible, never early-returns.
        # (The wipe itself runs inside the attempt loop below, so Try Again gets a FRESH
        # wipe - including a fresh shot at anything the first wipe could not remove.)
        _needs_own_wipe = True
        post_wipe = True
    else:
        _needs_own_wipe = False

    def _rlog(m):
        xbmc.log("%s : %s" % (AddonTitle, m), xbmc.LOGINFO)

    # Both wiped flows (One-Tap already-wiped, clean-clone about-to-wipe) share the
    # uninterruptible contract AND the retry rule: a Try Again re-wipes fresh.
    wiped_flow = bool(post_wipe)
    leftovers = list(wipe_leftovers or [])

    def _wipe_pass():
        """The proven One-Tap wipe, with the same count-only progress rules as before
        (a per-file name would re-trigger the text renderer crash). Returns the NAMED
        leftovers for triage; never raises."""
        # Lazy import breaks the onetap<->wiz cycle: onetap imports wiz lazily (inside
        # apply()), so importing onetap at call time - not at module top - is the
        # lowest-risk way to reuse its proven wipe without an import loop.
        from resources.lib.modules import onetap

        with ui.Progress("Wiping the device clean...", heading="Restoring") as wp:
            # wipe_excludes_keeping_databases(), NOT _wipe_excludes(): unlike Fresh
            # Start, restore keeps Kodi ALIVE for the whole extract that follows, so
            # it must not unlink a database Kodi holds open (that is the crash in
            # maintenance._purge_texture_cache, and this path armed it for minutes).
            # Nothing is lost - the archive re-supplies the databases. keep_addon_db()
            # was also MISSING here while Fresh Start passed it, so this path unlinked
            # Addons*.db too, the one database whose loss brings EZ Maintenance++ back
            # DISABLED. Kept as belt and braces if the exclude set ever changes.
            _wres = onetap._wipe(
                translatePath("special://home/"),
                onetap._wipe_excludes(),
                onetap.keep_addon_db() | onetap.keep_live_databases(),
                progress=lambda done, total: wp.items(
                    done, total, note="Removing old files"
                ),
            )
        try:
            return list(_wres[3])
        except (TypeError, IndexError):
            return []

    # A backup zip is either HOME-anchored (full: members under userdata/ + addons/) or
    # USERDATA-anchored (a "kodi_settings" backup: bare userdata contents, NO userdata/
    # prefix). Extract to the MATCHING root - extracting a userdata-anchored zip to HOME is
    # exactly the bug that scattered settings into the home root and bricked the box.
    try:
        _names = zipfile.ZipFile(local).namelist()
    except Exception:
        _names = []
    items = len(_names)
    manifest = _read_manifest(local)  # None on older, pre-manifest backups
    anchor = _archive_anchor(_names, hint=anchor_hint)
    extract_root = control.HOME if anchor == "home" else control.USERDATA
    _rlog(
        "restore: anchor=%s -> extract_root=%s (%d members, manifest=%s)"
        % (anchor, extract_root, items, "yes" if manifest is not None else "no")
    )

    iptv_prefixes = _iptv_profile_prefixes(_names, anchor)

    # Skip the temp/ self-reference, recomputed against the ACTUAL extract root. On a
    # userdata anchor relpath(home/temp, home/userdata) -> '../temp' -> stays None (a
    # userdata zip has no home temp tree), which is correct.
    skip_prefix = None
    try:
        rel = os.path.relpath(translatePath("special://temp"), extract_root)
        if not rel.startswith(".."):
            skip_prefix = rel.replace("\\", "/").rstrip("/") + "/"
    except Exception:
        pass
    _skip_fn = _extract_skip(anchor, skip_prefix)

    # The Kodi-version gate (versiongate.py, self-contained by design): an
    # archive whose addons/ tree was built on a DIFFERENT Kodi major - or one
    # too old to say which (every backup made before the manifest carried
    # kodi_version) - must not lay its add-ons and skins over this install.
    # Userdata still restores in full; add-ons re-download from their
    # repositories. Decided ONCE, here, outside _restore_pass, so the
    # explanation dialog cannot fire a second time when the attempt loop runs
    # a retry pass; spoken BEFORE any wipe or extract, so the user hears what
    # the restore will not bring back while the box is still untouched.
    _gate = versiongate.evaluate(manifest, get_Kodi_Version(), _names)
    if _gate.blocked:
        _rlog("restore: %s" % _gate.log_line)
        _skip_fn = versiongate.wrap_skip(_skip_fn)
        dialog.ok(AddonTitle, _gate.message)
    # What the extract will actually lay down, for the post-restore triage:
    # with the gate up, an addons/ wipe leftover must triage as residue, not
    # as "overwritten by the archive" - the archive's copy never landed.
    _restored_names = (
        [n for n in _names if not versiongate.is_addons_member(n)]
        if _gate.blocked
        else _names
    )

    def _restore_pass(pass_leftovers):
        """One full extract -> apply -> verify pass.

        Returns (hard, attention, canceled):
          hard      - content from the backup did not land (extract failures, unmapped
                      members, manifest gaps). The backup is not fully on the box.
          attention - everything landed but the restored STATE may not stick or may be
                      shadowed (sweep/rewrite/apply failures, surviving stale keys,
                      two-layer duplicates). A fresh pass usually clears these.
          canceled  - the user canceled a merge-path extract; nothing to report.
        All detail goes to the LOG; the caller decides which locked message to show.
        """
        # Restore-scoped PVR pause. pvr.iptvsimple reads instance-settings-*.xml only at
        # client start and FLUSHES its stale in-memory copy over them at teardown
        # (hardware-proven: docs/playbooks/kodi-settings-clobber.md) - so extracting IPTV
        # config under a LIVE client is undone at the next clean shutdown. When (and only
        # when) the archive carries pvr.iptvsimple config and the client is enabled, it is
        # disabled here and ALWAYS re-enabled after the durability rewrite (both the cancel
        # path and the completion path re-enable; a re-enable failure is reported loudly).
        # This is the single sanctioned exception to "restore never toggles add-ons": a
        # bounded, restore-scoped pause - never boot-time automation, never install/stage.
        attention = []
        pvr_paused = False
        if iptv_prefixes:
            if _pvr_enabled():
                pvr_paused = _pvr_set_enabled(False)
                if pvr_paused:
                    # Record the outstanding pause BEFORE the extract, so if this
                    # restore is interrupted (crash, power loss) the boot service
                    # re-enables the client instead of leaving it disabled forever.
                    try:
                        tools.mark_pvr_paused()
                    except Exception:
                        pass
                    _rlog(
                        "restore: paused %s for the IPTV restore window" % _PVR_ADDON_ID
                    )
                else:
                    # A pause miss is a RISK ("its shutdown MAY overwrite..."), not a
                    # realized failure. If it mattered, it shows up as a functional
                    # problem later (the client re-enable, or a duplicate/stray
                    # instance) - which stay attention. Alone, it is log-only, so it
                    # cannot cry wolf on a restore that landed fine.
                    _rlog(
                        "restore: could not pause %s (risk-only; functional checks "
                        "below decide)" % _PVR_ADDON_ID
                    )

        # Post-wipe the extract is uninterruptible: a cancel here would strand the wiped
        # box, so cancelable is off and we ALWAYS fall through to the reporting below.
        result = ExtractResult(canceled=False, extracted=-1)
        with ui.Progress("Extracting the backup...", heading="Restoring") as p:
            result = _as_extract_result(
                ExtractWithProgress(
                    local,
                    extract_root,
                    p,
                    skip_prefix=skip_prefix,
                    cancelable=not post_wipe,
                    skip_member=_skip_fn,
                )
            )

        if result.canceled and not post_wipe:
            # The user canceled a (non-wipe) restore. The stray sweep runs only AFTER a
            # completed extract, so the box's own IPTV config was NOT touched; the only
            # residue is whatever members the (now-stopped) extract already overwrote.
            if pvr_paused:
                if _pvr_set_enabled(True):
                    _clear_pvr_pause_marker()
                else:
                    dialog.ok(
                        AddonTitle,
                        "The IPTV client could not be re-enabled automatically. "
                        "It will be re-enabled on the next restart.",
                    )
            dialog.ok(
                AddonTitle,
                "Restore Canceled. Files extracted before the cancel remain on disk; "
                "your IPTV configuration was not swept.",
            )
            return ([], [], True)

        # IPTV duplicate-instance STRAY sweep (AFTER the extract, deliberately): the
        # archive's own instance files were just written by the extract; this removes only
        # instance-settings-*.xml the archive does NOT carry (both layers on tvOS), so the
        # restored instance set exactly equals the archive and numbering can never
        # accumulate (the 2026-07-08 brick guard). Running it post-extract means a cancel
        # can never destroy config the box already had. No-op without IPTV in the archive.
        swept, sweep_failed = _sweep_iptv_instances(_names, anchor, log=_rlog)

        # Capture the RESTORED skin from the freshly extracted guisettings.xml NOW, before
        # apply_guisettings' live SetSettingValue calls make Kodi save the file and stamp
        # the current (stock) in-memory skin over the archive's value. The captured skin is
        # persisted as the restore's LAST userdata write (see _apply_boot_skin), so the
        # post-restore force-quit reopens on the restored skin with no keep-skin dialog ever
        # appearing. A missing file / absent / empty setting -> None -> nothing to assert.
        try:
            _boot_skin["target"] = _read_target_skin(
                os.path.join(control.USERDATA, "guisettings.xml")
            )
        except Exception:
            _boot_skin["target"] = None

        # Capture the restored SKIN SETTINGS here too, for the same reason and one
        # more: on tvOS nsud.rewrite_userdata_xml vectors this file into the
        # NSUserDefaults key and DROPS the POSIX copy, so a later plain read would
        # find nothing. Read once, now, while the extracted file is still on disk.
        # Applied late (see _apply_skin_settings) so the in-memory state is correct
        # for the shutdown flush that would otherwise destroy it. Defect A.
        try:
            _skin = _boot_skin.get("target")
            _boot_skin["settings"] = (
                _read_skin_settings(
                    os.path.join(control.USERDATA, "addon_data", _skin, "settings.xml")
                )
                if _skin
                else []
            )
        except Exception:
            _boot_skin["settings"] = []

        # HARD failures: content from the backup that is not on the box. These are the
        # only findings allowed to call a restore a problem to the user.
        hard = []
        if result.failed:
            hard.append("%d item(s) did NOT restore" % len(result.failed))
            for line in _failed_lines(result.failed):
                _rlog("failed member: %s" % line)

        # Members the anchor rules refused to map are surfaced, never silently dropped:
        # on a HOME anchor a member outside the allowed top-level dirs is real content
        # from a foreign/legacy layout the restore cannot place - the owner must know it
        # did not land (dropping it silently while saying "Complete" is the old lie).
        def _skipped_on_purpose(name):
            """True for members _skip_fn drops as POLICY rather than as unplaceable
            content. These must not reach `unmapped`: reporting a deliberate skip as
            "did NOT restore" turns a clean restore into a hard failure on screen, which
            is the same cry-wolf lie in the opposite direction."""
            rel = (name or "").lstrip("/").replace("\\", "/")
            if rel == MANIFEST_NAME:
                return True
            if skip_prefix and rel.startswith(skip_prefix):
                return True
            # EZM's own settings.xml and its own add-on tree: never restored BY DESIGN
            # (see _is_secret_arc and _SELF_ADDON_PREFIX). Their absence is the fix
            # working, not content the restore failed to place.
            if _is_secret_arc(rel) or rel.startswith(_SELF_ADDON_PREFIX):
                return True
            # Members the Kodi-version gate withheld: policy, decided up front
            # and announced in its own dialog - not content that failed to land.
            if _gate.blocked and versiongate.is_addons_member(rel):
                return True
            return False

        unmapped = [n for n in _names if _skip_fn(n) and not _skipped_on_purpose(n)]
        if unmapped:
            hard.append(
                "%d member(s) outside the recognized layout were NOT restored: %s"
                % (len(unmapped), ", ".join(unmapped[:5]))
            )
        if manifest is not None:
            hard.extend(_manifest_problems(manifest, _names))
        if sweep_failed:
            attention.append(
                "%d stale IPTV instance file(s) could not be removed: %s"
                % (len(sweep_failed), ", ".join(sweep_failed[:5]))
            )

        # Make the restore actually take effect on every platform - critically on tvOS,
        # where Kodi mirrors guisettings.xml in NSUserDefaults and would otherwise revert
        # a file-only restore. Mirror the official Backup add-on: re-apply settings
        # through the JSON-RPC API and rescan add-ons. The skin is deliberately NOT
        # live-applied here (see _apply_boot_skin below). A failure is never swallowed
        # silently: it is logged AND weighed in the verdict.
        applied = 0
        try:
            from resources.lib.modules import _kodisettings

            applied = _kodisettings.apply_guisettings(
                os.path.join(control.USERDATA, "guisettings.xml")
            )
        except Exception as e:
            xbmc.log(
                "%s : apply_guisettings failed: %s: %s"
                % (AddonTitle, type(e).__name__, e),
                level=xbmc.LOGERROR,
            )
            attention.append("settings re-apply failed (%s)" % type(e).__name__)
        try:
            xbmc.executebuiltin("UpdateLocalAddons")
        except Exception as e:
            xbmc.log(
                "%s : UpdateLocalAddons failed: %s" % (AddonTitle, type(e).__name__),
                level=xbmc.LOGWARNING,
            )

        # tvOS durability: the extract wrote userdata/*.xml with plain POSIX I/O, which on
        # Apple TV BYPASSES Kodi's CTVOSFile VFS - so the restored settings never reach
        # NSUserDefaults (tvOS's only persistent store) and are shadowed by the stale
        # mirror at boot. Re-write each restored userdata/*.xml THROUGH xbmcvfs so tvOS
        # vectors it into NSUserDefaults (durable on the first reopen, no clean shutdown
        # needed). Runs AFTER apply_guisettings/UpdateLocalAddons (so nothing re-saves
        # defaults over it). A failure is logged AND weighed, never silently swallowed.
        try:
            from resources.lib.modules import nsud

            # Purge FIRST (before the rewrite): out-of-scope vector-everything-era keys
            # shadow restored files and inflate the NSUserDefaults store; clearing them
            # before vectoring the restored xml keeps the store's peak size down (the
            # 512 KB warn / 1 MB kill budget) and cannot touch in-scope keys the rewrite
            # is about to write. This purge is also the AUTO-FIX for wipe-leftover keys:
            # the verification below re-reads the store and only a key that survives
            # BOTH the wipe and this purge can ever reach the user as a problem.
            if hasattr(nsud, "purge_stale_keys"):
                try:
                    pg = nsud.purge_stale_keys(control.USERDATA, log=_rlog)
                    try:
                        pg_failed = int(pg[3])
                    except (TypeError, IndexError, ValueError):
                        pg_failed = 0
                    if pg_failed:
                        xbmc.log(
                            "%s : stale-key purge left %d pre-existing key(s) "
                            "unresolved (weighed by the verification below)"
                            % (AddonTitle, pg_failed),
                            level=xbmc.LOGINFO,
                        )
                except Exception as e:
                    # purge_stale_keys is documented never to raise; guard anyway.
                    xbmc.log(
                        "%s : purge_stale_keys raised %s: %s (verification decides)"
                        % (AddonTitle, type(e).__name__, e),
                        level=xbmc.LOGWARNING,
                    )

            rw = nsud.rewrite_userdata_xml(control.USERDATA, log=_rlog)
            try:
                rw_failed = int(rw[2])
            except (TypeError, IndexError, ValueError):
                rw_failed = 0
            if rw_failed:
                # A tvOS re-vector miss means a restored userdata file did not reach
                # the NSUserDefaults layer. Whether that is HARMLESS or a SILENT LOSS
                # depends on the path (audit Finding A/B, 2026-07-17):
                #   WIPE path (One-Tap / "Erase first"): the wipe already cleared every
                #     NSUD key, so no stale key can shadow the restored POSIX file - Kodi
                #     serves the restored file. Harmless -> LOG only. (Alarming here is
                #     what cried wolf on atv2's clean restores.)
                #   MERGE path (add-on-top / all Dropbox restores): pre-existing keys
                #     survive (purge keeps in-scope keys), so a re-vector miss leaves a
                #     STALE key shadowing the restored file - Kodi serves the OLD value
                #     forever and the user would never know. That is a real partial loss
                #     and MUST be surfaced (the shadow probe only covers three addon_data
                #     dirs, so it cannot catch a shadowed top-level userdata file).
                if wiped_flow:
                    _rlog(
                        "tvOS re-vector missed %d file(s) on the wipe path (keys were "
                        "cleared; restored files are served - harmless)" % rw_failed
                    )
                else:
                    attention.append(
                        "%d restored setting file(s) did not persist to tvOS storage; "
                        "an older copy may shadow them (merge restore)" % rw_failed
                    )
        except Exception as e:
            xbmc.log(
                "%s : tvOS settings rewrite failed: %s: %s"
                % (AddonTitle, type(e).__name__, e),
                level=xbmc.LOGERROR,
            )

        # ALWAYS resume the paused IPTV client - the pause is restore-scoped by contract.
        # The client starts fresh and reads the restored (and on tvOS, vectored) instance
        # files. A re-enable failure is loud: it is logged and weighed in the verdict.
        if pvr_paused:
            if _pvr_set_enabled(True):
                _clear_pvr_pause_marker()
                _rlog("restore: resumed %s" % _PVR_ADDON_ID)
            else:
                attention.append(
                    "the IPTV client could not be re-enabled now - it will be "
                    "re-enabled automatically on the next restart"
                )

        # The verification stage: prove the restored state before anyone reports it.
        # Triage of the wipe leftovers + the two-layer probes (restorecheck.py). Only
        # proven-dangerous findings survive into `attention`.
        with ui.Progress("Verifying backup...", heading="Restoring"):
            try:
                from resources.lib.modules import restorecheck

                ver_attention, _detail = restorecheck.verify_restored_state(
                    pass_leftovers, _restored_names, anchor
                )
            except Exception as e:
                # verify_restored_state is documented never to raise; guard the IMPORT
                # too so a broken deploy missing the module cannot strand a wiped box
                # before the restart prompt (the uninterruptible invariant).
                _rlog(
                    "verification unavailable (%s: %s); reporting on extract/sweep only"
                    % (type(e).__name__, e)
                )
                ver_attention = []
        attention.extend(ver_attention)

        # The restored skin is persisted LAST (after apply_guisettings, the stale-key
        # purge, the tvOS re-vector, and the verification) so nothing re-saves over it.
        # A pure write, never a live switch.
        #
        # This runs INSIDE the pass - not after the attempt loop - for two reasons that
        # are both load-bearing:
        #   1. its result reaches the verdict, so an unconfirmed tvOS vector can no
        #      longer be reported as "Restore Complete" while the box quietly reopens
        #      on the previous skin (a partial restore must never claim success), and
        #   2. it therefore gets the SAME auto-fix retry every other attention-only
        #      finding gets. _apply_boot_skin is idempotent, so a one-off flaky
        #      read-back self-heals on the silent second pass and NEVER reaches the
        #      user. Only a failure that survives both passes is warned about.
        # Defect A: re-apply the restored skin settings IN MEMORY, immediately before
        # the boot-skin write and therefore before ui.restart(). The shutdown flush
        # serializes whatever is in memory over addon_data/<skin>/settings.xml, so the
        # extract alone loses them; this makes memory agree with the archive first.
        _apply_skin_settings(
            _rlog, _boot_skin.get("target"), _boot_skin.get("settings") or []
        )
        # Put this box's OWN device name back over the archive's, and reset the cache
        # buffer to Kodi's default, now that nothing further will re-save
        # guisettings.xml. Runs inside the pass so it gets the same auto-fix retry every
        # other step gets, and before the boot-skin write so the skin remains the
        # restore's last userdata write.
        _preserve_device_settings(_rlog, preserved)
        boot_skin = _apply_boot_skin(_rlog, _boot_skin.get("target"))
        if str(boot_skin or "").startswith("unconfirmed:"):
            attention.append(
                "the restored skin may not have been saved - this box may open "
                "with the previous skin"
            )

        extracted_n = result.extracted if result.extracted >= 0 else items
        total_n = result.total if result.total else items
        _rlog(
            "restore pass: %d/%d extracted, %d settings applied, %d swept, "
            "hard=%r attention=%r"
            % (extracted_n, total_n, applied, swept, hard, attention)
        )
        _stats.clear()
        _stats.update(
            extracted=extracted_n,
            total=total_n,
            failed=len(result.failed or []),
            unmapped=len(unmapped),
            skipped=len(unmapped),
            applied=applied,
        )
        return (hard, attention, False)

    # The structured, machine-facing summary of the LAST pass: the honest record
    # for callers and tests (the UI speaks only the locked vocabulary; this dict
    # carries the counts the dialogs deliberately do not).
    _stats = {}

    # The RESTORED skin captured from the extracted guisettings.xml, BEFORE
    # apply_guisettings can rewrite the file (populated inside _restore_pass; persisted
    # by _apply_boot_skin as the last userdata write). None -> no skin to assert.
    _boot_skin = {}

    # ---- attempt loop: at most 2 passes. A hard failure asks the user (Try Again);
    # attention-only failures are auto-fixed with a silent fresh pass ("when this
    # occurs, we should just fix it" - owner, 2026-07-17). ----
    def _guard(fn, what):
        # A raise from a wipe/extract pass (e.g. a ui.Progress construction or a lazy
        # import) must still drive a WIPED box to the restart prompt - a wiped box may
        # never be stranded with no way forward (audit Finding D). Re-raises after so
        # the error is not swallowed silently.
        try:
            return fn()
        except Exception as e:
            _rlog("restore: %s raised %s" % (what, e))
            if wiped_flow:
                try:
                    ui.ask_restart("")
                except Exception:
                    pass
            raise

    if _needs_own_wipe:
        leftovers = _guard(_wipe_pass, "wipe")
    hard, attention, canceled = _guard(lambda: _restore_pass(leftovers), "restore pass")
    # A pass that did not cancel actually laid a full restore on the box (extract +
    # apply + rewrite). Track it: a later canceled retry must not throw away the fact
    # that an earlier pass already restored the box (Finding 4).
    completed_once = not canceled
    declined_retry = False
    if not canceled and (hard or attention):
        do_retry = True
        if hard:
            do_retry = dialog.yesno(
                AddonTitle, MSG_PROBLEM, yeslabel="Try Again", nolabel="Close"
            )
            declined_retry = not do_retry
        else:
            _rlog("auto-fix: re-running the restore to clear a fixable state")
        if do_retry:
            if wiped_flow:
                leftovers = _guard(_wipe_pass, "wipe")
            else:
                leftovers = []
            hard, attention, canceled = _guard(
                lambda: _restore_pass(leftovers), "restore pass"
            )
            completed_once = completed_once or not canceled

    if staged:
        try:
            os.remove(local)
        except OSError:
            pass

    # Publish the verdict as a Home window property so it is READABLE over JSON-RPC
    # for diagnostics (this box's webserver /vfs endpoint refuses the log, so a
    # window property is the only reliable off-box visibility into what the restore
    # decided). Best-effort; never affects the restore.
    try:
        verdict = "hard" if hard else ("attention" if attention else "complete")
        xbmcgui.Window(10000).setProperty("ezm_restore_verdict", verdict)
        xbmcgui.Window(10000).setProperty(
            "ezm_restore_findings", " || ".join((hard or []) + (attention or []))[:900]
        )
    except Exception:
        pass

    if canceled and not completed_once:
        # A genuine abort with nothing restored: the "Restore Canceled" dialog already
        # fired inside the pass. Arm nothing, switch nothing.
        return dict(_stats, canceled=True, hard=[], attention=[])
    # If we get here after a canceled RETRY, an earlier pass DID restore the box: the
    # post-restore machinery below (markers, boot skin) must still arm. The
    # "Restore Canceled" dialog from the aborted retry already spoke, so we do not add
    # a Complete/Problem message on top - fall through to arm + the restart prompt.

    # A restore used to leave this box carrying the SOURCE box's device name and cache
    # buffer, and drop a marker so the boot service could ask the user to repair both on
    # the next start. Both questions are gone: _preserve_device_settings settles both
    # before the restart - the name preserved from the pre-extract capture, the buffer
    # reset to Kodi's default - so there is nothing to ask and no marker to arm. The
    # per-device buffer recommendation lives on demand in the add-on's own Video Cache
    # Buffer menu. The restore-check marker below is unrelated and stays - it makes the
    # boot service re-verify the restored state on the next start (silent on a pass).
    # Written HERE, after the final pass, deliberately: the wipe runs earlier and would
    # remove it, and the extract itself would overwrite it. Guarded: a marker-write
    # failure must never break the restore.
    try:
        # Record the skin the ARCHIVE carries, so the boot check can tell whether the
        # box actually reopened on it (defect A3 - the shutdown flush can overwrite
        # the boot-skin write from live memory). None/"" records no expectation and
        # the check stays silent, which is the correct behaviour for a restore that
        # did not change the skin.
        tools.mark_restore_check_pending(_boot_skin.get("target"))
    except Exception:
        pass

    # NOTE: the boot skin is applied INSIDE _restore_pass (see the note there), not here.
    # It used to run at this point, after the verdict was already computed, which made an
    # unconfirmed tvOS vector invisible to the report - the box could reopen on the old
    # skin having been told "Restore Complete".

    # The locked user-facing vocabulary (owner-edited 2026-07-17) - see MSG_* at module
    # top. Everything else about this restore lives in the log.
    if canceled:
        # A canceled RETRY over an already-restored box: the pass's own "Restore
        # Canceled" dialog already spoke. Do not add Complete/Problem on top; the box
        # was restored by the earlier pass, so still drive the restart prompt.
        ui.ask_restart("")
        return dict(_stats, canceled=True, hard=[], attention=[])
    if not hard and not attention:
        ui.ask_restart(MSG_COMPLETE)
        return dict(_stats, canceled=False, hard=[], attention=[])
    if not declined_retry:
        # HARD = backup CONTENT did not restore (the retry is already spent, so this is
        # a final notice, not another Try Again). It must use the PROBLEM wording, not
        # the softer "needs attention" - the box "may not work the way it did before"
        # (audit Finding C: a hard failure first appearing on the auto-fix pass was
        # being downgraded to needs-attention). attention-only = the box works but a
        # check flagged something -> needs-attention.
        dialog.ok(AddonTitle, MSG_PROBLEM if hard else MSG_NEEDS_ATTENTION)
    # A wiped box must ALWAYS be driven to the restart prompt, whatever was reported.
    ui.ask_restart("")
    return dict(_stats, canceled=False, hard=list(hard), attention=list(attention))


def CreateZip(
    folder,
    zip_filename,
    message_header,
    message1,
    exclude_dirs,
    exclude_files,
    prune_home_root=False,
):
    # EZ Maintenance++ : Python's zipfile can only write a LOCAL file. When the backup
    # destination is a Kodi VFS path (nfs://, smb://, ...) build the zip in special://temp
    # and copy the finished file to the destination via xbmcvfs (see the tail of this fn).
    remote = "://" in zip_filename
    target = zip_filename
    if remote:
        zip_filename = translatePath(
            os.path.join("special://temp", os.path.basename(target))
        )
    abs_src = os.path.abspath(folder)
    try:
        os.remove(zip_filename)
    except Exception:
        pass

    canceled = False
    failed = []  # arcnames that could NOT be captured (per-file accounting)
    entries_total = 0

    # ONE uniform gauge (percent + count). The divide-by-zero guard lives INSIDE
    # ui.Progress.items(), so an empty backup folder (0 files) no longer raises, and the
    # context manager guarantees the dialog is closed - the old DialogProgress leaked.
    # os.walk with the default onerror=None SKIPS a whole directory subtree
    # silently when its listing fails (SELinux/FUSE EACCES on sdcardfs, a dir
    # removed mid-walk). That is directory-granularity silent data loss - the
    # collector turns every unlistable directory into a counted, named failure.
    def _walk_error(err):
        try:
            failed.append(
                "%s/ (directory unreadable: %s)"
                % (
                    os.path.relpath(getattr(err, "filename", "?") or "?", abs_src),
                    type(err).__name__,
                )
            )
        except Exception:
            failed.append("directory unreadable (%s)" % type(err).__name__)

    try:
        with ui.Progress(message1, heading=message_header) as p:
            ITEM = []
            for _base, _dirs, files in os.walk(folder, onerror=lambda _e: None):
                ITEM.extend(files)
            n_item = len(ITEM)
            count = 0
            zip_file = zipfile.ZipFile(
                zip_filename, "w", zipfile.ZIP_DEFLATED, allowZip64=True
            )
            written_arcs = (
                set()
            )  # arcnames the POSIX walk captured (for the tvOS augment)
            try:
                for dirpath, dirnames, filenames in os.walk(
                    folder, onerror=_walk_error
                ):
                    if canceled:
                        break
                    at_root = os.path.normpath(dirpath) == abs_src
                    # exclude_dirs (i.e. ["temp"]) means special://home/temp - the
                    # transient, self-referential dir at the WALK ROOT. Prune it there
                    # ONLY: a nested dir that happens to be named "temp"
                    # (addon_data/<id>/temp/...) is real content and must be captured.
                    if at_root:
                        dirnames[:] = [d for d in dirnames if d not in exclude_dirs]
                    filenames[:] = [f for f in filenames if f not in exclude_files]
                    # A FULL backup (folder == special://home) must capture ONLY the allowed
                    # home-level dirs. Stray root userdata pollution (loose guisettings.xml or
                    # a sibling addon_data/ left by the old userdata-restore-to-HOME bug) would
                    # otherwise be re-captured and re-scattered on the next restore. Depth-scoped
                    # to the walk ROOT so the real userdata/addon_data/... is untouched. Never
                    # set for userdata/dropbox mode, where those names ARE the content.
                    if prune_home_root and at_root:
                        dirnames[:] = [d for d in dirnames if d in _HOME_ALLOWED_TOP]
                        filenames[:] = []
                    for fname in filenames:
                        if p.cancelled():
                            canceled = True
                            break
                        count += 1
                        p.items(count, n_item, note="[COLOR lime]%s[/COLOR]" % fname)
                        fpath = os.path.normpath(os.path.join(dirpath, fname))
                        # Forward-slash arcnames (the ZIP convention) so the tvOS augment's
                        # idempotency check (nsub compares its own '/'-joined arcs) matches -
                        # os.sep is already '/' on the darwin/tvOS/Android fleet, a no-op there.
                        arc = fpath[len(abs_src) + 1 :].replace(os.sep, "/")
                        # NEVER embed EZM's own settings.xml. It carries the SOURCE box's
                        # download/restore paths AND its dropbox_refresh_token (a secret);
                        # restoring it onto another box hands that box this box's paths and
                        # token. nsub excludes it on the tvOS NSUserDefaults path; the POSIX
                        # walk (Fire TV / on-disk copy) must exclude it too. Matched by suffix
                        # so a per-profile copy is covered. (Caught on real hardware 2026-07-16
                        # by tools/backup_lint.py's secret check.)
                        if _is_secret_arc(arc) or _is_regenerable_cache_arc(arc):
                            continue
                        # PER-FILE error handling: one unreadable file must never
                        # silently drop the rest of its directory (the old
                        # per-directory except swallowed every file after the first
                        # failure). Name it, count it, keep going - the manifest and
                        # the completion dialog report exactly what is missing.
                        try:
                            zip_file.write(fpath, arc)
                            written_arcs.add(arc)
                        except Exception as e:
                            failed.append(arc)
                            xbmc.log(
                                "%s : backup could not capture %s (%s: %s)"
                                % (AddonTitle, fpath, type(e).__name__, e),
                                level=xbmc.LOGERROR,
                            )
                # tvOS completeness: the POSIX walk above cannot see the userdata *.xml
                # that live only in NSUserDefaults (guisettings.xml, profiles.xml, addon
                # settings, ...), so on Apple TV the backup would silently omit exactly
                # the settings the owner cares about. Capture them by reading Kodi's
                # NSUserDefaults plist directly and add any the walk missed. Additive +
                # idempotent - a pure no-op on Fire TV / desktop (no such plist). On
                # tvOS a capture failure FAILS the backup (BackupCaptureError) instead
                # of silently shipping an incomplete zip. See nsub.py (and why xbmcvfs
                # reads cannot be used) + docs/plans/atv-restore-*.
                if not canceled:
                    cap_added, cap_failed = _capture_nsud(
                        zip_file, abs_src, written_arcs
                    )
                    failed.extend(cap_failed)
                    entries_total = len(written_arcs) + cap_added
                    # The backup carries its own truth: what it holds and what it
                    # could NOT capture, so restore (and the owner) can verify it.
                    # A manifest that cannot be written (disk full in temp, a dying
                    # card) FAILS the backup: without its truth layer the zip is a
                    # stamped partial that would later restore as "Complete" - the
                    # exact lie this contract exists to kill.
                    try:
                        _write_manifest(zip_file, entries_total, failed)
                    except Exception as e:
                        raise BackupCaptureError(
                            "backup manifest could not be written (%s)"
                            % type(e).__name__
                        )
            finally:
                zip_file.close()
    except BackupCaptureError:
        # The zip is NOT a valid backup (tvOS settings missing). Remove the partial
        # file and let the caller report the failure - never a success, never rotation.
        try:
            os.remove(zip_filename)
        except OSError:
            pass
        raise

    # If the destination was a VFS path, the zip was built in special://temp - ship the
    # finished file to the share/cloud with a gauge, then drop the staged temp.
    if remote:
        try:
            if not canceled:
                # Chunked, gauged, ATOMIC copy. It raises VfsCopyError on a definitive
                # failure (share offline / no space / permission) so backup() reports the
                # error and SKIPS rotation - a discarded failure used to look like success
                # and prune a good old backup. A cancel mid-ship returns COPY_CANCELLED.
                with ui.Progress(
                    "Copying to the backup location", heading=message_header
                ) as sp:
                    shipped = ui.copy_with_progress(zip_filename, target, progress=sp)
                if shipped == ui.COPY_CANCELLED:
                    canceled = True
        finally:
            try:
                os.remove(zip_filename)
            except Exception:
                pass

    return ZipResult(canceled=canceled, entries=entries_total, failed=failed)


def _capture_nsud(zip_file, abs_src, written_arcs):
    """Run nsub's NSUserDefaults capture (the tvOS backup augment) with honest
    failure semantics. Returns (added, failed_list).

    On tvOS the owner's settings live ONLY in NSUserDefaults, so a capture that
    raises, reports failures, or finds no store at all means the backup is MISSING
    the owner's settings: raise BackupCaptureError and fail the backup loudly.
    Off tvOS no NSUserDefaults store exists and the capture is a true no-op, so
    any hiccup there is logged and ignored."""
    tvos = _source_os() == "tvos"

    def _blog(m):
        xbmc.log("%s : %s" % (AddonTitle, m), xbmc.LOGINFO)

    try:
        from resources.lib.modules import nsub

        res = nsub.capture_nsud_userdata(zip_file, abs_src, written_arcs, log=_blog)
    except BackupCaptureError:
        raise
    except Exception as e:
        if tvos:
            raise BackupCaptureError(
                "NSUserDefaults capture raised %s: %s" % (type(e).__name__, e)
            )
        xbmc.log(
            "%s : NSUserDefaults capture skipped (non-tvOS): %s"
            % (AddonTitle, type(e).__name__),
            level=xbmc.LOGWARNING,
        )
        return (0, [])
    try:
        added, skipped, cap_failed = res
    except (TypeError, ValueError):
        added, skipped, cap_failed = 0, 0, 0
    # Tolerate either shape for the failure field: a count, or a list of paths.
    failed_list = (
        [unicode(f) for f in cap_failed]
        if isinstance(cap_failed, (list, tuple, set))
        else []
    )
    failed_n = len(failed_list) if failed_list else _as_count(cap_failed)
    if tvos:
        if failed_n:
            raise BackupCaptureError(
                "%d NSUserDefaults value(s) could not be captured" % failed_n
            )
        if not (_as_count(added) or _as_count(skipped)):
            # On tvOS the store ALWAYS exists (guisettings.xml at minimum lives
            # there); finding nothing to add or skip means the plist was never
            # read - the backup would silently miss every NSUserDefaults-only
            # setting.
            raise BackupCaptureError("NSUserDefaults store not found or unreadable")
    return (_as_count(added), failed_list)


def _as_count(value):
    """An int from a count-or-list field (len() of a collection); 0 on anything else."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return len(value)
        except TypeError:
            return 0


# EXTRACT ZIP
#
# ExtractWithProgress (below) is the ONE extractor. The old ExtractZip dispatcher and
# its silent ExtractNOProgress branch were removed: nothing called ExtractZip, and
# ExtractNOProgress was reachable only from it. ExtractNOProgress also used a bare
# extractall(), which cannot report WHICH members failed - the exact "restore that
# lies about what landed" shape the ExtractResult contract exists to prevent.

# How often the extract refreshes the progress dialog. See the CRASH FIX note in
# ExtractWithProgress: redrawing it for every one of thousands of files SIGSEGVs Kodi's
# native text renderer on Fire OS 8, so we refresh at most every _UPDATE_EVERY files.
_UPDATE_EVERY = 50


# The only top-level dirs that legitimately live at special://home. A HOME-anchored extract
# member (or a full-backup root entry) whose FIRST path segment is anything else is stray
# userdata pollution: the on-disk residue of the historical "userdata-mode backup extracted
# to HOME" bug, which scattered guisettings.xml/addon_data/ as siblings of userdata/.
_HOME_ALLOWED_TOP = frozenset({"userdata", "addons", "media", "system", "temp"})


def _first_seg(name):
    return (name or "").lstrip("/").replace("\\", "/").split("/", 1)[0]


def _archive_anchor(namelist, hint=None):
    """'home' if any member sits under userdata/ or addons/ (a full/home backup), else
    'userdata' (a settings backup: bare userdata contents, no userdata/ prefix). `hint`
    (from a pin type / filename) breaks a tie ONLY on an empty/degenerate archive."""
    for raw in namelist:
        if _first_seg(raw) in ("userdata", "addons"):
            return "home"
    if not namelist and hint in ("home", "userdata"):
        return hint
    return "userdata"


def _extract_skip(anchor, skip_prefix):
    """Return a predicate(name)->bool: True => do NOT extract this member. Drops the temp/
    self-reference, and on a HOME anchor drops any member whose first segment is not an
    allowed home-level dir (stray userdata pollution - the on-disk residue of the historical
    userdata-restore-to-HOME bug). This only CHOOSES what to extract; it never deletes an
    existing file.

    The ANCHOR rule above is inert on a USERDATA anchor, where addon_data/ and
    guisettings.xml ARE the real content. The two POLICY skips - EZM's own settings.xml
    (_is_secret_arc) and its own add-on tree (_SELF_ADDON_PREFIX) - fire on BOTH anchors,
    because a userdata-anchored archive carries addon_data/<id>/settings.xml too."""

    def _skip(name):
        rel = (name or "").lstrip("/").replace("\\", "/")
        if rel == MANIFEST_NAME:
            return (
                True  # backup metadata (verified separately), never a file to restore
            )
        if skip_prefix and rel.startswith(skip_prefix):
            return True
        # SYMMETRY with backup. backup() refuses to EMBED EZM's own settings.xml
        # (_is_secret_arc), but the extract used to happily write one back if the archive
        # contained it, which every archive made before 2026-07-16 does, as does any
        # archive built by a fork or an older build. Restoring one of those handed THIS
        # box the SOURCE box's download/restore paths and its dropbox_refresh_token -
        # precisely the leak the backup-side exclusion exists to prevent. An exclusion
        # enforced on only one side of a round trip is not an exclusion.
        if _is_secret_arc(rel):
            return True
        # Never restore EZM over the running EZM. See _SELF_ADDON_PREFIX: this is what
        # stops an older archive from silently downgrading the add-on that is mid-restore.
        if rel.startswith(_SELF_ADDON_PREFIX):
            return True
        if anchor == "home" and _first_seg(rel) not in _HOME_ALLOWED_TOP:
            return True
        return False

    return _skip


# NOTE: the boot-time home-root self-heal sweep (sweep_home_root_pollution) was REMOVED. It
# deleted files at special://home on every boot, and nothing in EZ Maintenance++ may delete
# files at boot. The anchor-aware extract (_extract_skip / _archive_anchor) prevents the
# scatter at its source - a userdata backup extracts UNDER userdata/, never at the home root -
# so there is no pollution to sweep in the first place.


def _order_userdata_first(infos):
    """Defense-in-depth ordering for a restore extract: write userdata/ (irreplaceable
    settings) FIRST, then everything else, then addons/ LAST (add-ons re-download).

    A backup zip lists userdata/ as its LAST ~70 entries, so a mid-extract failure with
    the archive's own order would lose ALL settings while the (recoverable) add-ons were
    already written. Reordering makes settings the first thing on disk. Order within each
    bucket is preserved (stable)."""
    userdata, addons, other = [], [], []
    for it in infos:
        fn = (getattr(it, "filename", "") or "").lstrip("/")
        if fn.startswith("userdata/"):
            userdata.append(it)
        elif fn.startswith("addons/"):
            addons.append(it)
        else:
            other.append(it)
    return userdata + other + addons


# --------------------------------------------------------------------------- #
# Restore-side IPTV duplicate-instance sweep (the 2026-07-08 brick guard).
#
# pvr.iptvsimple numbers its instances via instance-settings-<N>.xml. When a
# restore lays the archive's instance files OVER a target that already has its
# own (differently numbered) ones, the union accumulates duplicate instances -
# the failure that bricked a box on 2026-07-08. The sweep below runs AFTER the
# extract and ONLY when the archive actually carries pvr.iptvsimple addon_data:
# it deletes the TARGET's STRAY instance-settings-*.xml (files the archive does
# not carry; top-level, and per-profile only for profiles the archive carries)
# so the restored IPTV state exactly equals the archive. It deletes BOTH tvOS
# layers (NSUserDefaults key + POSIX file) and VERIFIES the key drop by
# re-reading the plist, because xbmcvfs.delete's boolean lies on tvOS. It never
# installs or configures any add-on; the only toggle anywhere in the restore is
# the bounded PVR pause documented in restore() itself.
# --------------------------------------------------------------------------- #
_IPTV_ADDON_DATA = "addon_data/pvr.iptvsimple/"
_INSTANCE_XML_RE = re.compile(r"^instance-settings-\d+\.xml$", re.IGNORECASE)
_PROFILE_IPTV_RE = re.compile(
    r"^(profiles/[^/]+/)addon_data/pvr\.iptvsimple/", re.IGNORECASE
)
_PVR_ADDON_ID = "pvr.iptvsimple"


def _jsonrpc(method, params):
    """One JSON-RPC call against the live Kodi; the parsed 'result' or None."""
    try:
        resp = json.loads(
            xbmc.executeJSONRPC(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                )
            )
        )
        return resp.get("result")
    except Exception:
        return None


def _pvr_enabled():
    """True iff pvr.iptvsimple is installed AND enabled; False when it is not
    installed, disabled, or the probe fails (failing toward 'no pause needed' is
    safe: the pause exists only to protect a LIVE client's teardown flush)."""
    res = _jsonrpc(
        "Addons.GetAddonDetails", {"addonid": _PVR_ADDON_ID, "properties": ["enabled"]}
    )
    try:
        return bool(res["addon"]["enabled"])
    except (TypeError, KeyError):
        return False


def _pvr_set_enabled(flag):
    """Enable/disable pvr.iptvsimple via JSON-RPC. True iff Kodi acknowledged."""
    res = _jsonrpc(
        "Addons.SetAddonEnabled", {"addonid": _PVR_ADDON_ID, "enabled": bool(flag)}
    )
    return res == "OK"


def _clear_pvr_pause_marker():
    """Clear the crash-recovery marker once the IPTV client is confirmed enabled."""
    try:
        tools.clear_pvr_pause_marker()
    except Exception:
        pass


def _userdata_rel(member, anchor):
    """A zip member -> its userdata-relative path (forward-slash), or None when the
    member does not live under userdata (a home-anchored addons/ or media/ entry)."""
    rel = (member or "").lstrip("/").replace("\\", "/")
    if anchor == "home":
        if not rel.startswith("userdata/"):
            return None
        rel = rel[len("userdata/") :]
    return rel or None


def _iptv_profile_prefixes(namelist, anchor):
    """The userdata-relative profile prefixes under which the ARCHIVE carries
    pvr.iptvsimple addon_data: '' for the top-level profile, 'profiles/<name>/' per
    profile. An empty set means the archive has no IPTV config (no sweep)."""
    prefixes = set()
    for member in namelist:
        rel = _userdata_rel(member, anchor)
        if not rel:
            continue
        if rel.lower().startswith(_IPTV_ADDON_DATA):
            prefixes.add("")
            continue
        m = _PROFILE_IPTV_RE.match(rel)
        if m:
            prefixes.add(m.group(1))
    return prefixes


def _sweep_iptv_instances(namelist, anchor, log=None):
    """Adapter: compute the archive's userdata-relative paths and delegate the IPTV
    stray-instance sweep to nsud.sweep_iptv_instances - the two-layer delete plus
    live-layer (listdir dup-count) verification live with the rest of the tvOS
    storage machinery (inside the hardware gate's fingerprint), not here. Runs
    AFTER the extract; see restore(). Returns (removed, failed_list); never raises."""
    try:
        rels = [r for r in (_userdata_rel(m, anchor) for m in namelist) if r]
        from resources.lib.modules import nsud

        return nsud.sweep_iptv_instances(control.USERDATA, rels, log=log)
    except Exception as e:
        xbmc.log(
            "%s : IPTV instance sweep failed: %s: %s"
            % (AddonTitle, type(e).__name__, e),
            level=xbmc.LOGERROR,
        )
        return (0, ["sweep aborted (%s)" % type(e).__name__])


def ExtractWithProgress(
    _in, _out, progress, skip_prefix=None, cancelable=True, skip_member=None
):
    """Extract _in over _out with the throttled gauge. Returns an ExtractResult
    carrying the TRUTH of what happened: extracted / skipped / total counts, the
    exact members that failed (with their errors), and the canceled flag - so the
    caller can report honestly instead of assuming the archive's member count."""
    count = 0
    extracted = 0
    skipped = 0
    n_files = 0
    canceled = False
    failed = []  # "member (Error: msg)" for every member that did NOT land
    try:
        zin = zipfile.ZipFile(_in, "r")
        # Defense-in-depth: extract userdata/ before addons/ so an interrupted extract
        # keeps the irreplaceable settings and only loses re-downloadable add-ons.
        ordered = _order_userdata_first(zin.infolist())
        # Never restore the transient temp tree: a full backup includes a partial copy of
        # itself at temp/<backup>.zip, and the restore zip is staged in temp - extracting
        # it would overwrite the source mid-read (Truncated file header). Filter it out up
        # front so the count/percent below is over the files we actually write.
        to_extract = [
            it
            for it in ordered
            if not (skip_prefix and it.filename.startswith(skip_prefix))
            and not (skip_member and skip_member(it.filename))
        ]
        skipped = len(ordered) - len(to_extract)
        n_files = len(to_extract)
        for item in to_extract:
            # Post-wipe this is off (cancelable=False): the box is already wiped, so a
            # cancel here would strand it - the extract must run to completion.
            if cancelable and progress.cancelled():
                canceled = True
                break
            count += 1
            # CRASH FIX (Fire OS 8 sticks): the old code called progress.items() with a
            # changing per-file basename for EVERY file. Thousands of rapid filename
            # text-layout redraws SIGSEGV Kodi's native text renderer (CGUIFont::
            # GetTextWidth <- CGUITextLayout::WrapText <- CGUITextBox::UpdateInfo), which
            # killed a restore around file ~5600 of 6130 - and a wipe-then-restore is
            # unsafe with that crash. Refresh the dialog at most every _UPDATE_EVERY files
            # (plus the first and last), with a SHORT static note - never the per-file
            # basename. The bar still advances in visible steps and always reaches 100%.
            if count == 1 or count % _UPDATE_EVERY == 0 or count == n_files:
                progress.items(
                    count,
                    n_files,
                    note="Extracting file %d of %d" % (count, n_files),
                )
            try:
                zin.extract(item, _out)
                extracted += 1
            except Exception as e:
                failed.append("%s (%s: %s)" % (item.filename, type(e).__name__, e))
    except Exception as e:
        failed.append("archive read failed (%s: %s)" % (type(e).__name__, e))
    xbmc.log(
        "%s : extract to %s -> %d ok, %d failed, %d skipped%s"
        % (
            AddonTitle,
            _out,
            extracted,
            len(failed),
            skipped,
            (" | failed: " + "; ".join(failed)) if failed else "",
        ),
        level=xbmc.LOGERROR if failed else xbmc.LOGINFO,
    )
    return ExtractResult(
        canceled=canceled,
        extracted=extracted,
        skipped=skipped,
        total=n_files,
        failed=failed,
    )
