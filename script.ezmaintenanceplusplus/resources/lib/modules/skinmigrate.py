"""Skin migration: move a box from Estuary POV (``skin.estuary.pov``) to
Estuary++ (``skin.estuary.plusplus``) by itself, at boot, keeping the menu.

Owner decision 2026-09-26: the skin is renamed, and to Kodi a new add-on id
is a new add-on. Nothing upgrades across ids: the hub's new entry is not an
update of the old one, ``addon_data/<newid>/`` starts empty, and
``lookandfeel.skin`` stores the id string. So every box has to (a) install
the new skin, (b) carry its settings across, (c) switch, and (d) drop the old
one. This add-on's boot service is the one thing that runs on every box, so
it does those four, one log line per step, idempotent, and it does nothing
at all on a box that has stock Estuary or no POV skin.

The mechanisms, each chosen for a measured reason:

* **Install through Kodi's own installer** (the ``InstallAddon`` builtin,
  ``CAddonInstaller::InstallModal``), not by extracting the zip. The
  installer stamps ``installed.origin`` with the repository id
  (AddonInstaller.cpp, "Write origin to database via addon manager"), and a
  blank origin is what makes Kodi treat an add-on as hand-installed and
  never offer it an update (the update skill's 2026-07-21 measurement). The
  builtin asks Kodi's own "Would you like to download this add-on?" confirm
  (strings 24076/24100/24101) on the GUI thread; this module answers it the
  way the Settings Profile answers Kodi's confirm-gated settings: verify the
  dialog text is that exact string, walk focus to Yes, select. The install
  is modal (``ModalJob::CHOICE_YES``), so ``CSkinInfo::OnPostInstall`` never
  raises its own "switch to this skin?" prompt (Skin.cpp: ``!modal`` gates it).
  The builtin returns silently when the repository index does not list the
  id, so the id is resolved FIRST through ``Addons.GetAddons`` with
  ``installed=false`` (the JSON-RPC route to ``GetInstallableAddons``) and a
  box whose hub does not yet carry the new skin skips with one log line and
  tries again next start.
* **The settings copy is a plain byte copy** of
  ``addon_data/skin.estuary.pov/settings.xml`` to the new id's folder, read
  through the VFS (on tvOS that is the NSUserDefaults key, the layer Kodi
  reads), written atomically and persisted with ``nsud.persist_one`` so both
  tvOS layers agree. Only when the target is absent: the new skin's own file
  is never overwritten. The skin saves its settings to disk 500 ms after any
  change (Skin.cpp: ``CSkinSettingUpdateHandler::TriggerSave``, ``DELAY =
  500ms``), so the file copied is the live state. The skin's setting keys keep
  their ``pov_`` prefix, which is why the copy carries the arranged menu and
  the ``pov_menu_defaults`` guard across unchanged.
* **The switch is a live ``lookandfeel.skin`` set with Kodi's "Keep skin?"
  dialog answered Yes.** Kodi's callback posts ``ReloadSkin(confirm)``
  (ApplicationSkinHandling.cpp: ``OnSettingChanged``), the new skin loads,
  and ``ShowYesNoDialogText(13123, 13111, ..., 10000)`` arms a 10 second
  countdown whose every outcome but Yes reverts to the old skin, which is how
  atv2 was reverted to stock on 2026-07-17 when a restore let it time out.
  The dialog is posted by the GUI thread of this process, so it is
  answerable by ordinary input (measured for the profile 2026-08-30); the
  watcher polls every 300 ms against a 10 s window, and the verdict is read
  back from ``xbmc.getSkinDir()`` after the dialog is gone, never assumed. A
  miss is reported as an error and retried next start; Kodi's own revert
  leaves the box exactly where it was. The alternative, writing the setting
  in the shutdown window, was not chosen because it only takes effect at the
  next Kodi start, and an appliance can run for days between starts; the
  live switch lands the moment the release does.
* **Removal waits for the next start.** Kodi will not unload the active
  skin's files from under itself, so on a start where the active skin is
  already Estuary++ and the old skin is still installed, its directory (and
  its cached package zips) are removed and ``UpdateLocalAddons`` makes
  ``CAddonDatabase::SyncInstalled`` drop the row. ``addon_data/skin.estuary.pov/``
  is kept for now (a later release removes it once every box has moved).

Guards, each a rule: never while something is playing (the service retries
on its next idle tick); a setting switches the whole migration off
(``migrate_skin``, default on); the step is a silent no-op on a box that is
not on POV and has no POV installed, so it can ship before, with or after
the skin and never harm a box it does not apply to.

Restore: an archive that names the old skin restores onto the new one when
the new skin is installed (``map_restored_skin``), its skin settings are
copied to the new id's folder (``carry_restored_settings``), and the boot
check accepts either id (``same_skin``). Old archives keep restoring.
"""

