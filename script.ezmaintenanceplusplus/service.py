import xbmc
import xbmcaddon
import xbmcgui
import json
import os
import xbmcvfs
import time
from resources.lib.modules.backtothefuture import PY2
from resources.lib.modules import maintenance

# Code to map the old translatePath
if PY2:
    translatePath = xbmc.translatePath
    loglevel = xbmc.LOGNOTICE
else:
    translatePath = xbmcvfs.translatePath
    loglevel = xbmc.LOGINFO

AddonID = "script.ezmaintenanceplusplus"
packagesdir = translatePath(os.path.join("special://home/addons/packages", ""))
thumbnails = translatePath("special://home/userdata/Thumbnails")
iconpath = translatePath(os.path.join("special://home/addons/" + AddonID, "icon.png"))


class Monitor(xbmc.Monitor):
    def __init__(self):
        xbmc.Monitor.__init__(self)
        maintenance.logMaintenance("Monitor init")
        maintenance.determineNextMaintenance()

    def onSettingsChanged(self):
        maintenance.logMaintenance("onSettingsChanged")
        maintenance.determineNextMaintenance()


# This service opens NO dialog at boot, and therefore knows nothing about any skin.
#
# It used to wait out a specific skin's deferred menu rebuild before showing the
# post-restore prompt, because that rebuild ended in ReloadSkin() and destroyed the
# dialog. The prompt is gone (a restore now PRESERVES this box's device name and
# cache buffer instead of asking the user to repair them), and with it the reason to
# know anything about the skin's internals. The wait, its timing constant, and its
# poll of skinshortcuts' in-progress flag were all deleted rather than renamed: an
# add-on that must time itself against a particular skin's behaviour is coupled to
# it, whatever the wait is called.
#
# Do not reintroduce a boot-time dialog here. A dialog nobody is watching cannot be
# answered, and Kodi's API cannot tell a destroyed dialog from a cancelled one, so an
# unattended boot prompt is unanswerable by construction. Boot work here must be
# silent and complete on its own.


# REMOVED 2026-07-22: _CONTRACT_FILES, _contract_fingerprint,
# _publish_contract_fingerprint and the ezm_contract_fingerprint window property.
# They existed for ONE reader, tools/verify_device.py, which published the hash of
# the developer's working tree while the box only ever asserted a hand-typed version
# string - so a box running an older build could produce a green artifact certifying
# code it had never run. That tool, its committed verification/*.json artifacts and
# the test that pinned the two file lists together were all DELETED on 2026-07-21
# with the rest of the device-verification gate. Nothing has read the property since.
# Computing a hash nobody reads, under a comment naming two files that no longer
# exist, is worse than nothing: it tells the next agent a gate is watching when none
# is. Do not reinstate it; if a gate is ever wanted again, it needs a reader first.
#
# _purge_stale_bytecode below is NOT part of that and stays. It has its own reason.


def _purge_stale_bytecode():
    """Delete this add-on's __pycache__ so what runs next start is the source on disk.

    CPython invalidates a .pyc by comparing the SOURCE's mtime AND size recorded in
    its header; it is not content-hashed. tools/build.py stamps every zip entry
    1980-01-01 for reproducible builds, so across builds the mtime half is a CONSTANT
    and staleness detection collapses onto size alone. Any edit that preserves byte
    length - a flipped comparison, a same-length constant, a reflowed docstring -
    leaves a stale .pyc valid, and the box goes on executing the OLD code after a
    correct upgrade landed the new source beside it. That is not theoretical: stale
    __pycache__ was observed on atv2 on 2026-07-19, and it is why a fix can appear to
    have failed on a box that has it.

    That stale __pycache__ could NOT be removed with devicectl. It can be removed from
    in here, because the add-on runs on the box with write access to its own
    directory. Best-effort and silent: a failure leaves the previous behaviour, never
    a broken start."""
    try:
        base = os.path.dirname(os.path.abspath(__file__))
        for root, dirs, _files in os.walk(base):
            for d in list(dirs):
                if d != "__pycache__":
                    continue
                target = os.path.join(root, d)
                for sub, _sd, sf in os.walk(target, topdown=False):
                    for name in sf:
                        try:
                            os.remove(os.path.join(sub, name))
                        except OSError:
                            pass
                    try:
                        os.rmdir(sub)
                    except OSError:
                        pass
                dirs.remove(d)
    except Exception:
        pass


