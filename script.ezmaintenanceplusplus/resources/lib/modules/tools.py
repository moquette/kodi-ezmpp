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
import os
import re
from resources.lib.modules.backtothefuture import unicode, PY2
from resources.lib.modules import ui

if PY2:
    translatePath = xbmc.translatePath
else:
    translatePath = xbmcvfs.translatePath

dialog = xbmcgui.Dialog()
addonInfo = xbmcaddon.Addon().getAddonInfo

AddonTitle = "EZ Maintenance++"
AddonID = "script.ezmaintenanceplusplus"


ADV_XML = "special://home/userdata/advancedsettings.xml"
# On Kodi 21 Omega the cache buffer lives in the GUI setting `filecache.memorysize` (in MB);
# advancedsettings.xml <cache> is DEPRECATED and IGNORED. So we read/write it via JSON-RPC,
# which is what actually takes effect. (Confirmed from the Omega source + v21 wiki.)
CACHE_SETTING = "filecache.memorysize"
KODI_DEFAULT_MB = 20  # Kodi's factory-default cache buffer

# Device name lives in the core setting `services.devicename` (Settings > Services > General).
# Set via JSON-RPC like the cache buffer; persisted both-ways (see _set_devicename).
DEVICENAME_SETTING = "services.devicename"
GUISETTINGS_XML = translatePath("special://home/userdata/guisettings.xml")


def _jsonrpc(method, params):
    import json

    try:
        resp = xbmc.executeJSONRPC(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        )
        return json.loads(resp)
    except Exception:
        return {}


def _get_cache_mb():
    """Kodi's current cache buffer in MB from the live GUI setting (the one Omega uses)."""
    r = _jsonrpc("Settings.GetSettingValue", {"setting": CACHE_SETTING})
    try:
        return int(r["result"]["value"])
    except Exception:
        return None


def _set_cache_mb(mb):
    """Set Kodi's cache buffer (MB) via the live GUI setting. Returns True on success."""
    r = _jsonrpc(
        "Settings.SetSettingValue", {"setting": CACHE_SETTING, "value": int(mb)}
    )
    return bool(r.get("result"))


def _get_devicename():
    """This box's current device name from the live core setting. '' if unavailable."""
    r = _jsonrpc("Settings.GetSettingValue", {"setting": DEVICENAME_SETTING})
    try:
        return r["result"]["value"]
    except Exception:
        return ""


def capture_device_identity():
    """This box's OWN identity settings, read LIVE before a restore overwrites them.

    A restore clones the SOURCE box's guisettings, which carries values describing the
    TARGET hardware that must not travel with an archive. Exactly ONE of them is
    PRESERVED and therefore captured here:

      * ``services.devicename`` - what this box is called on the network. A fresh
        Kodi install is already named "Kodi", so this box always HAS a name; there
        is no first-run case where the value is absent and something must be asked.

    ``filecache.memorysize`` is deliberately NOT captured. It is RESET to
    ``KODI_DEFAULT_MB`` by ``reset_cache_buffer`` rather than preserved (owner
    decision 2026-07-31: the fleet mixes device classes whose right buffer differs,
    so no inherited number survives a restore - not the archive's, and not this box's
    own previous one either). Capturing it would only hand the write-back loop an
    inherited number to smuggle past the reset.

    Returns ``{setting_id: value}`` holding only the values that could actually be
    read. A value that could not be read is OMITTED rather than defaulted, so the
    caller writes back nothing instead of writing back a guess. Never raises.

    Read from Kodi's LIVE settings, which is why this survives a wipe: the wipe
    removes files, not the running process's in-memory settings. Capturing before
    the extract is still the contract - once the archive's guisettings.xml is on
    disk, the box's own values exist nowhere else."""
    out = {}
    try:
        name = _get_devicename()
        if name:
            out[DEVICENAME_SETTING] = name
    except Exception:
        pass
    return out