import glob
import json
import os
import shutil
import stat
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.modules import nsud

OLD_SKIN = "skin.estuary.pov"
NEW_SKIN = "skin.estuary.plusplus"

# This add-on's own setting that switches the migration off. Unset reads as
# "" from getSetting, which is ON: only an explicit "false" disables it.
SETTING_ID = "migrate_skin"

# Kodi's own strings, matched by localized text so only KODI'S question about
# the thing this module just asked for is ever answered.
KEEP_SKIN_TEXT_ID = 13111  # "Would you like to keep this change?"
INSTALL_PROMPT_TEXT_ID = 24101  # "Would you like to download this add-on?"

APPLIED = "applied"
ALREADY = "already-correct"
SKIPPED = "skipped"
ERROR = "error"

# A 21 MB skin over a Fire TV's Tailscale link; generous, and the flow reads
# Kodi's own view of the add-on rather than trusting the builtin.
_INSTALL_TIMEOUT_S = 180.0
# The keep-skin countdown is 10 s; the reload itself takes a few seconds on
# an appliance before the dialog can appear.
_SWITCH_TIMEOUT_S = 30.0
_ENABLE_POLL_S = 10.0
_REMOVE_POLL_S = 10.0
_POLL_MS = 300

# Indirection so the tests can drive the clock; the module never sleeps
# through the real one for more than a poll.
_now = time.time


def _default_log(msg):
    xbmc.log("ezmaintenanceplus: skin migration: %s" % msg, level=xbmc.LOGINFO)


def _rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(req)))


def same_skin(a, b):
    """True when the two ids name the same skin across the rename: equal,
    or one is the old id and the other the new one."""
    a = (a or "").strip()
    b = (b or "").strip()
    if a == b:
        return True
    return {a, b} == {OLD_SKIN, NEW_SKIN}


def enabled_by_setting():
    """The migration is on unless this add-on's setting says "false"."""
    try:
        return (xbmcaddon.Addon().getSetting(SETTING_ID) or "").strip().lower() != "false"
    except Exception:
        return True


def is_playing():
    try:
        return bool(xbmc.Player().isPlaying())
    except Exception:
        return False


def live_skin():
    try:
        return (xbmc.getSkinDir() or "").strip()
    except Exception:
        return ""


def _addons_home():
    return xbmcvfs.translatePath("special://home/addons/")


def _skin_dir(aid):
    return os.path.join(_addons_home(), aid)


def known(aid):
    """(known, enabled) from Kodi's own view of an add-on id."""
    try:
        resp = _rpc(
            "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
        )
    except Exception:
        return False, False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False, False
    return True, bool(result.get("addon", {}).get("enabled"))


def hub_lists(aid):
    """True iff a repository Kodi has indexed offers `aid` (Addons.GetAddons
    with installed=false is the JSON-RPC route to GetInstallableAddons)."""
    try:
        resp = _rpc(
            "Addons.GetAddons",
            {
                "type": "xbmc.gui.skin",
                "installed": False,
                "enabled": "all",
                "properties": ["version"],
            },
        )
    except Exception:
        return False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False
    for addon in result.get("addons") or []:
        if isinstance(addon, dict) and addon.get("addonid") == aid:
            return True
    return False


def new_skin_present():
    """True iff the new skin is installed as far as Kodi or the disk knows.
    Used by the restore mapping, which may run before the boot step did."""
    if known(NEW_SKIN)[0]:
        return True
    try:
        return os.path.isfile(os.path.join(_skin_dir(NEW_SKIN), "addon.xml"))
    except Exception:
        return False


def map_restored_skin(target):
    """The skin a restore should assert for an archive naming `target`: the
    new id when the archive names the old one and the new skin is present,
    otherwise `target` unchanged (old archives keep restoring as they were)."""
    target = (target or "").strip()
    if target == OLD_SKIN and new_skin_present():
        return NEW_SKIN
    return target or None


