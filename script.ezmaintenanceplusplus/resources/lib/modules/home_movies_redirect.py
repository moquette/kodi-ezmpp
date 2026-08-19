# -*- coding: utf-8 -*-

"""Send Kodi's Home > Movies button straight to POV's Movies list.

WHAT THIS IS
------------
On stock Estuary the Home menu's "Movies" button is hardcoded in the skin's own
Home.xml, so its destination cannot be changed by any setting, any library node,
any sources.xml entry or any advancedsettings.xml rule. Measured against Kodi
22.0-BETA1 / skin.estuary 4.1.0:

  * Home.xml:982-984  Library.HasContent(movies)
                      -> ActivateWindow(Videos,videodb://movies/titles/,return)
  * Home.xml:985      !Library.HasContent(movies)
                      -> ActivateWindow(Videos,sources://video/,return)

Neither target is redirectable from outside the skin. Three stock mechanisms were
measured and rejected before this module was written:

  * The sources.xml <default> entry does NOT auto-enter a source in My Videos.
    Every caller of CMediaSourceSettings::GetDefaultSource is a non-video window
    (GUIWindowPrograms, GUIWindowGames, GUIWindowPictures, GUIWindowFileManager,
    GUIDialogContextMenu); the symbol appears nowhere in GUIMediaWindow,
    GUIWindowVideoBase or GUIWindowVideoNav.
  * Library nodes only define the library:// tree (LibraryDirectory.cpp:165-185).
    The videodb:// tree is built in C++ with no XML input
    (VideoDatabaseDirectory/DirectoryNode.cpp:93-142), and the empty-library
    branch goes to sources://, handled by CSourcesDirectory, which reads no node
    file at all.
  * advancedsettings.xml <pathsubstitution> would work mechanically, but it is
    global and unconditional: Home.xml:996 sends the TV Shows button to the SAME
    sources://video/, and Home.xml:361 uses it as a widget content_path, so one
    rule silently reroutes several unrelated things.

So the redirect has to be observed and corrected from outside, which is what this
does: watch for the Videos window landing on one of those targets and immediately
re-point the container at POV.

OFF BY DEFAULT, AND OFF MEANS OFF
---------------------------------
The homemenu.movies_redirect setting defaults to false. service.py reads that
setting DIRECTLY and only imports this module when it is true, so on a box that
never opts in this file is never imported, no thread is constructed, and nothing
here can affect the boot path that runs scheduled maintenance, the post-restore
check and the stale-key migration. That is deliberate: service.py has
start="startup" on every box in the fleet, and an import error here must not be
able to cost them that. enabled() is checked again inside start() and run() as
defence in depth, so calling this module directly with the setting off is also a
no-op that builds no thread and never enters a poll loop.

WHY THIS DOES NOT CONTRADICT "THIS SERVICE KNOWS NOTHING ABOUT ANY SKIN"
-----------------------------------------------------------------------
service.py carries a comment stating that this service knows nothing about any
skin, written after a skin coupling was deleted rather than renamed. That rule
stands, and this module is a deliberate, single exception to it. The two are not
in conflict, because they are not the same kind of coupling:

  * The DELETED coupling was a TIMING dependency. It waited out one skin's
    deferred menu rebuild before showing a dialog, and it was destructive when it
    was wrong: the rebuild ended in ReloadSkin() and destroyed the dialog, and
    Kodi's API cannot tell a destroyed dialog from a cancelled one. Being wrong
    produced an unanswerable prompt and a broken boot experience.

  * THIS coupling is READ-ONLY and FAILS SAFE. It reads one infolabel,
    Container(9000).ListItem.Property(id), and writes nothing to the skin. On any
    skin that has no control 9000, or whose items carry no "id" property, that
    infolabel returns an empty string, the latch below is never armed, and the
    redirect simply never fires. Being wrong produces exactly the behaviour of
    having the feature switched off.

Read-only, fail-safe, and opt-in is the whole difference. Do not delete this
module because service.py says the service knows nothing about skins, and do not
delete that comment because this module exists. Both are correct.
"""

import json
import threading

import xbmc
import xbmcaddon


# Keep this EQUAL to _REDIRECT_SETTING_ID in service.py. The two cannot share a
# constant, because service.py must be able to read the setting WITHOUT importing
# this module - that is the whole point of the lazy import.
SETTING_ID = "homemenu.movies_redirect"