def _set_devicename(name):
    """Set the device name durably on EVERY platform, then return True iff the live set took.

    Two persistence hazards, opposite per platform, so we do BOTH writes:
      - Settings.SetSettingValue updates Kodi's LIVE store. On tvOS that is the durable path
        (on tvOS the NSUserDefaults key SHADOWS guisettings.xml, so a file-only write is
        invisible to Kodi; nothing rewrites the file from the key).
      - write_guisetting() puts the value straight into guisettings.xml, which is what survives a
        Fire TV / Android UNCLEAN shutdown (there the live store only flushes to the file on a
        clean exit). On tvOS it is harmless same-value reinforcement.
    The live set is the authoritative in-session result; the file write is best-effort. On failure
    we log so a wrong setting id / rejected value is diagnosable from kodi.log."""
    r = _jsonrpc(
        "Settings.SetSettingValue", {"setting": DEVICENAME_SETTING, "value": name}
    )
    ok = bool(r.get("result"))
    if ok:
        try:
            from resources.lib.modules import _kodisettings

            _kodisettings.write_guisetting(GUISETTINGS_XML, DEVICENAME_SETTING, name)
        except Exception:
            pass
    else:
        try:
            xbmc.log(
                "ezmaintenanceplus: could not set %s to '%s' (JSON-RPC returned %r)"
                % (DEVICENAME_SETTING, name, r),
                level=xbmc.LOGWARNING,
            )
        except Exception:
            pass
    return ok


def _total_ram_mb():
    try:
        digits = re.sub("[^0-9]", "", xbmc.getInfoLabel("System.Memory(total)"))
        return int(digits) if digits else 0
    except Exception:
        return 0


# Kodi's OFFERED cache sizes (ServicesSettings.cpp). A value outside this list is
# honored at runtime, because CSettingInt::CheckValidity skips validation when a
# dynamic filler is present, BUT merely opening that screen in Kodi's own GUI can
# snap it to a listed value. The fleet ran 200 (Apple TV) and 165/166 (Fire TV),
# none of which are listed, so every box carried that hazard silently.
_KODI_CACHE_SIZES = (16, 20, 24, 32, 48, 64, 96, 128, 192, 256, 384, 512, 768, 1024)


def _snap_to_kodi_size(mb):
    """Largest OFFERED size not exceeding mb (never below the smallest offered).

    Rounding DOWN, not to nearest: the failure mode of too large is resident memory
    the OS cannot reclaim, and on tvOS an allocation failure is an uncatchable kill,
    while the failure mode of too small is a shorter buffer on a workload that
    already has far more depth than it needs."""
    below = [s for s in _KODI_CACHE_SIZES if s <= mb]
    return below[-1] if below else _KODI_CACHE_SIZES[0]


def _recommended_mb():
    """Recommend a cache buffer for THIS device, snapped to a size Kodi actually offers.

    Sized off TOTAL RAM deliberately: it is a constant, so the recommendation does not
    drift between boots the way free memory does.

    Known limits of this heuristic, recorded rather than pretended away:
      * On tvOS `System.Memory(total)` is `sysctl HW_MEMSIZE`, the DEVICE's physical
        RAM, which has no relationship to the per-app jetsam budget. Both Apple TV
        tiers compute above the ceiling and therefore land on it, so this is
        effectively a constant there, not a device-aware value.
      * The 10 percent factor and the 50/200 bounds were asserted when written, never
        derived from a measurement or a Kodi source citation.
      * Measured 2026-07-18 from a real jetsam report: Kodi's LIFETIME peak footprint
        on an Apple TV is about 69 MB, so a 200 MB buffer had never actually been
        allocated. Live IPTV never fills it either (a realtime stream is consumed as
        fast as it arrives); only debrid/HTTP VOD does.

    The snap is the part that matters and is safe to ship on its own: it removes the
    GUI hazard above without changing the sizing policy. Changing the 10 percent
    factor or the ceiling is an owner decision and is NOT made here."""
    total = _total_ram_mb()
    if total <= 0:
        return _snap_to_kodi_size(100)
    return _snap_to_kodi_size(max(50, min(200, int(total * 0.10))))


