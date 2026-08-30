import xbmc
import xbmcgui
import xbmcplugin
import xbmcvfs
import os
import sys
import time
from resources.lib.modules import control, ui
from resources.lib.modules.backtothefuture import PY2
from resources.lib.modules import maintenance

# Explicit submodule imports: a bare `import urllib` does NOT expose
# urllib.parse - the old code only worked because `import requests`
# (now removed) loaded it transitively. Proven live on the Office box:
# AttributeError: module 'urllib' has no attribute 'parse'.
if PY2:
    from urllib import quote_plus

    translatePath = xbmc.translatePath
else:
    from urllib.parse import quote_plus

    translatePath = xbmcvfs.translatePath

AddonID = "script.ezmaintenanceplusplus"

# ICONS FANARTS
ADDON_FANART = control.addonFanart()
ADDON_ICON = control.addonIcon()

# DIRECTORIES
HOME = translatePath("special://home/")

AddonTitle = "EZ Maintenance++"


# ######################### CATEGORIES ################################
def CATEGORIES():
    CreateDir(
        "Fresh Start",
        "url",
        "fresh_start",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )
    # ONE row, not a folder. "Set up this box" was a folder of five items of
    # which one was ever used, deleted for that reason in e52d170; that name is
    # burned and must not be reused (a stale favourite pointing at the old
    # action still routes to a silent no-op above). The profile's NAME goes in
    # the confirm dialog, not beside the row: the root menu is a plugin
    # directory where label2 rendering is skin-dependent, and
    # tests/test_no_skin_specific_listitem_property.py exists to stop exactly
    # that coupling (plan 6.1).
    CreateDir(
        "Apply Settings Profile",
        "url",
        "settings_profile",
        ADDON_ICON,
        ADDON_FANART,
        "One command that sets up a fresh box with the standard settings. "
        "Additive: nothing is removed, and a box that already matches is left "
        "alone.",
    )
    CreateDir(
        "Backup/Restore",
        "ur",
        "backup_restore",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )
    CreateDir(
        "Maintenance",
        "ur",
        "maintenance",
        ADDON_ICON,
        ADDON_FANART,
        "",
        isFolder=True,
    )
    CreateDir(
        "Video Cache Buffer",
        "ur",
        "adv_settings",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )
    CreateDir(
        "Log Viewer/Uploader",
        "ur",
        "log_tools",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )
    CreateDir(
        "Speedtest",
        "ur",
        "speedtest",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )

    CreateDir(
        "Settings",
        "ur",
        "settings",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )

    # Plain informational version line at the very bottom (non-clickable: the
    # "xxx" action matches no route, so selecting it just returns to the menu).
    # Version is read live from addon.xml so it stays correct on every release.
    CreateDir(
        "%s %s" % (AddonTitle, control.addonInfo("version")),
        "xxx",
        "xxx",
        None,
        ADDON_FANART,
        "",
        isFolder=False,
        iconImage="DefaultIconInfo.png",
    )


def MAINTENANCE():
    nextAutoCleanup = maintenance.getNextMaintenance()
    if nextAutoCleanup > 0:
        nextAutoCleanup = time.strftime(
            "%a, %d %b %Y %I:%M:%S %p %Z", time.localtime(nextAutoCleanup)
        )
        CreateDir(
            "Next Auto Cleanup: %s" % nextAutoCleanup,
            "xxx",
            "xxx",
            None,
            ADDON_FANART,
            "",
            isFolder=False,
            iconImage="DefaultIconInfo.png",
        )
    CreateDir("Clear All", "url", "clear_all", ADDON_ICON, ADDON_FANART, "")
    CreateDir("Clear Cache", "url", "clear_cache", ADDON_ICON, ADDON_FANART, "")
    CreateDir("Clear Packages", "url", "clear_packages", ADDON_ICON, ADDON_FANART, "")
    CreateDir("Clear Thumbnails", "url", "clear_thumbs", ADDON_ICON, ADDON_FANART, "")
    CreateDir(
        "Clear Recently Played Channels",
        "url",
        "clear_channels",
        ADDON_ICON,
        ADDON_FANART,
        "",
    )