# --------------------------------------------------------------------------- #
# Dialog answering (the profile's measured mechanism, module docstring)
# --------------------------------------------------------------------------- #
def _localized(sid):
    try:
        return (xbmc.getLocalizedString(sid) or "").strip()
    except Exception:
        return ""


def _yesno_showing(expected_text):
    """True iff Kodi's yes/no dialog is up and its text carries `expected_text`
    (the install prompt is three lines, the keep-skin dialog one)."""
    if not expected_text:
        return False
    try:
        if not xbmc.getCondVisibility("Window.IsActive(yesnodialog)"):
            return False
        shown = (xbmc.getInfoLabel("Control.GetLabel(9)") or "").strip()
    except Exception:
        return False
    return expected_text in shown


def _press_yes():
    """One step of the measured answer path: walk focus to the Yes button
    (control 11), then select. A missed step retries on the next poll."""
    try:
        if xbmc.getInfoLabel("System.CurrentControlId") == "11":
            xbmc.executebuiltin("Action(select)")
        else:
            xbmc.executebuiltin("Action(right)")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# The four steps
# --------------------------------------------------------------------------- #
def install_new_skin(log=None):
    """Install skin.estuary.plusplus through Kodi's installer so the origin
    row is the repository's. Returns (outcome, detail)."""
    log = log or _default_log
    is_known, enabled = known(NEW_SKIN)
    if is_known and enabled:
        return ALREADY, "%s is installed and enabled" % NEW_SKIN
    if is_known and not enabled:
        _rpc("Addons.SetAddonEnabled", {"addonid": NEW_SKIN, "enabled": True})
        deadline = _now() + _ENABLE_POLL_S
        while _now() < deadline:
            if known(NEW_SKIN)[1]:
                return APPLIED, "%s was installed but disabled; enabled" % NEW_SKIN
            xbmc.sleep(_POLL_MS)
        return ERROR, "%s is installed but would not enable" % NEW_SKIN
    if not hub_lists(NEW_SKIN):
        return SKIPPED, (
            "no repository offers %s yet; nothing touched, retried next start"
            % NEW_SKIN
        )
    prompt = _localized(INSTALL_PROMPT_TEXT_ID)
    box = {}

    def worker():
        # The builtin is posted to the GUI thread (executebuiltin without
        # wait), so this returns at once; the thread only keeps the call off
        # the watcher's own path.
        try:
            xbmc.executebuiltin("InstallAddon(%s)" % NEW_SKIN)
        except Exception as e:  # noqa: BLE001 - reported below
            box["err"] = "%s: %s" % (type(e).__name__, e)

    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    t.join(2.0)
    answered = False
    deadline = _now() + _INSTALL_TIMEOUT_S
    while _now() < deadline:
        if _yesno_showing(prompt):
            _press_yes()
            answered = True
        else:
            is_known, enabled = known(NEW_SKIN)
            if is_known and enabled:
                return APPLIED, "%s installed from the repository%s" % (
                    NEW_SKIN,
                    " (download prompt answered)" if answered else "",
                )
            if is_known and not enabled:
                _rpc("Addons.SetAddonEnabled", {"addonid": NEW_SKIN, "enabled": True})
        xbmc.sleep(_POLL_MS)
    if box.get("err"):
        return ERROR, "InstallAddon failed: %s" % box["err"]
    return ERROR, "%s not installed within %ds (prompt %s)" % (
        NEW_SKIN,
        int(_INSTALL_TIMEOUT_S),
        "answered" if answered else "never seen",
    )


def _read_special(special):
    try:
        f = xbmcvfs.File(special)
        try:
            data = f.readBytes()
        finally:
            f.close()
        return bytes(data) if data else b""
    except Exception:
        return b""


def _write_userdata_file(rel, data, log):
    """Atomic POSIX write of a userdata-relative file, then the one
    sanctioned persist (nsud.persist_one) so both tvOS layers agree. Returns
    True iff the bytes are durably on the box."""
    target = xbmcvfs.translatePath("special://profile/" + rel)
    d = os.path.dirname(target)
    if not os.path.isdir(d):
        os.makedirs(d)
    mode = None
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        pass
    tmp = target + ".ezm-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
    os.replace(tmp, target)
    persisted = nsud.persist_one(rel, log=log)
    if not persisted and nsud._is_tvos():
        log("%s: tvOS vector unconfirmed (POSIX copy stands)" % rel)
        return False
    return True