def _clean_stale_advancedsettings():
    """Best-effort removal of a stale advancedsettings.xml <cache> written by older versions
    (Omega ignores it, but it can confuse). Only deletes if it contains a cache block.

    Called ONLY from the "Reset to Kodi default" MENU action, and deliberately NOT from
    ``reset_cache_buffer`` on the restore / wipe paths. Three reasons, all load-bearing:

      * it cannot change the outcome. Omega reads the buffer from the GUI setting; the
        advancedsettings.xml <cache> block is deprecated and IGNORED, so deleting it
        moves nothing towards KODI_DEFAULT_MB.
      * it deletes the WHOLE FILE, not the <cache> block. advancedsettings.xml is a
        general Kodi tuning file (network timeouts, video settings, logging), so on a
        restore this would destroy archive-carried settings that have nothing to do
        with the cache - a restore silently deleting what it just restored.
      * on tvOS ``xbmcvfs.delete`` cannot delete a userdata xml at all: it drops the
        NSUserDefaults key, reports success and leaves the POSIX file. That is the
        one-layer asymmetry the backup/restore contract exists to forbid.

    In the menu the same three points do not apply: the user explicitly asked for
    Kodi's default, the file is his own rather than an archive's, and a stale <cache>
    block is exactly the confusion he is trying to clear."""
    try:
        if xbmcvfs.exists(ADV_XML):
            with xbmcvfs.File(ADV_XML) as f:
                data = f.read()
            if "<cache>" in data or "cachemembuffersize" in data:
                xbmcvfs.delete(ADV_XML)
    except Exception:
        pass


def reset_cache_buffer(guisettings_path=None, vector=True, log=None):
    """Land this box's video cache buffer on Kodi's OWN default (``KODI_DEFAULT_MB``).

    Owner decision 2026-07-31: a restore or a wipe leaves the box on Kodi's default.
    Not the archive's buffer, and NOT this box's own previous one either - the fleet
    mixes device classes whose right buffer differs, so no inherited number is allowed
    to survive. The per-device recommendation is then offered where it already lives,
    in ``advancedSettings()`` ("Use recommended for this device: N MB"), and the buffer
    only moves off the default when the user ACCEPTS that or types his own value.

    This function asks nothing and shows nothing. There is deliberately NO prompt: the
    post-restore popup was deleted 2026-07-19 and does not come back for this.

    THREE layers, because any one of them alone is silently reverted:

      * the LIVE store (``Settings.SetSettingValue``). Kodi's clean-shutdown flush
        serializes live memory over guisettings.xml, so a file-only reset is written
        straight back to the old value - the kodi-settings-clobber class, hardware
        proven. This is also the durable path on tvOS.
      * the FILE (guisettings.xml). The live store only reaches disk on a CLEAN exit,
        so on Fire TV / Android an unclean kill (power pull, task-swipe) would lose a
        live-only reset.
      * the tvOS VECTOR (``nsud.persist_one``). On Apple TV the NSUserDefaults key
        SHADOWS the disk file and Kodi never copies a key back, so a file-only reset is
        INVISIBLE there. Pass ``vector=False`` when the CALLER owns the single vector
        for this file - ``wiz._preserve_device_settings`` does, because it writes the
        device name into the same file and ``persist_one`` DROPS the POSIX copy on
        tvOS, so a vector taken here would strand that write.

    ``guisettings_path`` defaults to this box's own file; the restore passes the path it
    already resolved, because the module-level constant is frozen at import time.

    Never raises: every layer is independently guarded, because this runs mid-restore
    and mid-wipe where an exception would abort something that matters over a cosmetic
    setting. Returns ``{"live": bool, "file": bool, "before": int|None}`` so the caller
    can report truthfully - notably that a missing guisettings.xml (a wipe already
    removed it) is an EXPECTED ``file: False``, not a failure."""
    if guisettings_path is None:
        guisettings_path = GUISETTINGS_XML
    try:
        before = _get_cache_mb()
    except Exception:
        before = None
    try:
        live_ok = bool(_set_cache_mb(KODI_DEFAULT_MB))
    except Exception:
        live_ok = False
    try:
        from resources.lib.modules import _kodisettings

        file_ok = bool(
            _kodisettings.write_guisetting(
                guisettings_path, CACHE_SETTING, KODI_DEFAULT_MB
            )
        )
    except Exception:
        file_ok = False
    if vector:
        try:
            from resources.lib.modules import nsud

            nsud.persist_one("guisettings.xml", log=log)
        except Exception:
            pass
    if log:
        try:
            log(
                "cache-buffer: reset %s -> %d MB (live=%s file=%s)"
                % (
                    "unknown" if before is None else "%d MB" % before,
                    KODI_DEFAULT_MB,
                    live_ok,
                    file_ok,
                )
            )
        except Exception:
            pass
    return {"live": live_ok, "file": file_ok, "before": before}