def APPLY_SETTINGS_PROFILE():
    """The one-row Settings Profile flow (plan 6.2): one confirm, a progress
    dialog with real step messages, apply in 7.4 order, in-flow verify, ONE
    result message folded into the restart offer, no questions in between.

    The engine lives in resources/lib/modules/profile.py and never imports
    xbmcgui; this function owns every dialog. A partial result is reported as
    partial, never as complete."""
    from resources.lib.modules import profile, tools

    try:
        bundle = profile.load(
            profile.default_bundle_dir(), profile.detect_device_class()
        )
    except profile.ProfileError as e:
        for problem in e.problems:
            xbmc.log(
                "%s : settings profile bundle invalid: %s" % (AddonID, problem),
                level=xbmc.LOGERROR,
            )
        ui.error(
            "The settings profile bundle failed validation, so nothing was "
            "applied. The log has the details."
        )
        return
    ops = profile.plan(bundle)
    if not ui.confirm(
        "Apply the %s settings profile?\n"
        "This sets up the box the standard way: web control and remote "
        "control on, add-ons allowed from any repository, guide and language "
        "defaults, the Tony.7.Bones repository, the KodiShare and KodiBackup "
        "sources, and this add-on's backup folder.\n"
        "Nothing is removed. Kodi asks to restart when it finishes."
        % bundle["name"],
        yeslabel="Apply",
        nolabel="Cancel",
    ):
        return
    with ui.Progress("Applying the %s settings profile" % bundle["name"]) as p:
        record = profile.apply(ops, on_step=p.items)
        # In-flow verification (plan 7.6): read the live state back rather
        # than trusting what apply just reported. Class C is deliberately not
        # here - Files.GetSources cannot see an on-disk write until the next
        # boot, so its live confirmation belongs to the boot check.
        vitems = profile.verify(ops)
    items = record["items"] + vitems
    ok, total, failures = profile.summarize(items)
    for w in record["warnings"]:
        xbmc.log(
            "%s : settings profile warning: %s" % (AddonID, w),
            level=xbmc.LOGWARNING,
        )
    for it in failures:
        xbmc.log(
            "%s : settings profile %s: %s -> %s (%s)"
            % (AddonID, it["kind"], it["label"], it["outcome"], it["detail"]),
            level=xbmc.LOGWARNING,
        )
    applied_any = any(it["outcome"] == "applied" for it in record["items"])
    if not failures and not applied_any:
        # The idempotent re-run: nothing changed, so nothing needs a restart,
        # no boot check is owed, and saying "applied" would claim work that
        # never happened. Verified distinguishable per item in the log.
        ui.done(
            "This box already matches the settings profile. Nothing was "
            "changed."
        )
        return
    # Arm the one-shot boot check that confirms after the restart what cannot
    # be confirmed now (the sources). A failed marker write is LOUD: 6.3
    # promises sources take effect after the reopen, and if this write fails
    # silently nothing ever checks that promise (plan 7.7).
    # Network.MacAddress returns the literal "Busy" while the info system is
    # still warming up (it did exactly that on the first full bench run,
    # 2026-08-30, and the boot check would have read "Busy" as a FOREIGN box
    # and cleared the marker unrun). Retry briefly; a stamp that never becomes
    # MAC-shaped is recorded as empty, which the reader treats as unstamped.
    mac = ""
    for _ in range(5):
        mac = (xbmc.getInfoLabel("Network.MacAddress") or "").strip()
        if ":" in mac:
            break
        xbmc.sleep(200)
    payload = {
        "box": mac if ":" in mac else "",
        "created": time.time(),
        "sources": [path for _name, path in bundle["sources"]],
        "settings": dict(bundle["class_a"]),
    }
    if not tools.mark_profile_check_pending(payload):
        xbmc.log(
            "%s : settings profile boot-check marker could NOT be written; "
            "the post-restart source check will not run" % AddonID,
            level=xbmc.LOGWARNING,
        )
    if not failures:
        status = (
            "Settings profile applied. The media sources appear after Kodi "
            "reopens."
        )
    else:
        status = (
            "Settings profile partly applied (%d of %d). Nothing was removed; "
            "the log has the details." % (ok, total)
        )
    ui.ask_restart(status)


# RETIRED 2026-07-22: the "Set up this box" folder, ALL FIVE ITEMS, and the
# boxsetup.py module behind them. Owner's verdict after living with it, and the
# reason each removal is safe, so this is not re-litigated:
#
#   * Add media sources - DELETED. It wrote the .T7B repository and the two mini
#     NFS shares into sources.xml. Kodi's own File Manager adds a source in the
#     same number of steps, which is what it is for, so the add-on was
#     reimplementing a built-in with three hardcoded paths that would rot the day
#     the mini's address changed. (It briefly became a Media Sources settings tab
#     the same day; that tab is gone too.)
#   * Device Name - DELETED. It only wrote Kodi's own services.devicename, which
#     every box already exposes at Settings > Services > General. Preservation
#     across a restore does NOT depend on it (that is tools._get_devicename /
#     _set_devicename, both still live and still tested).
#   * Set up weather, Enable RSS ticker, Set up everything - DELETED with their
#     implementations; nothing else in the add-on called them.
#
# boxsetup.py also left service.py's _CONTRACT_FILES, so the storage-contract
# fingerprint changes with this release. That is correct, not drift: the file it
# hashed no longer exists.


# ###########################################################################################
# ##################################### OWNER TOOLS #########################################


# The manifest wiz.backup embeds
# ({"created","source_os","kodi_version","entries","failed":[...]}).
BACKUP_MANIFEST_NAME = "backup_manifest.json"
# Any entry under this addon_data path means the archive carries IPTV client state,
# whether the zip is anchored at home/ ("userdata/addon_data/...") or at userdata/.
IPTV_ADDON_DATA_MARKER = "addon_data/pvr.iptvsimple/"