def _startup_sequence(monitor):
    """The boot-time steps, in order, extracted so the ORDER is testable.

    This used to be inline under __main__, which meant nothing could assert it: a
    mutation wrapping a step in `if False:` passed the whole suite, and the only guard
    was a test checking that two statements were textually adjacent."""
    # Stale-key migration FIRST, so files a "vector everything" era left shadowed in
    # NSUserDefaults are visible on disk before the post-restore check below reads
    # them.
    _maybe_purge_stale_nsud_keys()
    _purge_stale_bytecode()
    _maybe_resume_paused_pvr()
    _maybe_restore_check(monitor)


def _wait_kodi_ready(monitor, timeout=120):
    """Block (interruptibly) until Kodi's GUI is actually up, so boot work never runs
    against a black boot screen. Returns False on abort; True once the home window is
    visible OR the bound is reached (well past any black-screen phase). Never raises."""
    waited = 0
    while waited < timeout:
        if monitor.abortRequested():
            return False
        try:
            if xbmc.getCondVisibility("Window.IsVisible(home)"):
                return True
        except Exception:
            pass
        if monitor.waitForAbort(2):
            return False
        waited += 2
    return True


def _folder_size_and_count(top, monitor=None):
    """Total byte size and file count of a tree. Per-file errors (a file deleted
    mid-scan) are skipped, never raised - this runs unattended at every boot.

    Abandons the walk on abort. packagesdir and Thumbnails are unbounded (a box
    that has never been cleaned is exactly the box this alert exists for), so an
    uninterruptible walk here is a second way to overrun Kodi's 5 second budget.
    A partial total is fine: the caller only ever compares it to a threshold, and
    an abandoned scan means the shutdown, not the alert, is what matters now."""
    total = 0
    count = 0
    for dirpath, dirnames, filenames in os.walk(top):
        if monitor is not None and monitor.abortRequested():
            break
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
                count += 1
            except OSError:
                pass
    return total, count


def _int_setting(setting, sid, default):
    try:
        return int(setting(sid))
    except (TypeError, ValueError):
        return default


def _aborting(monitor):
    """True once Kodi has asked this service to stop. Never raises."""
    if monitor is None:
        return False
    try:
        return bool(monitor.abortRequested())
    except Exception:
        return False


def _alert(message, seconds=8):
    """Non-blocking boot notification. Never blocks the service thread."""
    try:
        xbmc.executebuiltin(
            "Notification(%s, %s, %s, %s)"
            % ("EZ Maintenance++", message, seconds * 1000, iconpath)
        )
    except Exception:
        pass