def advancedSettings():
    current = _get_cache_mb()
    rec = _recommended_mb()
    cur_txt = "%d MB" % current if current is not None else "unknown"
    idx = dialog.select(
        "Video cache buffer  (current: %s . Kodi default %d MB)"
        % (cur_txt, KODI_DEFAULT_MB),
        [
            "Use recommended for this device:  %d MB" % rec,
            "Enter a value (MB)...",
            "Reset to Kodi default (%d MB)" % KODI_DEFAULT_MB,
        ],
    )
    if idx == -1:
        return  # cancel / back

    if idx == 0:
        mb = rec
    elif idx == 1:
        entered = _get_keyboard(
            default=str(rec),
            heading="Cache size in MEGABYTES (Cancel to abort)",
            cancel="-",
        )
        if not entered or entered == "-" or not str(entered).isdigit():
            return
        mb = int(entered)
        if mb > 400 and not ui.confirm(
            "%d MB is very large. Kodi buffers up to ~500 MB and a big buffer can make "
            "playback fail on low-memory devices. Use it anyway?" % mb,
            yeslabel="Use it",
            nolabel="Cancel",
        ):
            return
    else:  # Reset to Kodi default
        mb = KODI_DEFAULT_MB
        _clean_stale_advancedsettings()

    if _set_cache_mb(mb):
        ui.done(
            "Cache buffer set to %d MB.\n"
            "Applies to the next video you play - no restart needed." % mb
        )
    else:
        ui.error("Could not change the cache setting. Nothing was changed.")


# RETIRED 2026-07-22: deviceName(), the on-demand rename menu item, went with the
# "Set up this box" folder. It only wrote services.devicename, which Kodi itself
# exposes at Settings > Services > General on every box, so the add-on was
# carrying a second door to the same room.
#
# _get_devicename and _set_devicename below are NOT dead with it: they are the
# restore-preservation contract (capture this box's own name before the extract,
# write it back into the restored guisettings.xml), which is why a restore no
# longer clones the source box's name. Do not delete them chasing this comment.


# --------------------------------------------------------------------------- #
# There is NO post-restore prompt, and no marker driving one.
#
# A restore used to clone the SOURCE box's `services.devicename` and
# `filecache.memorysize`, then ask the user, on the first boot afterwards, to
# repair what it had just broken. Both questions were deleted (owner decision,
# 2026-07-19). `_kodisettings._BOOT_STATE_ONLY` stops the archive's values from
# being live-applied over this box's, and wiz._preserve_device_settings settles
# both before the restart - with two DIFFERENT resolutions:
#   * the device name is PRESERVED, captured live before the extract;
#   * the cache buffer is RESET to KODI_DEFAULT_MB (owner decision 2026-07-31),
#     because the fleet mixes device classes whose right buffer differs and no
#     inherited number may survive - not the archive's, not this box's own.
# Either way there is nothing left to ask, and STILL no prompt: the buffer's
# per-device recommendation is offered on demand under Video Cache Buffer.
#
# Why the questions were wrong, recorded so they are not reinvented:
#   * The buffer question was theatre. _recommended_mb() derives the value from
#     the box's own RAM, so the user could contribute nothing he knew better.
#     Both Apple TV tiers land on the ceiling, making it a constant there, and a
#     real jetsam report put Kodi's LIFETIME peak at about 69 MB, so the 200 MB
#     it offered had never been allocated. It remains available on demand:
#     Video Cache Buffer in the add-on's own menu (advancedSettings).
#   * The device-name question existed only to undo the clone. A fresh Kodi
#     install is ALREADY named "Kodi", so preserving covers every case including
#     a first-ever run; there is no first-run gap. Renaming on demand is a menu
#     item too (deviceName).
#
# Deleting the flow also deleted its whole failure surface: a boot-time modal
# that could be destroyed by anything owning the window stack, an attempt
# counter, a multi-boot marker, and the first-run arming that fired the same
# prompt on a brand-new box.
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Restore self-check marker (2026-07-17): a restore arms this so the boot
# service re-verifies the restored state on the next start - final certainty
# lives AFTER the restart, where the restored settings are actually live.
# SILENT on a clean pass (the box simply working is the message, log only);
# only a real finding speaks. Same location rules as the buffer marker: this
# add-on's own addon_data, which the wipe preserves and the extract precedes.
# --------------------------------------------------------------------------- #
RESTORE_CHECK_MARKER = translatePath(
    "special://home/userdata/addon_data/script.ezmaintenanceplusplus/.ezm_restore_check"
)


