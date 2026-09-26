# -*- coding: utf-8 -*-

"""
CONTROL ROUTINES

Only the surface the add-on actually uses. This module is imported at the top
of default.py, i.e. on EVERY plugin invocation - it must never instantiate
GUI objects (DialogProgress, WindowDialog, Player) at import time.
"""

import os

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs
from resources.lib.modules.backtothefuture import PY2
from resources.lib.modules import addon_id as _addon_id
from resources.lib.modules import ui


setting = xbmcaddon.Addon().getSetting

# Kept even though no shipped code path writes through it: the wiz restore test
# patches it as a tripwire proving restore() never re-stamps box-local settings.
setSetting = xbmcaddon.Addon().setSetting

addonInfo = xbmcaddon.Addon().getAddonInfo

dialog = xbmcgui.Dialog()

execute = xbmc.executebuiltin

if PY2:
    translatePath = xbmc.translatePath
else:
    translatePath = xbmcvfs.translatePath

AddonID = _addon_id.get()
# DIRECTORIES
USERDATA = translatePath(os.path.join("special://home/userdata", ""))
HOME = translatePath("special://home/")


def addonIcon():
    path = translatePath(os.path.join("special://home/addons/" + AddonID, "icon.png"))
    return path


def addonFanart():
    return translatePath(os.path.join("special://home/addons/" + AddonID, "fanart.jpg"))


def infoDialog(message, heading=ui.HEADING, icon="", time=None, sound=False):
    if time is None:
        time = 3000
    else:
        time = int(time)
    if icon == "":
        icon = addonIcon()
    elif icon == "INFO":
        icon = xbmcgui.NOTIFICATION_INFO
    elif icon == "WARNING":
        icon = xbmcgui.NOTIFICATION_WARNING
    elif icon == "ERROR":
        icon = xbmcgui.NOTIFICATION_ERROR
    dialog.notification(heading, message, icon, time, sound=sound)


def selectDialog(list, heading=ui.HEADING):
    """Kodi's select dialog, plain list.

    Deliberately NOT useDetails=True. The detailed view is the only way to get a
    real second line per row, but it reserves a thumbnail column and Kodi fills an
    artless row with DefaultAddonMore.png, putting a column of "+" glyphs down a
    backup menu. Callers that want extra text put it in the label instead - see
    _menu_rows in default.py.

    Note the argument order: Kodi takes (heading, list), this takes (list,
    heading). Do not pass a third positional argument through - Kodi's third is
    `autoclose`, in milliseconds."""
    return dialog.select(heading, list)


# How many times openSettings() has fired in THIS script run. Addon.OpenSettings is
# an ASYNC builtin: it returns long before the window exists, so "is the settings
# window active yet?" is a race, and the looping Backup/Restore menu in default.py
# used to lose it and drop a modal on top of the window the user had just been sent
# to. This counter is not a race: the call either happened or it did not. Every bail
# to the settings window goes through this one function (wiz.backup, wiz.restoreFolder
# and default.VERIFY_BACKUP_ARCHIVE all call it), so it is the honest chokepoint.
_open_settings_calls = 0


def open_settings_count():
    """How many times openSettings() has been called in this process."""
    return _open_settings_calls


def openSettings(query=None, id=None):
    global _open_settings_calls
    _open_settings_calls += 1
    try:
        if id is None:
            id = addonInfo("id")
        idle()
        execute("Addon.OpenSettings(%s)" % id)
        if query is None:
            raise Exception()
        c, f = query.split(".")
        execute("SetFocus(%i)" % (int(c) + 100))
        execute("SetFocus(%i)" % (int(f) + 200))
    except Exception:
        return


# Index of the "Backup/Restore" tab in resources/settings.xml, counted over the
# categories Kodi actually renders as buttons. Asserted by a test that models
# SettingSection::GetCategories, so reordering (or conditionally hiding) a category
# fails the suite rather than silently opening the wrong tab.
SETTINGS_TAB_BACKUP_RESTORE = 1


def openSettingsTab(index, timeout=5.0, poll=0.1):
    """Open this add-on's settings ON a given tab. Best effort by design.

    Every "you have not set a path yet" bail in this add-on ends here, and they all
    mean the same tab: wiz.backup (download.path), wiz.restoreFolder (restore.path)
    and default.VERIFY_BACKUP_ARCHIVE (restore.path). A plain openSettings() drops the
    user on the FIRST category, Maintenance, which is not where the path she was just
    told to set lives, and nothing on screen says which tab is. So the tab-aware open
    lives HERE, in the module all three already import, rather than in default.py
    where wiz could not reach it without inverting the dependency.

    Kodi 19+ builds the category buttons dynamically and gives them NEGATIVE control
    ids: GUIDialogSettingsBase.h defines CONTROL_SETTINGS_START_BUTTONS as -200, and
    SetupControls assigns `CONTROL_SETTINGS_START_BUTTONS + offset` in the order
    GetCategories returns, so tab N is control -200 + N. Focusing it makes the dialog
    rebuild the settings pane for that category, which is exactly the jump we want.
    (The 100/200 arithmetic in openSettings(query) above is Krypton-era and no longer
    addresses anything - do not copy it.)

    Both builtins are ASYNC, so SetFocus must not be fired until the dialog is
    actually up; otherwise it lands on whatever window is still on screen. If it never
    comes up, or a probe throws, the user is left on the settings window's first tab -
    one click from where she asked to be, never an error."""
    openSettings()
    try:
        monitor = xbmc.Monitor()
        waited = 0.0
        while waited < timeout:
            if xbmc.getCondVisibility("Window.IsActive(addonsettings)"):
                execute("SetFocus(%i)" % (-200 + index))
                return True
            if monitor.waitForAbort(poll):  # Kodi is shutting down
                return False
            waited += poll
    except Exception:
        pass
    return False


def idle():
    return execute("Dialog.Close(busydialog)")