def _startup_checks(monitor=None):
    """The one-shot boot checks: packages/thumbnails size alerts, the status
    notification, and the optional startup cache clean.

    Runs INSIDE the service after _wait_kodi_ready - NOT at import. At import time
    (Kodi startup) the two full-tree walks delayed every boot, and the modal yesno
    prompts could park a black boot screen; a "yes" then ran deleteThumbnails()
    synchronously in the boot path. Settings are read here, not at module scope, so
    a fresh boot sees current values.

    The two size alerts are NOTIFICATIONS, not prompts. They used to be modal
    Dialog().yesno() offers to clean now, and a modal opened on the service thread
    is the one thing in this file that Kodi's shutdown cannot survive: doModal()
    blocks the service thread, so the abort flag can never be polled, and Kodi
    kills the script 5 seconds later ("script didn't stop in 5 seconds - let's
    kill it"). Reproduced on the macOS bench 2026-07-20 with a 257 MB packages
    folder: abort 04:39:42.478, kill 04:39:47.490.

    A watchdog thread that closes the dialog on abort was tried and PROVEN not to
    work (bench, 2026-07-20). xbmc.executebuiltin("Dialog.Close(all, true)")
    queues a message to the application thread, and the application thread is the
    very thread blocked inside CPythonInvoker::stop() waiting for this script. The
    watchdog logged that it fired and that executebuiltin returned, and the kill
    still landed 5 seconds later. Do not re-propose it: it is a deadlock, not a
    tuning problem.

    So the alert reports the size and names where to clean it. This is the rule
    the file already states above about boot dialogs, applied to the one boot step
    that still escaped it. Every other step is gated on abort so an unbounded walk
    cannot overrun the budget either."""
    setting = xbmcaddon.Addon().getSetting
    notify_mode = setting("notify_mode")
    auto_clean = setting("startup.cache")
    # Fallbacks mirror the settings.xml schema defaults.
    filesize = _int_setting(setting, "filesize_alert", 200)
    filesize_thumb = _int_setting(setting, "filesizethumb_alert", 500)

    total_size, count = _folder_size_and_count(packagesdir, monitor)
    total_sizetext = "%.0f" % (total_size / 1024000.0)

    if _aborting(monitor):
        return
    if int(total_sizetext) > filesize:
        _alert(
            "Packages folder: %s MB in %s zip files. Clean it from "
            "EZ Maintenance++ > Maintenance." % (total_sizetext, count)
        )

    if _aborting(monitor):
        return
    total_size2, _ = _folder_size_and_count(thumbnails, monitor)
    total_sizetext2 = "%.0f" % (total_size2 / 1024000.0)

    if _aborting(monitor):
        return
    if int(total_sizetext2) > filesize_thumb:
        _alert(
            "Images folder: %s MB. Clean it from EZ Maintenance++ > Maintenance."
            % total_sizetext2
        )

    if _aborting(monitor):
        return
    if notify_mode == "true":
        xbmc.executebuiltin(
            "Notification(%s, %s, %s, %s)"
            % (
                "Maintenance Status",
                "Packages: " + str(total_sizetext) + " MB"
                " - Images: " + str(total_sizetext2) + " MB",
                "5000",
                iconpath,
            )
        )
    if auto_clean == "true" and not _aborting(monitor):
        maintenance.clearCache()


def _maybe_restore_check(monitor):
    """On the FIRST boot after a restore, re-verify the restored state now that it is
    actually LIVE (restorecheck's two-layer probes). SILENT on a clean pass - the box
    simply working is the message; the verdict goes to the log. Only a real finding
    speaks, with the locked needs-attention line. The marker is cleared either way so
    the check runs exactly once. Fully guarded: nothing here may block or crash the
    boot service; a normal boot (no marker) is a single os-stat no-op."""
    try:
        from resources.lib.modules import tools
    except Exception:
        return
    try:
        if not tools.restore_check_pending():
            return
    except Exception:
        return
    # Wait for the GUI OUTSIDE the try/finally: an aborted or interrupted boot must
    # NOT consume the one-shot marker (the check never ran, so it is still owed).
    if not _wait_kodi_ready(monitor):
        return
    try:
        from resources.lib.modules import restorecheck

        attention = []
        try:
            attention.extend(
                "%d duplicate two-layer listing(s): %s" % (len(d), ", ".join(d[:5]))
                for d in [restorecheck.duplicate_listing_hits()]
                if d
            )
        except Exception:
            pass
        # Defect A3: the restore wrote the archive's skin to disk, then Kodi's
        # shutdown flush serialized the PRE-restore skin from live memory over it,
        # so the box can reopen on the wrong skin. This is the ONLY place it is
        # observable - the restore itself finishes before the restart that decides
        # the outcome. getSkinDir() is the read-only probe; Skin.HasSetting /
        # GetInfoBooleans MUTATE (they insert a default-false setting and schedule
        # a save) and must never be used to check skin state.
        try:
            expected = tools.restore_check_expected_skin()
            if expected:
                live = (xbmc.getSkinDir() or "").strip()
                if live and live != expected:
                    attention.append(
                        "restored skin did not become live: expected %s, running %s "
                        "(the restored skin and its settings are installed and intact; "
                        "switch in Settings > Interface > Skin)" % (expected, live)
                    )
        except Exception:
            pass
        if attention:
            for line in attention:
                xbmc.log(
                    "%s : boot restore-check ATTENTION: %s" % (AddonID, line),
                    level=xbmc.LOGWARNING,
                )
            # Names an action the owner can actually take. The old text ("open EZ
            # Maintenance++") sent her to a menu whose only relevant entry was
            # "Purge stale tvOS keys" - jargon she could not be expected to
            # recognize, removed in 2026.07.19.5. Both actions named here re-run
            # the stale-key purge on their own, so the fix is reachable without
            # her ever knowing the word NSUserDefaults.
            xbmcgui.Dialog().notification(
                "EZ Maintenance++",
                "Your restore finished, but one setting may not have applied. "
                "Restore again, or restart the box.",
                time=8000,
            )
        else:
            xbmc.log(
                "%s : boot restore-check: restored state verified clean" % AddonID,
                level=xbmc.LOGINFO,
            )
    except Exception:
        pass
    finally:
        try:
            tools.clear_restore_check_marker()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# One-shot stale NSUserDefaults key migration (tvOS only).