def mark_restore_check_pending(expected_skin=None):
    """Arm the post-restart restore self-check. Best-effort; never raises.

    ``expected_skin`` is the skin the ARCHIVE carries, recorded so the post-restart
    check can tell whether the box actually came up on it (defect A3). Without it
    the marker holds no expectation and the wrong-skin case is undetectable: the
    restore's own report cannot see the outcome, because the outcome happens after
    the restart.

    A3, bench-confirmed 2026-07-19: `_apply_boot_skin` writes lookandfeel.skin to
    disk, and Kodi's clean shutdown then serializes guisettings from LIVE memory
    over it (Application.cpp:2131, "Saving settings"), so a restore that CHANGES
    the skin reopens on the OLD one. Kodi offers no way to set the skin live
    without arming its 10-second keep-skin countdown, and any non-Yes - including a
    DESTROYED dialog - reverts (ApplicationSkinHandling.cpp:394-401), which is how
    atv2 was corrupted to stock on 2026-07-17. So this records the expectation and
    reports the mismatch; it does not attempt the switch."""
    try:
        d = os.path.dirname(RESTORE_CHECK_MARKER)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(RESTORE_CHECK_MARKER, "w") as f:
            # Legacy markers hold "1" and carry no expectation - see
            # restore_check_expected_skin, which must keep reading those.
            f.write(str(expected_skin or "1"))
        return True
    except Exception:
        return False


def restore_check_expected_skin():
    """The skin the restore expected the box to reopen on, or None.

    None means "no expectation recorded": either a legacy "1" marker or no marker.
    Callers MUST treat None as 'nothing to compare' and stay silent - never as a
    mismatch, or every legacy marker would report a false finding."""
    try:
        with open(RESTORE_CHECK_MARKER) as f:
            value = (f.read() or "").strip()
    except Exception:
        return None
    if not value or value == "1":
        return None
    return value


def restore_check_pending():
    """True iff a restore asked for a post-restart self-check. Never raises."""
    try:
        return os.path.exists(RESTORE_CHECK_MARKER)
    except Exception:
        return False


def clear_restore_check_marker():
    """Remove the marker so the check runs exactly once. Never raises."""
    try:
        if os.path.exists(RESTORE_CHECK_MARKER):
            os.remove(RESTORE_CHECK_MARKER)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# PVR pause crash-recovery marker. A restore that carries IPTV briefly DISABLES
# pvr.iptvsimple for the extract window (so its teardown flush cannot overwrite
# the restored instance settings) and re-enables it afterward. If the restore is
# interrupted between the two (a crash mid-extract, a power loss - PROVEN on a
# real Fire TV 2026-07-16, where a heavy merge-restore killed Kodi after the
# pause), the client would be left DISABLED forever. This marker records the
# outstanding pause; the boot service re-enables the client and clears it, so the
# pause can never strand the IPTV client past the next launch.
# --------------------------------------------------------------------------- #
PVR_PAUSE_MARKER = translatePath(
    "special://home/userdata/addon_data/script.ezmaintenanceplusplus/.ezm_pvr_paused"
)


def mark_pvr_paused():
    """Record that a restore disabled the IPTV client and owes a re-enable.
    Best-effort; never raises."""
    try:
        d = os.path.dirname(PVR_PAUSE_MARKER)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(PVR_PAUSE_MARKER, "w") as f:
            f.write("1")
        return True
    except Exception:
        return False


def pvr_pause_pending():
    """True iff a restore disabled the IPTV client and has not re-enabled it.
    Never raises."""
    try:
        return os.path.exists(PVR_PAUSE_MARKER)
    except Exception:
        return False


def clear_pvr_pause_marker():
    """Clear the outstanding-pause marker once the client is confirmed enabled.
    Never raises."""
    try:
        if os.path.exists(PVR_PAUSE_MARKER):
            os.remove(PVR_PAUSE_MARKER)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Settings-profile boot-check marker (2026-08-30). Applying the profile arms