def carry_settings(log=None):
    """Copy the old skin's settings.xml to the new id's folder, only when the
    new one is absent. Returns (outcome, detail)."""
    log = log or _default_log
    new_rel = "addon_data/%s/settings.xml" % NEW_SKIN
    old_rel = "addon_data/%s/settings.xml" % OLD_SKIN
    new_special = "special://profile/" + new_rel
    try:
        if xbmcvfs.exists(new_special):
            return ALREADY, "%s already has its own settings.xml" % NEW_SKIN
    except Exception:
        pass
    raw = _read_special("special://profile/" + old_rel)
    if not raw:
        return SKIPPED, "%s has no settings.xml to carry across" % OLD_SKIN
    try:
        ok = _write_userdata_file(new_rel, raw, log)
    except Exception as e:  # noqa: BLE001 - reported, never raises into boot
        return ERROR, "settings copy failed (%s: %s)" % (type(e).__name__, e)
    if not ok:
        return ERROR, "settings copied but not durably persisted on tvOS"
    return APPLIED, "settings.xml carried across (%d bytes)" % len(raw)


def switch(log=None):
    """Set lookandfeel.skin live and answer Kodi's keep-skin dialog Yes.
    Returns (outcome, detail); the verdict is the live skin read back."""
    log = log or _default_log
    if live_skin() == NEW_SKIN:
        return ALREADY, "%s is the active skin" % NEW_SKIN
    is_known, enabled = known(NEW_SKIN)
    if not (is_known and enabled):
        return ERROR, "%s is not installed and enabled; not switching" % NEW_SKIN
    keep = _localized(KEEP_SKIN_TEXT_ID)
    try:
        resp = _rpc(
            "Settings.SetSettingValue",
            {"setting": "lookandfeel.skin", "value": NEW_SKIN},
        )
    except Exception as e:  # noqa: BLE001 - reported
        return ERROR, "SetSettingValue raised %s: %s" % (type(e).__name__, e)
    if resp.get("result") is not True:
        return ERROR, "SetSettingValue returned %r" % (
            resp.get("error") or resp.get("result"),
        )
    seen = False
    deadline = _now() + _SWITCH_TIMEOUT_S
    while _now() < deadline:
        if _yesno_showing(keep):
            seen = True
            _press_yes()
        elif seen:
            break
        xbmc.sleep(_POLL_MS)
    # Let the reload settle, then read the truth back: after a revert Kodi
    # has put the old id back in the setting.
    settle = _now() + 5.0
    live = live_skin()
    while live != NEW_SKIN and _now() < settle:
        xbmc.sleep(_POLL_MS)
        live = live_skin()
    if live == NEW_SKIN:
        return APPLIED, "switched to %s (keep-skin dialog %s)" % (
            NEW_SKIN,
            "answered yes" if seen else "not observed",
        )
    return ERROR, (
        "switch to %s did not hold (live skin %s; keep-skin dialog %s); "
        "retried next start" % (NEW_SKIN, live or "?", "answered" if seen else "not seen")
    )


def remove_old_skin(log=None):
    """Remove the old skin's files and cached packages, then make Kodi drop
    its row. Only when the new skin is the active one. Returns
    (outcome, detail)."""
    log = log or _default_log
    if live_skin() != NEW_SKIN:
        return SKIPPED, "%s is not the active skin; old skin kept" % NEW_SKIN
    old_dir = _skin_dir(OLD_SKIN)
    had_dir = os.path.isdir(old_dir)
    was_known = known(OLD_SKIN)[0]
    if not had_dir and not was_known:
        return ALREADY, "%s already gone" % OLD_SKIN
    removed = []
    if had_dir:
        shutil.rmtree(old_dir)
        removed.append("directory")
    for z in glob.glob(os.path.join(_addons_home(), "packages", OLD_SKIN + "-*.zip")):
        try:
            os.remove(z)
            removed.append(os.path.basename(z))
        except OSError:
            pass
    if was_known:
        xbmc.executebuiltin("UpdateLocalAddons")
        deadline = _now() + _REMOVE_POLL_S
        while _now() < deadline:
            if not known(OLD_SKIN)[0]:
                return APPLIED, "%s removed (%s) and dropped from the add-on database" % (
                    OLD_SKIN,
                    ", ".join(removed) or "no files",
                )
            xbmc.sleep(_POLL_MS)
        return ERROR, "%s files removed (%s) but Kodi still lists it; retried next start" % (
            OLD_SKIN,
            ", ".join(removed),
        )
    return APPLIED, "%s files removed (%s)" % (OLD_SKIN, ", ".join(removed))