def analyze_backup_zip(zip_path):
    """Read-only analysis of a backup zip (never extracts, never restores).

    Returns a dict:
      total_entries     - int, every member in the archive
      manifest_present  - bool, backup_manifest.json anywhere in the archive
      manifest_failed   - list[str], the manifest's "failed" list ([] if absent)
      kodi_version      - int, the Kodi MAJOR the backup was made on (0 when the
                          manifest is absent or predates the stamp; restore's
                          version gate treats 0 as cross-major)
      iptv_present      - bool, any addon_data/pvr.iptvsimple/ entry
      composition       - {"userdata": n, "addons": n, "media": n, "other": n}
                          counted by each member's top-level path segment

    Raises whatever zipfile raises on an unreadable/corrupt archive; the caller
    turns that into a dialog."""
    import json
    import zipfile

    report = {
        "total_entries": 0,
        "manifest_present": False,
        "manifest_failed": [],
        "kodi_version": 0,
        "iptv_present": False,
        "composition": {"userdata": 0, "addons": 0, "media": 0, "other": 0},
    }
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        report["total_entries"] = len(names)
        manifest_member = None
        for member in names:
            norm = member.replace("\\", "/").lstrip("/")
            if not norm:
                continue
            top = norm.split("/", 1)[0]
            if top in report["composition"]:
                report["composition"][top] += 1
            else:
                report["composition"]["other"] += 1
            if norm.split("/")[-1] == BACKUP_MANIFEST_NAME and manifest_member is None:
                manifest_member = member
                report["manifest_present"] = True
            if IPTV_ADDON_DATA_MARKER in norm:
                report["iptv_present"] = True
        if manifest_member is not None:
            try:
                data = json.loads(zf.read(manifest_member).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                data = None
            if isinstance(data, dict):
                failed = data.get("failed")
                if isinstance(failed, list):
                    report["manifest_failed"] = [str(item) for item in failed]
                try:
                    kv = int(float(data.get("kodi_version")))
                except (TypeError, ValueError):
                    kv = 0
                report["kodi_version"] = kv if kv > 0 else 0
    return report


def format_backup_report(report, zip_name=""):
    """Turn analyze_backup_zip()'s dict into the owner-facing dialog text."""
    comp = report["composition"]
    lines = []
    if zip_name:
        lines.append("Backup archive: %s" % zip_name)
    lines.append("Total entries: %d" % report["total_entries"])
    lines.append(
        "Manifest (%s): %s"
        % (BACKUP_MANIFEST_NAME, "present" if report["manifest_present"] else "MISSING")
    )
    failed = report["manifest_failed"]
    if failed:
        shown = ", ".join(failed[:5])
        extra = len(failed) - 5
        if extra > 0:
            shown += ", and %d more" % extra
        lines.append("Manifest failed items (%d): %s" % (len(failed), shown))
    elif report["manifest_present"]:
        lines.append("Manifest failed items: none")
    if report["manifest_present"]:
        kv = report.get("kodi_version") or 0
        lines.append(
            "Made on Kodi: %s"
            % (kv if kv > 0 else "not recorded (backup predates the version stamp)")
        )
    lines.append(
        "IPTV (pvr.iptvsimple) data: %s" % ("yes" if report["iptv_present"] else "no")
    )
    lines.append(
        "Top level: userdata=%d, addons=%d, media=%d, other=%d"
        % (comp["userdata"], comp["addons"], comp["media"], comp["other"])
    )
    return "\n".join(lines)


def VERIFY_BACKUP_ARCHIVE():
    """Owner tool: pick a backup zip (same restore.path picker restore uses), open
    it READ-ONLY, and report what is inside. Never extracts, never restores."""
    # wiz.configured_path is the one definition of "configured" this add-on has:
    # it strips whitespace (so "   " bails here instead of failing later) and strips
    # the port Kodi's browse dialog bakes into nfs:// paths (which breaks listing).
    # The Backup/Restore row shows the same value, so what is on screen is what this
    # reads.
    from resources.lib.modules import wiz

    zipFolder = wiz.configured_path("restore.path")
    if zipFolder == "":
        control.infoDialog("Please Setup a Zip Files Location first")
        # ON the Backup/Restore tab, where restore.path actually is. A plain
        # openSettings() lands on Maintenance: told to set a path, then dropped on the
        # wrong tab with nothing on screen saying which one.
        control.openSettingsTab(control.SETTINGS_TAB_BACKUP_RESTORE)
        return
    try:
        _dirs, _files = xbmcvfs.listdir(zipFolder)
    except Exception:
        _files = []
    names = [f for f in _files if f.endswith(".zip")]
    if not names:
        ui.error("No backup zips found in:\n%s" % zipFolder)
        return
    select = control.selectDialog(names)
    if select == -1:
        return
    chosen = names[select]
    source = translatePath(os.path.join(zipFolder, chosen))
    local = source
    temp_special = None
    if "://" in source:
        # Remote share: zipfile cannot open a VFS URL, so stage a read-only copy in
        # temp (the source archive itself is never touched).
        temp_special = "special://temp/ezmpp_verify_%s" % chosen
        try:
            with ui.Progress(
                "Fetching backup for verification...", heading=AddonTitle
            ) as p:
                outcome = ui.copy_with_progress(source, temp_special, progress=p)
        except Exception:
            ui.error("Could not fetch that backup from the share for verification.")
            return
        if outcome != ui.COPY_OK:
            return  # user cancelled the fetch; nothing to report
        local = translatePath(temp_special)
    try:
        try:
            report = analyze_backup_zip(local)
        except Exception as e:
            ui.error(
                "Could not read that zip (corrupt or not a zip?)\n%s: %s"
                % (type(e).__name__, e)
            )
            return
    finally:
        if temp_special is not None:
            try:
                os.remove(translatePath(temp_special))
            except OSError:
                pass
    ui.done(format_backup_report(report, chosen))


# --------------------------------------------------------------------------- #
# Guards for the looping Backup/Restore menu (2026.07.19.8)
#
# The menu re-presents itself after each sub-action. These decide when it must NOT,
# because a sub-action returns None whether it worked, cancelled, or fired an ASYNC
# builtin that took the screen away from us.
# --------------------------------------------------------------------------- #
# Published by wiz.restore() (wiz.py:1769) on every path that got as far as touching
# the box. Read as "a restore really ran", not as a verdict - any value counts.
RESTORE_VERDICT_PROP = "ezm_restore_verdict"


def _clear_restore_verdict():
    """Drop a stale verdict from an earlier restore in this same Kodi session.

    Without this, one restore would poison every later RESTORE pick in the same
    session: the property would still be set, and the menu would exit on a restore
    the user actually cancelled. Best-effort - a failure here only costs an early
    exit from a menu, so it must never raise into the menu loop."""
    try:
        xbmcgui.Window(10000).clearProperty(RESTORE_VERDICT_PROP)
    except Exception:
        pass


# Destination: 0 Local, 1 Network (SMB/NFS), 2 Dropbox. On Dropbox neither path
# setting applies - settings.xml hides both - so the second line must say so
# rather than report the stale local path she is not writing to.
DESTINATION_DROPBOX = "2"


def _path_detail(setting_id):
    """The second line for a row whose action reads or writes a configured path.

    Just the path: the row's own label already says which one it is, so a
    "Backup path:" prefix would repeat the word directly above it.

    Says the awkward states plainly. "Not set" is what makes Backup and Restore
    bail into the settings window, and this is the one place she is already
    looking when it happens.

    It reports what the ACTION will use, via the action's own wiz.configured_path,
    not the raw setting. Two things used to differ. A whitespace-only setting showed
    "Not set" while Backup and Restore treated it as configured and pushed on, so the
    row told the truth and the action did not. And the row printed the nfs:// port
    that both actions strip before using (the live boxes really do carry :2049 in
    that setting), so the folder on screen was not the folder written to."""
    try:
        if control.setting("destination") == DESTINATION_DROPBOX:
            return "Dropbox"
        from resources.lib.modules import wiz

        return wiz.configured_path(setting_id) or "Not set"
    except Exception:
        # The second line is decoration. A settings read that throws must never
        # take the menu down with it.
        return ""


# How much of a select-dialog row is readable at a glance, in characters.
#
# Estuary draws each row of its select dialog from ListItem.Label in font13 -
# NotoSans-Regular at 30px - inside a list 880 wide with 20px insets, so 840
# skin pixels of usable label. Stock Estuary and skin.estuary7 were both read on
# 2026-07-22 and the geometry and the font are identical in the two trees, so
# this is not a number tuned to our skin.
#
# Measured against the real NotoSans-Regular.ttf at 30px, path text runs about
# 14.6px per character, and the fleet's own row
#   "Backup   nfs://192.168.7.2/Users/moquette/Kodi/Backup/fireos/"
# is 61 characters and 891px - 51px past the edge. That is the overflow this
# budget exists to stop. 56 characters is roughly 818px and fits, with a little
# room for a path made of wider-than-average characters.
#
# It is a readability heuristic, not a contract. A proportional font cannot be
# budgeted exactly by counting characters, and a pathological path of nothing
# but "m" would still overrun. Overrunning is not a NEW failure - it is what
# every long row did before this - so an approximate budget costs nothing, while
# an exact one would cost a font metric table the add-on has no way to keep
# true across skins.
ROW_BUDGET = 56

# The gap between a row's action name and its greyed path.
ROW_SEPARATOR = "   "

# ASCII, not U+2026. The row is drawn in whatever font the current skin resolves
# for font13, and three dots cannot come out as a missing-glyph box on a skin
# that ships a narrower font file.
ELLIPSIS = "..."


def _elide_path(path, budget):
    """`path` shortened to `budget` characters by dropping the MIDDLE of it.

    The Backup and Restore rows carry the folder they act on. With the fleet's
    real path that row is 61 characters where about 56 fit, and Estuary SCROLLS
    the focused row rather than clipping it, so the row the owner is actually
    pointing at is the one that becomes unreadable: a bench capture caught it
    mid-scroll reading

        /Users/moquette/Kodi/Backup/fireos/ | Backup   nfs://192.168.7.

    with the row's own name sitting in the middle of the path. Correct data,
    unreadable presentation, and worst on the row under the cursor.

    Dropping the middle keeps the two parts that answer the two questions:

      * the HEAD - scheme and host - answers "which share is this".
      * the TAIL - as many whole trailing segments as fit - answers "which
        folder on it", and that is the part that tells .../Backup/fireos from
        .../Backup/tvos. Truncating from the right, which is what a dialog does
        on its own, throws away exactly that.

    Only whole path segments are kept, so the result never shows half a folder
    name and never invents one. A path with no separators to cut on, or a host
    so long that not even one trailing segment fits beside it, falls back to
    keeping the tail alone; that is still the more informative end.

    The FULL path is not lost: it is on the Backup/Restore tab of the add-on
    settings, in the very setting this reads, and the Settings row of this same
    menu jumps straight there.

    Returns `path` untouched when it already fits, and for "", None, "Not set"
    and "Dropbox" alike - none of which are long enough to elide."""
    if not path or budget <= 0 or len(path) <= budget:
        return path

    # The head is scheme://host, and only that: the first "/" after the "://".
    # A local path has none, and then the tail is the whole story.
    head = ""
    mark = path.find("://")
    if mark != -1:
        slash = path.find("/", mark + 3)
        if slash != -1:
            head = path[:slash]

    # A trailing slash is worth keeping - it is what says "folder" - and it costs
    # one character, so it is reserved out of the budget rather than counted as a
    # segment.
    trailing = "/" if path.endswith("/") and len(path) > 1 else ""
    segments = [s for s in path[len(head) :].split("/") if s]
    prefix = (head + "/" + ELLIPSIS) if head else ELLIPSIS

    tail = ""
    for segment in reversed(segments):
        candidate = "/" + segment + tail
        if len(prefix) + len(candidate) + len(trailing) > budget:
            break
        tail = candidate
    if tail:
        return prefix + tail + trailing

    # Nothing whole fits beside the head. Keep the tail characters instead of the
    # head ones: a truncated host is a host you cannot identify anyway, while the
    # last characters of the path still name the folder.
    if budget > len(ELLIPSIS):
        return ELLIPSIS + path[-(budget - len(ELLIPSIS)) :]
    return path[:budget]


def _menu_rows(rows):
    """[(label, detail)] -> plain label strings, path folded into the row.

    NO ListItems, NO art, NO detailed view. Kodi's detailed list (useDetails) is
    the only way to get a real second line, but it reserves a thumbnail column
    and core fills an artless row with DefaultAddonMore.png - four "+" glyphs
    down the side of a backup menu. Giving the rows art to suppress that means
    inventing decoration nobody asked for.

    So the path rides the label itself, greyed, on one line. Every skin draws
    ListItem.Label; nothing here needs a skin to cooperate, which is the whole
    point after 2026-07-22.

    A path too long for the row is elided in the MIDDLE (see _elide_path) rather
    than left to the dialog. Estuary scrolls the FOCUSED row, so leaving it long
    made the row under the cursor the ambiguous one - the row's own name would
    slide into the middle of the path. The budget is per row, because the action
    name shares the line with the path: "Restore" leaves one character less for
    the folder than "Backup" does.

    The colour markup is deliberately outside the budget. It is never drawn - it
    is a formatting tag Kodi consumes - so counting it would shorten every path
    by 20 characters for nothing."""
    out = []
    for label, detail in rows:
        if detail:
            shown = _elide_path(detail, ROW_BUDGET - len(label) - len(ROW_SEPARATOR))
            # HEX, not the name "grey". A colour NAME is looked up in the skin's
            # palette and GUIColorManager falls back to sscanf("%x") on a miss,
            # which leaves 0 - alpha 0, i.e. the path renders INVISIBLE on any
            # skin that does not define it. That is the same silent-disappearance
            # this whole change exists to kill. FFA0A0A0 is byte-identical to what
            # both fleet skins call grey, so nothing looks different, and it
            # cannot vanish anywhere.
            out.append("%s%s[COLOR FFA0A0A0]%s[/COLOR]" % (label, ROW_SEPARATOR, shown))
        else:
            out.append(label)
    return out


# MOVED 2026-07-22 to control.openSettingsTab / control.SETTINGS_TAB_BACKUP_RESTORE.
# The same jump is needed by wiz.backup and wiz.restoreFolder when they bail on an
# unset path, and wiz cannot import default.py. One implementation, in the module all
# three already import.


def _restore_verdict():
    """True if wiz.restore() published a verdict since the last clear."""
    try:
        return bool(xbmcgui.Window(10000).getProperty(RESTORE_VERDICT_PROP))
    except Exception:
        # Unreadable means unknown. Return False so the menu stays open: the failure
        # mode of a false True (ejecting her to the root) is the bug being fixed,
        # while a false False is caught by _safe_to_re_present's abort check.
        return False


def _safe_to_re_present(monitor=None, settle=0.25, opened_before=None):
    """False when re-presenting the Backup/Restore menu would fight another window.

    Two ASYNC builtins can take the screen between iterations, invisibly to a
    sub-action's return value:

      * `Quit` (ui.restart, from the post-restore ask_restart). executebuiltin is
        called WITHOUT the wait flag, so Kodi's teardown runs while this script is
        still alive. Monitor.waitForAbort is Kodi's own "we are shutting down" signal.
      * `Addon.OpenSettings` (control.openSettings). ALL THREE sub-actions bail to it
        when their path setting is unconfigured - wiz.backup on download.path
        (wiz.py:337), wiz.restoreFolder (wiz.py:684) and VERIFY_BACKUP_ARCHIVE on
        restore.path. Re-presenting would drop a modal select dialog on top of the
        settings window the user was just sent to.

    `opened_before` is control.open_settings_count() sampled BEFORE the sub-action
    ran, and it is the authoritative signal for the second case. Asking Kodi whether
    the settings window is active yet is a RACE the guard used to lose: this function
    settled 0.25s and looked once, while _open_settings_tab (right above) polls the
    very same window for up to 5s because on an appliance that is how long it can
    take. A late window meant the probe said "nothing there", the menu re-presented,
    and the modal landed on top of the settings window anyway - exactly the defect
    the guard exists to prevent. The call COUNT cannot lose that race: the builtin was
    either fired or it was not, and the answer is already known before we look.

    The window probe stays as a backstop for anything that opens the settings window
    without going through control.openSettings. The short wait is still the abort
    check. Best-effort by design - if the probes themselves fail we keep the menu
    open, because staying is the behaviour the owner asked for and the abort check is
    the backstop for the one case where leaving matters."""
    if opened_before is not None:
        try:
            if control.open_settings_count() != opened_before:
                return False  # a sub-action bailed to the settings window
        except Exception:
            pass  # older control.py without the counter: fall back to the probe
    try:
        if monitor is None:
            monitor = xbmc.Monitor()
        if monitor.waitForAbort(settle):
            return False  # Kodi is shutting down
        return not xbmc.getCondVisibility("Window.IsActive(addonsettings)")
    except Exception:
        return True


# ###########################################################################################
# ###########################################################################################


def FRESHSTART(mode="verbose"):
    # Wipe to a clean Kodi, then hard-exit via ui.terminate() (os._exit, NOT a graceful
    # Quit). Skipping CApplication::Stop() skips its save-skin-settings-on-exit flush,
    # which used to re-write the wiped custom skin's addon_data AFTER the wipe and
    # re-dirty the slate. No pre-wipe skin-swap (that step used to hang). Uses the shared
    # hardened wipe engine in onetap.py (preserves this add-on, its runtime deps, temp/,
    # and backupdir); the two Fresh Start settings can also keep the user's file-manager
    # sources (+ credentials) and repositories. mode="silent" wipes with no prompts, no exit.
    if mode != "silent":
        # Fresh Start deletes everything under the wipe root (special://home), INCLUDING
        # the active skin's files when that skin is installed there. A skin that lives
        # OUTSIDE the wipe root (the built-in Estuary, bundled read-only in the APK)
        # survives, so its dialogs can still draw the completion prompt after the wipe.
        # Refuse when the live skin sits under the wipe root: it would be pulled out from
        # under Kodi mid-wipe and nothing could render. Checked by PATH, never by skin
        # id, so EZM++ stays skin-agnostic.
        skin_path = os.path.normpath(translatePath("special://skin/"))
        wipe_root = os.path.normpath(HOME)
        if skin_path == wipe_root or skin_path.startswith(wipe_root + os.sep):
            ui.error(
                "Please switch to the default Estuary skin before running Fresh "
                "Start.\n"
                "Settings > Interface > Skin > Estuary",
                heading=AddonTitle,
            )
            return
        if not ui.confirm_wipe(
            "Wipe this Kodi to a clean state?\n"
            "EZ Maintenance++ will survive the wipe. You must relaunch Kodi when done.",
            heading=AddonTitle,
        ):
            return
    # The wipe is a single step (no per-item progress); the context-managed gauge shows a
    # 'Wiping install...' spinner and is always closed.
    # Opt-in "keep across wipe" (Fresh Start settings tab; default OFF == full wipe).
    keep_sources = control.setting("freshstart.keep_sources") == "true"
    keep_repos = control.setting("freshstart.keep_repos") == "true"
    wipe_failed = None  # None = the wipe itself never ran (import failure / raise)
    # Did the destructive pass BEGIN? Distinct from wipe_failed, which only says whether
    # it ran to completion. _wipe deletes files first and sweeps NSUserDefaults keys last
    # (onetap._wipe_nsud_keys), so a raise from the key pass lands here with the POSIX
    # tree - including every userdata/Database file - ALREADY GONE. Treating that as
    # "the wipe did not run" both told the owner a falsehood and, worse, returned without
    # terminating: Kodi then stayed alive on a tree whose open databases had been
    # unlinked, which is precisely the SIGABRT this release exists to prevent.
    wipe_started = False
    with ui.Progress("Wiping install...", heading=AddonTitle) as p:
        try:
            from resources.lib.modules import onetap

            # keep_addon_db() preserves Kodi's add-on state DB so EZ Maintenance++ comes
            # back ENABLED after the restart (not disabled/"gone", which was the bad UX).
            # The opt-in keeps add the user's file-manager sources (+ credentials) and/or
            # their repositories to what survives. _wipe returns
            # (files_removed, keys_removed, failed_count, named_leftovers); Fresh Start
            # only needs the failed COUNT. progress=p.items drives the wipe gauge.
            excludes = onetap._wipe_excludes()
            if keep_repos:
                excludes = excludes | onetap.repository_addon_names()
            keep = onetap.keep_addon_db()
            if keep_sources:
                keep = keep | onetap.keep_source_files()
            wipe_started = (
                True  # set BEFORE the call: anything after this may have deleted
            )
            _f, _k, wipe_failed, _leftovers = onetap._wipe(
                HOME, excludes, keep, progress=p.items
            )
        except Exception as e:
            xbmc.log(
                "%s : Fresh Start wipe FAILED: %s: %s"
                % (AddonTitle, type(e).__name__, e),
                level=xbmc.LOGERROR,
            )
        try:
            xbmc.executebuiltin(
                "UpdateLocalAddons"
            )  # reconcile the DB with what's left
        except Exception:
            pass
    # The box is going to a clean state, so the video cache buffer goes to Kodi's own
    # default with it (owner decision 2026-07-31: no inherited buffer survives a wipe or
    # a restore). MOSTLY redundant - the wipe removes guisettings.xml and Kodi's own
    # default is already 20 MB - but only mostly, and the gaps are exactly the ones this
    # add-on has been bitten by before: Kodi's LIVE store still holds this box's old
    # buffer, so any flush writes it straight back into the fresh file, and a wipe that
    # left guisettings.xml behind (wipe_failed) leaves the old number sitting in it.
    # Runs AFTER the wipe for that second reason. Gated on wipe_started so a run that
    # destroyed nothing changes nothing. No prompt here or anywhere; the per-device
    # recommendation is offered on demand under Video Cache Buffer.
    if wipe_started:
        try:
            from resources.lib.modules import tools

            tools.reset_cache_buffer(
                log=lambda m: xbmc.log("%s : %s" % (AddonTitle, m), level=xbmc.LOGINFO)
            )
        except Exception as e:
            xbmc.log(
                "%s : cache-buffer reset after Fresh Start failed: %s: %s"
                % (AddonTitle, type(e).__name__, e),
                level=xbmc.LOGWARNING,
            )
    if mode != "silent":
        # Honest completion: "Clean slate ready" is only ever claimed when the wipe
        # ran AND removed everything it was asked to. A wipe that never ran, or that
        # left survivors (on tvOS: NSUserDefaults keys that resurrect old settings),
        # says so plainly instead of pretending.
        if wipe_failed is None and not wipe_started:
            # Genuinely nothing happened (import error, or a raise before the first
            # delete). Kodi is untouched, so it is safe to stay up.
            ui.done(
                "Fresh Start FAILED: the wipe did not run. Nothing was removed. "
                "See the log."
            )
            return
        if wipe_failed is None:
            # The wipe BEGAN and then raised. Files are gone - including databases Kodi
            # holds open - so staying up is the one thing we must not do. Terminate, and
            # say what actually happened instead of "nothing was removed".
            ui.ask_terminate(
                "Fresh Start did not finish: it stopped part way through, so some "
                "items were removed and others were not (see the log).",
                heading=AddonTitle,
            )
            return
        # Name what the opt-in keeps preserved, so a non-empty "clean" slate is honest.
        kept = []
        if keep_sources:
            kept.append("file manager sources")
        if keep_repos:
            kept.append("repositories")
        kept_line = ("\n\nKept: " + ", ".join(kept) + ".") if kept else ""
        # Completion notice: the box MUST close, so ask_terminate always exits. It
        # renders because Fresh Start required stock Estuary, which survived the wipe.
        if wipe_failed:
            ui.ask_terminate(
                "Fresh Start INCOMPLETE: %d item(s) could not be removed and may "
                "carry old settings over (see the log)." % wipe_failed,
                heading=AddonTitle,
            )
        else:
            ui.ask_terminate(
                "Clean slate ready.%s\n\nAfter you reopen Kodi, EZ Maintenance++ is "
                "under Add-ons > Program add-ons (if it is off, open it there and "
                "choose Enable)." % kept_line,
                heading=AddonTitle,
            )


def CreateDir(
    name,
    url,
    action,
    icon,
    fanart,
    description,
    isFolder=False,
    iconImage="DefaultFolder.png",
):
    if icon is None or icon == "":
        icon = ADDON_ICON
    u = (
        sys.argv[0]
        + "?url="
        + quote_plus(url)
        + "&action="
        + str(action)
        + "&name="
        + quote_plus(name)
        + "&icon="
        + quote_plus(icon)
        + "&fanart="
        + quote_plus(fanart)
        + "&description="
        + quote_plus(description)
    )
    ok = True
    if PY2:
        liz = xbmcgui.ListItem(name, iconImage=iconImage, thumbnailImage=icon)
    else:
        liz = xbmcgui.ListItem(name)
        # "thumb", NOT "thumbnailImage". The PY2 branch above passes
        # thumbnailImage= as a ListItem CONSTRUCTOR kwarg, which really did set
        # the thumbnail; the py3 port turned that kwarg name into a setArt KEY,
        # and there is no such art key, so it was silently dropped. With no
        # thumb and setInfo(type="Video") below, Kodi fell back to
        # DefaultVideo.png - the reel-to-reel movie camera that has been showing
        # in place of the add-on's own icon on every menu since the py3 port.
        liz.setArt({"icon": iconImage, "poster": icon})
    liz.setInfo(type="Video", infoLabels={"Title": name, "Plot": description})
    liz.setProperty("Fanart_Image", fanart)
    ok = xbmcplugin.addDirectoryItem(
        handle=int(sys.argv[1]), url=u, listitem=liz, isFolder=isFolder
    )
    return ok


def _dbtest(dropbox_remote):
    # Hidden on-device smoke test: upload -> list -> download -> delete a tiny file.
    # Logs EZPP_DBTEST lines. Only works once a Dropbox refresh token exists.
    import time as _time

    name = "ezpp_dbtest_%s.zip" % _time.strftime("%Y%m%d%H%M%S")
    local = translatePath("special://temp/" + name)
    try:
        with open(local, "wb") as fh:
            fh.write(b"EZPP dbtest payload")
        xbmc.log("EZPP_DBTEST start name=%s" % name, level=xbmc.LOGINFO)
        dropbox_remote.upload(local, name)
        xbmc.log("EZPP_DBTEST upload OK", level=xbmc.LOGINFO)
        listing = dropbox_remote.list_backups()
        xbmc.log(
            "EZPP_DBTEST list found=%s present=%s" % (len(listing), name in listing),
            level=xbmc.LOGINFO,
        )
        got = dropbox_remote.download(name)
        size = os.path.getsize(translatePath(got))
        xbmc.log("EZPP_DBTEST download OK bytes=%s" % size, level=xbmc.LOGINFO)
        dropbox_remote.delete(name)
        xbmc.log("EZPP_DBTEST delete OK", level=xbmc.LOGINFO)
        xbmc.log("EZPP_DBTEST PASS", level=xbmc.LOGINFO)
    except Exception as e:
        xbmc.log("EZPP_DBTEST FAIL %s: %s" % (type(e).__name__, e), level=xbmc.LOGERROR)
    finally:
        try:
            os.remove(local)
        except Exception:
            pass


if PY2:
    from urlparse import parse_qsl
else:
    from urllib.parse import parse_qsl

# RunScript(script.ezmaintenanceplusplus,authorize) / (...,dbtest) arrive as a bare
# positional arg in sys.argv[1], NOT as the plugin "?action=" querystring. Route those
# first and exit, before the normal plugin parsing (which assumes sys.argv[2] is a qs).
_script_arg = sys.argv[1] if len(sys.argv) > 1 else ""
if _script_arg in ("authorize", "dbtest"):
    from resources.lib.modules import dropbox_remote

    if _script_arg == "authorize":
        dropbox_remote.authorize()
    else:
        _dbtest(dropbox_remote)
    sys.exit(0)

params = dict(parse_qsl(sys.argv[2].replace("?", "")))
action = params.get("action")

# xbmc.log("ezmaintenanceplus: action: %s" % action, level=xbmc.LOGINFO)

if action is None:
    CATEGORIES()
elif action == "settings":
    # Open Kodi's native add-on settings dialog. Every label now resolves through
    # resources/language/.../strings.po, so it renders correctly (the old custom
    # in-app screen was a workaround for a mis-labelled settings.xml, now removed).
    control.openSettings()

elif action == "fresh_start":
    FRESHSTART()

elif action == "settings_profile":
    APPLY_SETTINGS_PROFILE()

elif action == "maintenance":
    MAINTENANCE()

elif action == "adv_settings":
    from resources.lib.modules import tools

    tools.advancedSettings()

elif action == "clear_all":
    from resources.lib.modules import maintenance

    maintenance.clearAll()

elif action == "clear_channels":
    from resources.lib.modules import maintenance

    maintenance.clearRecentChannels()

elif action == "clear_cache":
    from resources.lib.modules import maintenance

    maintenance.clearCache()

elif action == "log_tools":
    from resources.lib.modules import logviewer

    logviewer.logView()


elif action == "clear_packages":
    from resources.lib.modules import maintenance

    maintenance.purgePackages()
elif action == "clear_thumbs":
    from resources.lib.modules import maintenance

    maintenance.deleteThumbnails()

elif action == "backup_restore":
    from resources.lib.modules import wiz

    # "VERIFY BACKUP ARCHIVE" moved here from the retired Tools category, which had
    # shrunk to this single entry once the manual stale-key purge was removed in
    # 2026.07.19.5 - a folder a user had to open to find one item, and that item is
    # plainly a backup operation. It sits LAST because it is a diagnostic on an
    # archive that already exists, not a primary action. Its Tools-era description,
    # kept verbatim because this select dialog has no plot slot to render it in:
    # "Read-only check of a backup zip: entry count, manifest, failed list, IPTV
    # data, top-level layout. Restores nothing."
    # "Settings" jumps straight to the Backup/Restore tab of the add-on settings
    # (archive location, backup mode, Dropbox), which is where every path setting
    # these three actions depend on lives. Without it she had to back out to the
    # root menu and find the settings button.
    #
    # Backup and Restore carry the folder they act on IN THE ROW LABEL, greyed.
    # The path she is about to write to or read from is on screen while she
    # chooses, on stock Estuary as much as on ours, and no skin needs to know
    # this add-on exists (see _menu_rows).
    #
    # This menu LOOPS. Presented once, any sub-action that ended - a cancelled file
    # picker, a dismissed verify report, a cancelled backup-mode dialog - fell off the
    # end of this branch, the script exited, and Kodi dropped the user at the ROOT
    # menu. To check a second archive she had to walk back in from the top. Now every
    # sub-action returns HERE.
    #
    # There are THREE ways out, and the two beyond "she cancelled the menu" exist
    # because a sub-action's return tells us nothing: every one of them returns None
    # whether it worked, cancelled, or handed the screen to another window.
    #
    #   1. s_type is not 0/1/2 - she cancelled this menu (or an unexpected value came
    #      back, which must never spin).
    #   2. A restore actually RAN (see the RESTORE branch).
    #   3. Kodi is shutting down, or a sub-action opened the Settings window
    #      (see _safe_to_re_present).
    #
    # JUDGEMENT CALL - looping after a COMPLETED backup/restore, not just after a
    # cancel. An earlier revision of this comment argued it was uniformly safe on the
    # grounds that `Quit` tears the script down before the loop can act. THAT WAS
    # FALSE and is corrected here: ui.restart() calls executebuiltin("Quit") WITHOUT
    # the wait flag (this codebase documents the blocking form as
    # `executebuiltin(..., True)`, wiz.py:867), so it returns immediately and this
    # Python outlives it. That is not a theory - defect A is precisely a
    # CApplication::Stop settings flush running after the add-on returned. So:
    #   * after a BACKUP, looping is safe. wiz.backup() never calls ask_restart and
    #     never quits; the box is unchanged and she may well want a second archive.
    #   * after a RESTORE, it is NOT safe, and the branch below breaks instead.
    while True:
        # Rebuilt every pass, deliberately: she may have just changed the folder
        # in the settings window, and rows built once outside the loop would go on
        # showing the old one. Cheap - two settings reads.
        typeOfBackup = _menu_rows(
            [
                ("Backup", _path_detail("download.path")),
                ("Restore", _path_detail("restore.path")),
                ("Verify Backup Archive", ""),
                ("Settings", ""),
            ]
        )
        s_type = control.selectDialog(typeOfBackup)
        # Sampled BEFORE the sub-action so _safe_to_re_present can tell "it bailed to
        # the settings window" from "the window has not painted yet" without racing
        # an async builtin. See _safe_to_re_present.
        _opened_before = control.open_settings_count()
        if s_type == 0:
            modes = ["Full Backup", "Addons Settings"]
            select = control.selectDialog(modes)
            if select == 0:
                wiz.backup(mode="full")
            elif select == 1:
                wiz.backup(mode="userdata")
            # select == -1: she backed out of the mode dialog. Fall through to the
            # top of the loop and re-present Backup/Restore - backing out of a
            # sub-dialog must never eject her all the way to the root menu.
        elif s_type == 1:
            # A restore that REACHED THE BOX ends this menu. Cleared first, then read
            # back: wiz.restore() publishes this Home-window property on every path
            # that got as far as touching the box, so its presence afterwards is a
            # reliable "a restore really ran here" - and there is no other signal,
            # since restoreFolder() returns None either way and wiz.py is a frozen
            # contract file this fix may not touch.
            #
            # Two independent reasons a restore must not come back to this menu:
            #   * Every terminal path of restore() ends in ui.ask_restart()
            #     (wiz.py:1725/1815/1818/1829). Accept it and Kodi is ALREADY tearing
            #     down behind this line (the async `Quit` above), so re-presenting
            #     would open a modal into a shutting-down message pump.
            #   * Decline it ("Later") and the box now carries restored files whose
            #     settings only land at the next clean shutdown. Offering BACKUP into
            #     that half-applied state is how you archive the pre-restore values -
            #     the kodi-settings-clobber class this project has four instances of.
            # A cancel at the file picker, the how-dialog, or the missing-zip-location
            # guard never reaches restore(), publishes nothing, and so DOES come back
            # to this menu. That is the owner's reported case and it still works.
            _clear_restore_verdict()
            wiz.restoreFolder()
            if _restore_verdict():
                break
        elif s_type == 2:
            VERIFY_BACKUP_ARCHIVE()
        elif s_type == 3:
            # She asked for the settings window, so this menu is done. Breaking here
            # rather than leaning on _safe_to_re_present: that probe is a backstop
            # for sub-actions that bail to settings on their own, and a deliberate
            # exit must not depend on a best-effort probe returning the right answer.
            control.openSettingsTab(control.SETTINGS_TAB_BACKUP_RESTORE)
            break
        else:
            break
        if not _safe_to_re_present(opened_before=_opened_before):
            break

elif action == "speedtest":
    xbmc.executebuiltin(
        'Runscript("special://home/addons/script.ezmaintenanceplusplus/resources/lib/modules/speedtest.py")'
    )

elif action == "authorize":
    # Also reachable as a plugin action (the Settings button uses RunScript -> the
    # sys.argv[1] guard above; this elif covers the plugin:// querystring path).
    from resources.lib.modules import dropbox_remote

    dropbox_remote.authorize()

elif action == "dbtest":
    from resources.lib.modules import dropbox_remote

    _dbtest(dropbox_remote)

elif action == "tools":
    # RETIRED: the Tools category is gone. Its last remaining item, "Verify backup
    # archive", now lives at the bottom of Backup/Restore where it belongs, so the
    # category was a folder wrapping a single backup action. Kept as an explicit
    # no-op so a stale favourite, widget or bookmark pointing at the old category
    # lands here instead of falling through to the unknown-action path. Deliberately
    # silent, same shape as the retired purge action below.
    pass

elif action == "purge_stale_tvos_keys":
    # RETIRED in 2026.07.19.5 (the purge runs automatically in restore, at boot
    # once per version, and in the two-layer wipe). Kept as an explicit no-op so a
    # stale favourite, widget or bookmark pointing at the old action lands here
    # instead of falling through to the unknown-action path. Deliberately silent:
    # nothing failed, and there is nothing the user needs to do.
    pass

elif action == "verify_backup_archive":
    VERIFY_BACKUP_ARCHIVE()

elif action in (
    "box_setup",
    "setup_all_box",
    "setup_sources",
    "setup_weather",
    "setup_rss",
    "device_name",
):
    # RETIRED 2026-07-22 with the "Set up this box" folder (see the note above
    # MAINTENANCE for what each one was and why it went). Explicit no-ops, same
    # shape as the retired tools/purge actions: a stale favourite, widget or
    # bookmark pointing at any of them lands here rather than falling through to
    # the unknown-action path. Deliberately silent - nothing failed, and there is
    # nothing the user needs to do.
    pass

xbmcplugin.endOfDirectory(int(sys.argv[1]))