# this so the boot service can confirm, after the restart, the one thing the
# apply flow cannot confirm in-session: the merged file manager sources, which
# Kodi only reads at startup (Files.GetSources returns the in-memory list, so
# an in-flow read-back of a correct write would report a false PARTIAL).
#
# Same location rules as the restore-check marker above, same reasons: a
# DOT-FILE in this add-on's own addon_data, never setSetting and never
# settings.xml (service.py documents why setSetting is unreliable for
# boot-time state), and nsud._should_vector leaves a non-settings dot-file
# alone on tvOS.
#
# The payload is JSON and is STAMPED with the box that wrote it (the MAC): a
# full backup captures everything under this add-on's addon_data except its
# own settings.xml, so a backup taken between apply and restart would
# otherwise make a SECOND box run this box's pending check. Excluding the
# marker from backups instead would contradict the "Full means full" contract
# line; the stamp needs no contract amendment. (The same exposure exists for
# RESTORE_CHECK_MARKER and PVR_PAUSE_MARKER; this declines to add a third,
# per the plan, rather than fixing those here.)
# --------------------------------------------------------------------------- #
PROFILE_CHECK_MARKER = translatePath(
    "special://home/userdata/addon_data/script.ezmaintenanceplusplus/.ezm_profile_check"
)


def mark_profile_check_pending(payload):
    """Arm the one-shot post-restart profile check. `payload` is a dict
    ({"box": mac, "sources": [...], "settings": {...}}). Returns True iff the
    marker landed; the CALLER logs loudly on False, because section 6.3
    promises the owner the sources take effect after the reopen and a silent
    failure here means nothing ever checks that promise. Never raises."""
    try:
        import json

        d = os.path.dirname(PROFILE_CHECK_MARKER)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(PROFILE_CHECK_MARKER, "w") as f:
            f.write(json.dumps(payload))
        return True
    except Exception:
        return False


def read_profile_check():
    """The armed payload dict, or None (no marker, or an unreadable one -
    which the caller treats as nothing owed, never as a finding)."""
    try:
        import json

        with open(PROFILE_CHECK_MARKER) as f:
            data = json.loads(f.read())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def profile_check_pending():
    """True iff an apply asked for a post-restart check. Never raises."""
    try:
        return os.path.exists(PROFILE_CHECK_MARKER)
    except Exception:
        return False


def clear_profile_check_marker():
    """Remove the marker so the check runs exactly once - and it is cleared
    REGARDLESS of outcome, or an owner who picked "Later", changed a setting
    by hand and rebooted next week would be nagged about a profile he
    deliberately moved away from (the read-only cousin of defect A3).
    Never raises."""
    try:
        if os.path.exists(PROFILE_CHECK_MARKER):
            os.remove(PROFILE_CHECK_MARKER)
    except Exception:
        pass


# NOTE: EZ Maintenance++ has NO IPTV behavior. The former post-restore IPTV auto-enable
# intent flag and the unattended boot gate that turned the IPTV client back on were REMOVED
# (they auto-enabled a client that crashed natively on a real box). A restore never touches,
# enables, disables, or stages the IPTV client; the user turns IPTV on deliberately.


def _keyboard_result(default="", heading="", hidden=False):
    """Show a keyboard and return (confirmed, text) WITHOUT collapsing a non-answer.

    Kodi's Python API cannot distinguish "user pressed Back" from "the dialog was
    destroyed under us": the two are INDISTINGUISHABLE, because both give isConfirmed()
    False with the typed text still in getText(). This is generic Kodi behaviour, not a
    property of any one skin or window - any Kodi
    component that owns the window stack can destroy a dialog
    (a skin reload, a window switch, a shutdown), and nothing in the API reports it.
    Do not claim to detect teardown. Callers must instead refuse to treat a
    non-answer as an answer: make "unconfirmed" mean "change nothing".

    Returning the text even when unconfirmed lets a caller re-present the dialog
    PREFILLED with whatever the user had already typed, so nothing is silently
    discarded. This is why the add-on never opens a dialog UNATTENDED at boot: a
    dialog nobody is watching cannot answer for itself, and an unanswerable
    question is a defect, not a feature."""
    confirmed = False
    text = ""
    try:
        keyboard = xbmc.Keyboard(default, heading, hidden)
        keyboard.doModal()
        try:
            confirmed = bool(keyboard.isConfirmed())
        except Exception:
            confirmed = False
        try:
            text = unicode(keyboard.getText() or "")
        except Exception:
            text = ""
    except Exception:
        return False, ""
    return confirmed, text


def _get_keyboard(default="", heading="", hidden=False, cancel=""):
    """shows a keyboard and returns a value

    Legacy contract, deliberately unchanged: returns ``cancel`` on any non-answer. The
    sentinel callers (wiz.py backup naming, cache size) rely on it. New callers that
    must not discard input should use _keyboard_result instead."""
    if cancel == "":
        cancel = default
    confirmed, text = _keyboard_result(default, heading, hidden)
    return text if confirmed else cancel


##############################    END    #########################################