# --------------------------------------------------------------------------- #
# The boot step
# --------------------------------------------------------------------------- #
def run(log=None, playing=None):
    """The step. Returns a dict:

        outcome  applied | already-correct | skipped | error
        detail   one line for the log
        steps    [(name, outcome, detail)] of what ran this time

    One log line per step that did something; silent when nothing is owed.
    Never raises."""
    log = log or _default_log
    result = {"outcome": ALREADY, "detail": "", "steps": []}

    def step(name, outcome, detail):
        result["steps"].append((name, outcome, detail))
        if detail:
            log("%s: %s -> %s" % (name, outcome, detail))

    try:
        if not enabled_by_setting():
            result["outcome"] = SKIPPED
            result["detail"] = "disabled by the %s setting" % SETTING_ID
            log(result["detail"])
            return result
        live = live_skin()
        old_known = known(OLD_SKIN)[0]
        old_dir = os.path.isdir(_skin_dir(OLD_SKIN))
        new_known = known(NEW_SKIN)[0]
        if live == NEW_SKIN:
            if not (old_known or old_dir):
                return result  # migrated and clean: nothing owed, nothing said
            if playing is None:
                playing = is_playing()
            if playing:
                result["outcome"] = SKIPPED
                result["detail"] = "deferred: something is playing"
                log(result["detail"])
                return result
            outcome, detail = remove_old_skin(log)
            step("remove old skin", outcome, detail)
            result["outcome"] = outcome
            result["detail"] = detail
            return result
        if live != OLD_SKIN and not (old_known and not new_known):
            return result  # stock Estuary or no POV at all: not our box
        if playing is None:
            playing = is_playing()
        if playing:
            result["outcome"] = SKIPPED
            result["detail"] = "deferred: something is playing"
            log(result["detail"])
            return result
        outcome, detail = install_new_skin(log)
        step("install", outcome, detail)
        if outcome in (SKIPPED, ERROR):
            result["outcome"] = outcome
            result["detail"] = detail
            return result
        outcome, detail = carry_settings(log)
        step("settings", outcome, detail)
        if outcome == ERROR:
            # Switching without the menu would seed the new skin's defaults
            # over a copy that could never land afterwards; stop here.
            result["outcome"] = ERROR
            result["detail"] = detail
            return result
        if live == OLD_SKIN:
            outcome, detail = switch(log)
            step("switch", outcome, detail)
            if outcome == ERROR:
                result["outcome"] = ERROR
                result["detail"] = detail
                return result
        result["outcome"] = APPLIED
        result["detail"] = "; ".join(d for _n, _o, d in result["steps"] if d)
        return result
    except Exception as e:  # noqa: BLE001 - never disturb boot
        result["outcome"] = ERROR
        result["detail"] = "%s: %s" % (type(e).__name__, e)
        try:
            log(result["detail"])
        except Exception:
            pass
        return result


# --------------------------------------------------------------------------- #
# Restore
# --------------------------------------------------------------------------- #
def carry_restored_settings(userdata, log=None):
    """Inside a restore that maps the old id to the new one: copy the archive's
    just-extracted addon_data/<old>/settings.xml over the new id's file, so
    the restored menu lands where the new skin reads it. A plain POSIX copy
    at the extract stage, deliberately: the restore's own
    nsud.rewrite_userdata_xml pass runs after this and vectors it on tvOS
    exactly as it vectors the extracted file itself, and a persist here
    would drop the POSIX copy that wiz reads next to re-apply the values
    live. Returns True iff the file was copied."""
    log = log or _default_log
    src = os.path.join(userdata, "addon_data", OLD_SKIN, "settings.xml")
    dst = os.path.join(userdata, "addon_data", NEW_SKIN, "settings.xml")
    try:
        if not os.path.isfile(src):
            return False
        d = os.path.dirname(dst)
        if not os.path.isdir(d):
            os.makedirs(d)
        with open(src, "rb") as fh:
            data = fh.read()
        tmp = dst + ".ezm-tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dst)
        log("restore: %s settings carried to %s (%d bytes)" % (OLD_SKIN, NEW_SKIN, len(data)))
        return True
    except Exception as e:  # noqa: BLE001 - reported, never breaks a restore
        try:
            log("restore: could not carry %s settings to %s (%s)" % (OLD_SKIN, NEW_SKIN, e))
        except Exception:
            pass
        return False