# Byte-exact, owner-supplied. Do not "tidy" the parameter order: this is the URL
# POV itself builds for its own Movies row (menu_lists.py:2 root_list[0] fed
# through kodi_utils.py:320 build_url), so it is the value POV is known to accept.
# It contains no comma, which matters because Kodi's builtin parser splits
# arguments on commas. It also carries RAW ampersands - never XML-escape this
# constant; the &amp; form belongs only in a file Kodi parses as XML.
TARGET_URL = "plugin://plugin.video.pov/?name=32028&iconImage=movies.png&mode=navigator.main&action=MovieList"

POV_ADDON_ID = "plugin.video.pov"

# Stock Estuary's Home menu is <control type="fixedlist" id="9000"> (Home.xml:902)
# and its Movies row carries <property name="id">movies</property> (Home.xml:988).
# Any skin without both makes getInfoLabel return "", which disables the redirect
# rather than misfiring it.
HOME_MENU_CONTROL = 9000
HOME_ITEM_ID = "movies"

# Both branches of the skin's onclick, so the redirect does not silently stop
# working the day a box grows a real movie library. videodb://movies/ is the
# categories root used when the skin's home_no_movies_categories_widget setting is
# on; videodb://movies/titles/ is the flat list used otherwise.
REDIRECT_PATHS = (
    "sources://video/",
    "videodb://movies/titles/",
    "videodb://movies/",
)

# Idle at 1s, everywhere except inside the Videos window. The tight tick only runs
# while My Videos is on screen, which is a foreground interactive state a human is
# already looking at. A 150ms tick running 24/7 on a Firestick is not acceptable
# and is not what this does. The cost of that discipline is latency: the window can
# open up to IDLE_POLL after the click, and the stability rule below needs one more
# ACTIVE_POLL after that, so worst case the stock listing is visible for roughly
# 1.2s before the redirect lands. That is a deliberate trade, not an oversight.
IDLE_POLL = 1.0
ACTIVE_POLL = 0.15

# How often the running loop re-reads its own setting, so switching the feature
# off stops the thread without a Kodi restart. Deliberately not every tick: that
# would be a settings read up to seven times a second.
SETTING_RECHECK_SECONDS = 5.0

_ADDON = None
_THREAD = None


def _addon():
    global _ADDON
    if _ADDON is None:
        _ADDON = xbmcaddon.Addon()
    return _ADDON


def enabled():
    """True only when the user has explicitly opted in. Never raises."""
    try:
        return _addon().getSetting(SETTING_ID) == "true"
    except Exception:
        return False


def _cond(condition):
    """xbmc.getCondVisibility, hardened. A failure reads as False, which can only
    ever suppress the redirect, never trigger one."""
    try:
        return bool(xbmc.getCondVisibility(condition))
    except Exception:
        return False


def _label(infolabel):
    """xbmc.getInfoLabel, hardened. A failure reads as "", which is the same thing
    a skin without control 9000 returns: no latch, no redirect."""
    try:
        return xbmc.getInfoLabel(infolabel) or ""
    except Exception:
        return ""


def pov_available():
    """True only if plugin.video.pov is installed AND enabled on this box.

    Asked over JSON-RPC because it is the only probe that answers BOTH halves.
    System.HasAddon will not do: it is IsAddonInstalled (AddonsGUIInfo.cpp:263-266)
    and reads true for an add-on the user has switched off, which would send the
    user to a dead plugin path. Addons.GetAddonDetails returns the enabled flag
    itself, and answers with a JSON-RPC error rather than a result for an add-on
    Kodi does not know, so absent and disabled both land on False here.

    This ships to a fleet where not every box has POV. A box without it must no-op
    silently, not redirect the user onto an error dialog."""
    try:
        raw = xbmc.executeJSONRPC(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "Addons.GetAddonDetails",
                    "params": {
                        "addonid": POV_ADDON_ID,
                        "properties": ["enabled"],
                    },
                }
            )
        )
        return bool(json.loads(raw)["result"]["addon"]["enabled"])
    except Exception:
        return False


def _aborting(monitor):
    """True once Kodi has asked this service to stop. Never raises."""
    if monitor is None:
        return False
    try:
        return bool(monitor.abortRequested())
    except Exception:
        return False