#
# The 2026.07.08.2 - 2026.07.13.x releases vectored EVERY restored userdata xml
# into NSUserDefaults ("vector everything"). Boxes that ran them still hold keys
# for files current nsud policy deliberately leaves as plain POSIX (an add-on's
# private data), and on tvOS a key SHADOWS the disk file (CTVOSFile::Exists
# checks the key FIRST), so a freshly written or restored file can be silently
# invisible to Kodi forever. nsud.purge_stale_keys() materializes key-only files
# back to disk before purging the out-of-scope keys.
#
# The run-once marker is a FILE in this add-on's own addon_data, not a setSetting
# flag: a restore's extracted settings.xml plus Kodi's in-memory-settings clobber
# make setSetting unreliable for boot-time state. (The deleted post-restore prompt
# marker used the same pattern for the same reason; this and tools.RESTORE_CHECK_MARKER
# and tools.PVR_PAUSE_MARKER are the survivors.) It holds the add-on version the purge
# last ran for, so each upgrade gets exactly one purge and a normal boot is a single
# os-stat no-op.
# --------------------------------------------------------------------------- #
STALE_KEY_PURGE_MARKER = translatePath(
    "special://home/userdata/addon_data/" + AddonID + "/.ezm_stale_key_purge"
)


def _read_stale_purge_marker():
    """Version string the purge last completed for, '' if never. Never raises."""
    try:
        with open(STALE_KEY_PURGE_MARKER, "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def _write_stale_purge_marker(version):
    """Record the version the purge ran for. Best-effort; never raises."""
    try:
        d = os.path.dirname(STALE_KEY_PURGE_MARKER)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(STALE_KEY_PURGE_MARKER, "w") as f:
            f.write(version)
        return True
    except Exception:
        return False


def _maybe_purge_stale_nsud_keys():
    """Run nsud.purge_stale_keys(control.USERDATA) at most once per add-on version,
    and only on Apple TV (tvOS) - the only platform where NSUserDefaults exists.

    Fully guarded: any failure logs LOUDLY (LOGERROR) and returns; the boot
    service must never be crashed or blocked by this migration. On failure the
    marker is NOT written, so the next boot retries. If nsud predates
    purge_stale_keys (hasattr guard) this is a clean no-op that also leaves the
    marker unset, so the purge still happens once the capable nsud ships."""
    try:
        try:
            is_tvos = bool(xbmc.getCondVisibility("System.Platform.TVOS"))
        except Exception:
            is_tvos = False
        if not is_tvos:
            return
        try:
            version = xbmcaddon.Addon().getAddonInfo("version") or ""
        except Exception:
            version = ""
        if not version:
            # Without a version we cannot keep the once-per-version promise;
            # skip rather than risk running on every boot.
            return
        if _read_stale_purge_marker() == version:
            return
        from resources.lib.modules import control, nsud

        if not hasattr(nsud, "purge_stale_keys"):
            return  # older nsud: nothing to run; marker stays unset on purpose
        materialized, purged, kept, failed = nsud.purge_stale_keys(control.USERDATA)
        xbmc.log(
            "ezmaintenanceplus: stale NSUserDefaults key purge (v%s): "
            "%d materialized, %d purged, %d kept, %d failed"
            % (version, materialized, purged, kept, failed),
            level=loglevel,
        )
        # The marker is written ONLY on a proven-complete run. purge_stale_keys
        # never raises by design, so its failure modes are failed>0 (keys still
        # shadowing) and an all-zeros no-op (plist transiently unreadable at boot).
        # Burning the run-once marker on either would silently strand exactly the
        # boxes this migration exists for - so both retry next boot instead.
        if failed:
            xbmc.log(
                "ezmaintenanceplus: stale key purge left %d key(s) unresolved; "
                "marker not set, will retry next boot" % failed,
                level=xbmc.LOGWARNING,
            )
            return
        if not (materialized or purged or kept):
            xbmc.log(
                "ezmaintenanceplus: stale key purge saw an empty/unreadable store; "
                "marker not set, will retry next boot",
                level=xbmc.LOGWARNING,
            )
            return
        if not _write_stale_purge_marker(version):
            xbmc.log(
                "ezmaintenanceplus: stale key purge ran but its run-once marker "
                "could not be written; the purge may repeat next boot",
                level=xbmc.LOGWARNING,
            )
    except Exception as e:
        try:
            xbmc.log(
                "ezmaintenanceplus: stale NSUserDefaults key purge FAILED "
                "%s: %s (marker not set; will retry next boot)" % (type(e).__name__, e),
                level=xbmc.LOGERROR,
            )
        except Exception:
            pass


def _maybe_resume_paused_pvr():
    """If a restore's IPTV pause was left outstanding (interrupted before re-enable),
    re-enable pvr.iptvsimple and clear the marker. Fully guarded; never blocks boot.
    Only re-enables when the marker is present, so it never fights a user who
    deliberately disabled the client."""
    try:
        from resources.lib.modules import tools

        if not tools.pvr_pause_pending():
            return
        res = _jsonrpc_service(
            "Addons.SetAddonEnabled",
            {"addonid": "pvr.iptvsimple", "enabled": True},
        )
        if res == "OK":
            tools.clear_pvr_pause_marker()
            xbmc.log(
                "ezmaintenanceplus: re-enabled pvr.iptvsimple after an interrupted "
                "restore (crash-recovery); marker cleared",
                level=loglevel,
            )
        else:
            xbmc.log(
                "ezmaintenanceplus: could not re-enable pvr.iptvsimple on boot "
                "(will retry next boot)",
                level=xbmc.LOGWARNING,
            )
    except Exception as e:
        try:
            xbmc.log(
                "ezmaintenanceplus: PVR pause recovery failed %s: %s"
                % (type(e).__name__, e),
                level=xbmc.LOGWARNING,
            )
        except Exception:
            pass


def _jsonrpc_service(method, params):
    """One JSON-RPC call from the boot service; parsed 'result' or None."""
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


if __name__ == "__main__":
    monitor = Monitor()

    # NOTE: the boot-time home-root self-heal sweep and the unattended IPTV auto-enable gate
    # were REMOVED after 2026.07.08.4. The sweep deleted files at boot and the gate turned the
    # IPTV client back on by itself - both proved unsafe on a real box. Nothing at boot deletes
    # files or enables IPTV; the user turns IPTV on deliberately. The extract-root fix (a
    # restore puts files in the right folder) stands. The post-restore tune-up PROMPT that
    # used to run here was deleted on 2026-07-19: a restore now preserves this box's device
    # name and cache buffer, so nothing is asked at boot and nothing is asked at all.

    # PVR pause crash-recovery: if a restore disabled pvr.iptvsimple for its extract
    # window and was interrupted before re-enabling it (PROVEN possible on a real
    # Fire TV 2026-07-16, where a heavy restore killed Kodi mid-extract), the marker
    # is still set - re-enable the client and clear it so a restore can never strand
    # IPTV disabled past the next launch.
    _startup_sequence(monitor)

    if _wait_kodi_ready(monitor):
        try:
            _startup_checks(monitor)
        except Exception as e:
            # The alerts are best-effort; the maintenance loop below must start anyway.
            xbmc.log(
                "ezmaintenanceplus: startup checks failed %s: %s"
                % (type(e).__name__, e),
                level=xbmc.LOGWARNING,
            )

    while not monitor.abortRequested():
        # The auto-clean schedule is measured in days; a 60s tick is plenty.
        if monitor.waitForAbort(60):
            # Abort was requested while waiting. We should exit
            break
        if not xbmc.Player().isPlayingVideo():
            nextMaintenance = maintenance.getNextMaintenance()
            if (
                nextMaintenance > 0
                and time.time() >= nextMaintenance
                and not monitor.abortRequested()
            ):
                xbmc.log("ezmaintenanceplus: AutoClean started", level=loglevel)
                maintenance.clearCache()
                xbmc.log("ezmaintenanceplus: AutoClean done", level=loglevel)
                maintenance.determineNextMaintenance()

    del monitor