class MoviesRedirect(object):
    """The state machine, kept free of threads and sleeps so it can be stepped one
    tick at a time in a test.

    Two guards matter and both are load-bearing:

    THE ONE-SHOT LATCH. Home's Movies and TV Shows buttons issue the IDENTICAL
    builtin on an empty library (Home.xml:985 and Home.xml:996 are the same
    string), so the destination alone cannot tell them apart. The latch keys on
    the focused Home item id, and is cleared the instant the redirect fires and
    the instant the Videos window closes. Without that clearing, a user who
    focused Movies, entered Videos, then browsed to Files > sources://video/ by
    hand would be hijacked by a stale flag.

    The latch is armed on any NON-EMPTY reading of the property, rather than only
    while the Home window still reports visible. That is not a weakening: on a
    real box Container(9000) resolves against the active window, so the property
    is non-empty only while Estuary's Home menu is the container being asked -
    the same guarantee, without depending on which side of the window switch the
    poll happens to land on. An empty reading, which is what every other skin and
    every other window returns, leaves the latch exactly as it was.

    THE STABILITY REQUIREMENT. CGUIMediaWindow::OnInitWindow posts a DEFERRED
    plugin refresh (GUIMediaWindow.cpp:1689, PLUGIN_REFRESH_DELAY), so a redirect
    fired the instant the window appears can be clobbered by the window's own
    update. The same target path must therefore be observed on two consecutive
    ticks, and Container.IsUpdating must be false, before anything is sent."""

    def __init__(self, monitor=None):
        self.monitor = monitor
        self.latched = False
        self.videos_was_visible = False
        self.pending_path = ""
        self.fired = 0

    def _fire(self):
        """Re-point the container. Sent through xbmc.executebuiltin on purpose:
        with wait=False it ends in CApplicationMessenger::PostMsg
        (ModuleXbmc.cpp:121-123), so the builtin is marshalled onto the
        application thread rather than run on this service thread, and
        Container.Update's SendMessage reaches the active window legally.
        'replace' resets the path history (GUIContainerBuiltins.cpp:102-110), so
        Back leaves the Videos window and returns to Home, which is where the user
        came from."""
        try:
            xbmc.executebuiltin("Container.Update(%s,replace)" % TARGET_URL)
            self.fired += 1
            return True
        except Exception:
            return False

    def tick(self):
        """Advance one poll step. Returns the seconds to wait before the next."""
        videos = _cond("Window.IsVisible(videos)")

        item_id = _label("Container(%d).ListItem.Property(id)" % HOME_MENU_CONTROL)
        if item_id == HOME_ITEM_ID:
            self.latched = True
        elif item_id:
            self.latched = False

        # Videos closed: the latch never outlives the window it was sampled for.
        if self.videos_was_visible and not videos:
            self.latched = False
            self.pending_path = ""
        self.videos_was_visible = videos

        if not videos:
            self.pending_path = ""
            return IDLE_POLL

        if self.latched:
            path = _label("Container.FolderPath")
            if path in REDIRECT_PATHS and not _cond("Container.IsUpdating"):
                if self.pending_path == path:
                    self._fire()
                    self.latched = False
                    self.pending_path = ""
                else:
                    self.pending_path = path
            else:
                self.pending_path = ""

        return ACTIVE_POLL


def run(monitor):
    """Poll until Kodi aborts or the setting is switched off. Never raises.

    Polling is not a shortcut taken for convenience: Kodi has no window-opened
    event to hang off. The only GUI announcements in the JSON-RPC schema are
    OnScreensaverActivated/Deactivated and OnDPMSActivated/Deactivated, and
    IAnnouncer.h has no window flag, so xbmc.Monitor.onNotification cannot see a
    window opening at all."""
    if not enabled():
        return

    if not pov_available():
        xbmc.log(
            "ezmaintenanceplus: home Movies redirect idle, %s absent or disabled"
            % POV_ADDON_ID,
            level=xbmc.LOGINFO,
        )
        return

    state = MoviesRedirect(monitor)
    since_check = 0.0
    while not _aborting(monitor):
        try:
            wait = state.tick()
        except Exception as e:
            xbmc.log(
                "ezmaintenanceplus: home Movies redirect tick failed %s: %s"
                % (type(e).__name__, e),
                level=xbmc.LOGWARNING,
            )
            wait = IDLE_POLL

        since_check += wait
        if since_check >= SETTING_RECHECK_SECONDS:
            since_check = 0.0
            if not enabled():
                return

        if monitor is None:
            return
        if monitor.waitForAbort(wait):
            return


def start(monitor):
    """Spawn the poller on a daemon thread, at most one at a time.

    Returns the thread, or None when the feature is off or one is already
    running. The setting is checked BEFORE the thread is constructed: off must
    mean no thread and no poll, not a cheap poll."""
    global _THREAD
    if not enabled():
        return None
    if _THREAD is not None and _THREAD.is_alive():
        return None
    thread = threading.Thread(
        target=run, args=(monitor,), name="ezm-home-movies-redirect"
    )
    thread.daemon = True
    thread.start()
    _THREAD = thread
    return thread
