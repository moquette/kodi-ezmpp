# -*- coding: utf-8 -*-
"""Home > Movies -> POV redirect: the safety net, not the feature.

The feature is small. What can go wrong with it is not, and every case here
comes from something this tree has already paid for.

  * OFF MUST MEAN OFF, AND OFF LIVES IN service.py. The module is only imported
    when the setting is true, so the off-switch is not a branch inside the
    module, it is the absence of the import. service.py has start="startup" on
    every box in the fleet, so a module-level import would let a syntax error in
    the redirect take down scheduled maintenance, the post-restore check and the
    stale-key migration on boxes that never opted in. Default-OFF does not
    protect against that. A lazy import does, and the AST guards below assert it
    mechanically rather than trusting a comment.

  * Container.Update fired from a polling service is exactly the shape that
    looped the Estuary 8 widget picker for a full day. The re-entrancy test
    models Kodi for real: the fake executebuiltin MOVES the container, so after
    the redirect fires, Container.FolderPath really is the POV URL. A naive
    implementation re-fires on its own result forever.

  * The URL is the owner's, pasted verbatim, and is asserted BYTE-EQUAL. Not
    "contains pov", not "startswith plugin://". A dropped & or an XML-escaped
    &amp; is a silently broken menu item.

  * Home's Movies and TV Shows buttons emit the IDENTICAL builtin on an empty
    library (measured: skin.estuary 4.1.0 Home.xml:985 and Home.xml:996 are the
    same string), so the destination alone cannot discriminate. The latch is the
    only thing that can, and half these tests exist to prove it does not leak.

  * The loop must sleep on monitor.waitForAbort(), never xbmc.sleep(), or Kodi
    hangs on shutdown waiting for a service that is not listening.

The harness models the WORLD (which window is active, which folder is open,
which Home item is focused, whether POV is installed and enabled) rather than a
particular query, and drives the shipped state machine one tick at a time
through the seam its own docstring offers.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import threading
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parent / "script.ezmaintenanceplusplus"
MODULES_DIR = ADDON_ROOT / "resources" / "lib" / "modules"
SERVICE_PY = ADDON_ROOT / "service.py"
SETTINGS_XML = ADDON_ROOT / "resources" / "settings.xml"

# The owner pasted this. It is the contract. Do not "tidy" it.
TARGET_URL = (
    "plugin://plugin.video.pov/"
    "?name=32028&iconImage=movies.png&mode=navigator.main&action=MovieList"
)
POV_ADDON_ID = "plugin.video.pov"

# Stock Estuary's Movies branches, measured on the office Fire TV against
# Kodi 22.0-BETA1 / skin.estuary 4.1.0 Home.xml:982-985. The sources branch
# fires when the movie library is empty, the videodb ones when it is not.
STOCK_EMPTY_LIBRARY = "sources://video/"
STOCK_WITH_LIBRARY = "videodb://movies/titles/"
STOCK_CATEGORIES = "videodb://movies/"
ALL_STOCK_TARGETS = (STOCK_EMPTY_LIBRARY, STOCK_WITH_LIBRARY, STOCK_CATEGORIES)

CANDIDATE_MODULES = (
    "home_movies_redirect",
    "homeredirect",
    "home_redirect",
    "homemovies",
    "home_movies",
    "povredirect",
    "pov_redirect",
    "moviesredirect",
)


def _module_path():
    """Prefer the agreed name, else find the module carrying the POV target.

    Discovery rather than a hard-coded name, so the suite binds to the shipped
    module even if it is renamed, while still failing loudly if none exists.
    """
    for name in CANDIDATE_MODULES:
        p = MODULES_DIR / (name + ".py")
        if p.exists():
            return p
    hits = sorted(
        p
        for p in MODULES_DIR.glob("*.py")
        if POV_ADDON_ID in p.read_text(encoding="utf-8", errors="replace")
    )
    return hits[0] if hits else None


MODULE_PATH = _module_path()


# Deliberately NO module-level skipif. A missing feature module must turn this
# suite RED, not green-with-skips. A skipped safety net reads exactly like a
# passing one on a CI summary line, and that is how a partial ships.
def test_redirect_module_exists():
    assert MODULE_PATH is not None, (
        "No Home > Movies redirect module found in %s. Expected one of %s.py, or "
        "any module referencing %s." % (MODULES_DIR, CANDIDATE_MODULES, POV_ADDON_ID)
    )


# --------------------------------------------------------------------------- #
# The world the module runs in
# --------------------------------------------------------------------------- #
class _Monitor:
    """xbmc.Monitor stand-in that aborts after N waits.

    waitForAbort(secs) returning True is Kodi's real "stop now" contract and the
    only sanctioned way for a service to sleep.
    """

    def __init__(self, ticks=3):
        self._left = int(ticks)
        self.waits = []

    def waitForAbort(self, secs=0):
        self.waits.append(secs)
        self._left -= 1
        return self._left <= 0

    def abortRequested(self):
        return self._left <= 0


class _World:
    def __init__(self):
        # observable GUI state
        self.window = "Home"
        self.folder_path = ""
        self.home_item_id = ""
        self.container_updating = False
        # box state
        self.pov_installed = True
        self.pov_enabled = True
        self.setting_on = True
        # recordings
        self.builtins = []
        self.jsonrpc = []
        self.logs = []
        self.sleeps = []
        self.threads = []

    # -- info labels -------------------------------------------------------- #
    def info_label(self, label):
        low = str(label).lower()
        if "folderpath" in low:
            return self.folder_path
        if "property(id)" in low:
            # MEASURED on the office Fire TV (Kodi 22.0-BETA1, skin.estuary
            # 4.1.0): while the Videos window is active, Container(9000) reports
            # an EMPTY string, not the item that was focused on Home. Modelling
            # that is load-bearing. A harness that keeps reporting "movies"
            # inside Videos hides both a hijack path and a missed redirect.
            return self.home_item_id if self.window == "Home" else ""
        if "foldername" in low:
            return self.folder_path.rstrip("/").rsplit("/", 1)[-1]
        return ""

    # -- boolean conditions ------------------------------------------------- #
    def cond(self, condition):
        low = str(condition).lower()
        if "window.isactive(home)" in low or "window.isvisible(home)" in low:
            return self.window == "Home"
        if "window.isactive(videos)" in low or "window.isvisible(videos)" in low:
            return self.window == "Videos"
        if "container.isupdating" in low:
            return self.container_updating
        if "hasaddon" in low:
            return POV_ADDON_ID in str(condition) and self.pov_installed
        return False

    # -- JSON-RPC ----------------------------------------------------------- #
    def execute_jsonrpc(self, payload):
        self.jsonrpc.append(str(payload))
        try:
            req = json.loads(payload)
        except Exception:
            return "{}"
        if req.get("method") == "Addons.GetAddonDetails":
            addonid = req.get("params", {}).get("addonid")
            if addonid == POV_ADDON_ID and self.pov_installed:
                return json.dumps(
                    {
                        "id": 1,
                        "jsonrpc": "2.0",
                        "result": {
                            "addon": {
                                "addonid": addonid,
                                "enabled": bool(self.pov_enabled),
                            }
                        },
                    }
                )
            # Kodi's real shape for an add-on it does not know.
            return json.dumps(
                {
                    "id": 1,
                    "jsonrpc": "2.0",
                    "error": {"code": -32602, "message": "Invalid params."},
                }
            )
        return "{}"

    # -- actions ------------------------------------------------------------ #
    def executebuiltin(self, command, *a, **k):
        cmd = str(command)
        self.builtins.append(cmd)
        # Model Kodi for real: Container.Update MOVES the container, so the very
        # next poll sees the destination as the current folder. This is what
        # makes the re-entrancy test mean anything.
        if cmd.startswith("Container.Update"):
            inner = cmd[len("Container.Update") :].strip()
            if inner.startswith("(") and inner.endswith(")"):
                inner = inner[1:-1]
            if inner.endswith(",replace"):
                inner = inner[: -len(",replace")]
            self.folder_path = inner.strip()
            self.window = "Videos"
        return None

    # -- convenience -------------------------------------------------------- #
    def updates(self):
        return [c for c in self.builtins if c.startswith("Container.Update")]

    def on_home(self, item_id):
        self.window = "Home"
        self.home_item_id = item_id
        self.folder_path = ""

    def enter_videos(self, path):
        self.window = "Videos"
        self.folder_path = path


def _install_fakes(monkeypatch, world):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.LOGNOTICE = 1
    xbmc.log = lambda msg, level=0: world.logs.append((level, str(msg)))
    xbmc.getInfoLabel = world.info_label
    xbmc.getCondVisibility = world.cond
    xbmc.executebuiltin = world.executebuiltin
    xbmc.executeJSONRPC = world.execute_jsonrpc
    xbmc.sleep = lambda ms: world.sleeps.append(ms)
    xbmc.translatePath = lambda p: p
    xbmc.Monitor = _Monitor
    xbmc.Player = lambda *a, **k: types.SimpleNamespace(isPlayingVideo=lambda: False)

    xbmcaddon = types.ModuleType("xbmcaddon")

    class _FakeAddon:
        def __init__(self, id=None, *a, **k):
            # Kodi's real behaviour, and the reason a DISABLED add-on is
            # indistinguishable from an absent one: the constructor resolves
            # through CAddonMgr::GetAddon(..., OnlyEnabled::CHOICE_YES) and
            # throws AddonException when that lookup fails. System.HasAddon
            # would NOT do here: it is IsAddonInstalled and reads true for an
            # add-on the user switched off.
            if id == POV_ADDON_ID and not (world.pov_installed and world.pov_enabled):
                raise RuntimeError("Unknown addon id '%s'." % id)
            self.id = id or "script.ezmaintenanceplusplus"

        def getSetting(self, key):
            return "true" if world.setting_on else "false"

        def getSettingBool(self, key):
            return bool(world.setting_on)

        def setSetting(self, key, value):
            world.setting_on = str(value).lower() in ("true", "1")

        def getAddonInfo(self, key):
            return {
                "id": "script.ezmaintenanceplusplus",
                "name": "EZ Maintenance++",
                "version": "0",
            }.get(key, "")

        def getLocalizedString(self, sid):
            return str(sid)

    xbmcaddon.Addon = _FakeAddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Dialog = lambda *a, **k: types.SimpleNamespace(
        ok=lambda *a, **k: None,
        yesno=lambda *a, **k: False,
        notification=lambda *a, **k: None,
        select=lambda *a, **k: -1,
    )

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda p: p
    xbmcvfs.exists = lambda p: False

    pkgs = {}
    for name in ("resources", "resources.lib", "resources.lib.modules"):
        m = types.ModuleType(name)
        m.__path__ = []
        pkgs[name] = m

    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.PY2 = False
    b2f.unicode = str

    control = types.ModuleType("resources.lib.modules.control")
    control.USERDATA = "/tmp/userdata"

    maintenance = types.ModuleType("resources.lib.modules.maintenance")
    maintenance.logMaintenance = lambda *a, **k: None
    maintenance.determineNextMaintenance = lambda *a, **k: None

    tools = types.ModuleType("resources.lib.modules.tools")

    mods = dict(pkgs)
    mods.update(
        {
            "xbmc": xbmc,
            "xbmcaddon": xbmcaddon,
            "xbmcgui": xbmcgui,
            "xbmcvfs": xbmcvfs,
            "resources.lib.modules.backtothefuture": b2f,
            "resources.lib.modules.control": control,
            "resources.lib.modules.maintenance": maintenance,
            "resources.lib.modules.tools": tools,
        }
    )
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    pkgs["resources"].lib = pkgs["resources.lib"]
    pkgs["resources.lib"].modules = pkgs["resources.lib.modules"]
    for attr in ("backtothefuture", "control", "maintenance", "tools"):
        setattr(
            pkgs["resources.lib.modules"],
            attr,
            mods["resources.lib.modules." + attr],
        )
    return mods


class _ThreadRecorder:
    """Stands in for threading.Thread so CONSTRUCTION itself is observable."""

    def __init__(self, world):
        self._world = world

    def __call__(self, *a, **k):
        target = k.get("target")
        args = k.get("args", ())
        kwargs = k.get("kwargs", {})

        class _T(object):
            """A real thread keeps running after start(); modelling that is the
            only way the at-most-one-thread guard can be tested at all."""

            daemon = False
            name = k.get("name", "")

            def __init__(_self):
                _self._alive = False

            def start(_self):
                _self._alive = True

            def run_target(_self):
                _self._alive = True
                try:
                    if target is not None:
                        target(*args, **kwargs)
                finally:
                    _self._alive = False

            def join(_self, timeout=None):
                return None

            def is_alive(_self):
                return _self._alive

        t = _T()
        self._world.threads.append(t)
        return t


def _load_module(monkeypatch):
    name = "ezm_home_redirect_uut"
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(name, MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def world(monkeypatch):
    if MODULE_PATH is None:
        pytest.fail("redirect module missing; see test_redirect_module_exists")
    w = _World()
    _install_fakes(monkeypatch, w)
    # Patch BEFORE the module is imported so a `from threading import Thread`
    # inside the module also picks up the recorder.
    monkeypatch.setattr(threading, "Thread", _ThreadRecorder(w))
    w.load = lambda: _load_module(monkeypatch)
    return w


def _machine(mod, monitor=None):
    """The shipped state machine, which its own docstring offers as the
    thread-free, sleep-free seam for stepping one tick at a time."""
    cls = getattr(mod, "MoviesRedirect", None)
    assert cls is not None, "module must expose a steppable MoviesRedirect"
    return cls(monitor)


def _visit_movies(world, state, path=STOCK_EMPTY_LIBRARY, item_id="movies", ticks=4):
    """Model the real journey: focus the Home item, click it, land in Videos.

    The latch is sampled only while Home is active, so a test that teleports
    straight into Videos would never arm it and would pass for the wrong reason.
    """
    world.on_home(item_id)
    state.tick()
    world.enter_videos(path)
    for _ in range(ticks):
        state.tick()


# --------------------------------------------------------------------------- #
# 1. The URL is the contract
# --------------------------------------------------------------------------- #
def test_target_url_is_byte_equal_to_the_owners_string(world):
    mod = world.load()
    url = getattr(mod, "TARGET_URL", None)
    assert url is not None, "module must expose TARGET_URL"
    assert url == TARGET_URL, "TARGET_URL drifted from the owner's pasted string"
    assert "&amp;" not in url, "URL must carry raw &, never XML-escaped &amp;"
    assert url.count("&") == 3, "all three query separators must survive"
    assert "," not in url, (
        "a comma in the URL would be split by Kodi's builtin argument parser and "
        "silently truncate the path"
    )


def test_emitted_builtin_carries_the_url_byte_equal(world):
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state)
    updates = world.updates()
    assert len(updates) == 1, "expected exactly one Container.Update, got %r" % (updates,)
    assert updates[0] == "Container.Update(%s,replace)" % TARGET_URL, (
        "emitted builtin is not byte-equal to the contract: %r" % (updates[0],)
    )


def test_update_uses_replace_so_back_returns_to_home(world):
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state)
    assert world.updates()[0].endswith(",replace)"), (
        "without ,replace the path history keeps the stock Movies folder, so Back "
        "walks back into the container we just redirected away from"
    )


# --------------------------------------------------------------------------- #
# 2. Re-entrancy: the loop that burned the Estuary 8 widget picker
# --------------------------------------------------------------------------- #
def test_does_not_refire_on_the_container_it_just_created(world):
    """The one the owner cares most about.

    After the redirect fires, the fake moves the container so
    Container.FolderPath IS the POV URL. A module that only asks "am I on a
    stock Movies target" without clearing its latch will fire again, and again,
    and the user can never leave.
    """
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state, ticks=40)
    updates = world.updates()
    assert len(updates) == 1, (
        "re-entrancy: fired %d times, expected exactly 1. The module re-fired on "
        "the container it created. Updates: %r" % (len(updates), updates)
    )


def test_does_not_fire_when_already_inside_pov(world):
    mod = world.load()
    state = _machine(mod)
    world.on_home("movies")
    state.tick()
    world.enter_videos(TARGET_URL)
    for _ in range(10):
        state.tick()
    assert world.updates() == [], "fired while the container was already the target"


def test_a_second_genuine_visit_still_redirects(world):
    """One fire per visit, but the feature must not arm only once per boot."""
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state)
    assert len(world.updates()) == 1

    # Back out to Home, then click Movies again.
    world.on_home("movies")
    state.tick()
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    for _ in range(4):
        state.tick()
    assert len(world.updates()) == 2, (
        "the redirect armed once and never re-armed; a second visit to Movies "
        "must still redirect"
    )


# --------------------------------------------------------------------------- #
# 3. The discriminator: nothing else may be hijacked
# --------------------------------------------------------------------------- #
def test_tvshows_home_item_does_not_fire(world):
    """Home.xml:985 and Home.xml:996 emit the IDENTICAL builtin on an empty
    library, so only the latch can tell Movies from TV Shows."""
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state, item_id="tvshows")
    assert world.updates() == [], (
        "TV Shows was hijacked. Both items land on %s when the library is empty, "
        "so the discriminator must key on the focused Home item id."
        % STOCK_EMPTY_LIBRARY
    )


@pytest.mark.parametrize(
    "item_id", ["tvshows", "musicvideos", "livetv", "addons", "music", "pictures", ""]
)
def test_no_other_home_item_is_hijacked(world, item_id):
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state, item_id=item_id)
    assert world.updates() == [], "hijacked Home item %r" % (item_id,)


def test_latch_does_not_survive_the_videos_window_closing(world):
    """Focus Movies, enter Videos somewhere else, leave, then browse by hand to
    sources://video/. A stale latch would hijack that."""
    mod = world.load()
    state = _machine(mod)
    world.on_home("movies")
    state.tick()
    world.enter_videos("videodb://tvshows/titles/")
    state.tick()
    state.tick()
    # user leaves Videos entirely
    world.on_home("")
    state.tick()
    # ... and comes back via Files, with nothing focused on Home
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    for _ in range(4):
        state.tick()
    assert world.updates() == [], (
        "a stale latch hijacked a hand-browsed visit to %s" % STOCK_EMPTY_LIBRARY
    )


def test_unknown_skin_never_arms_the_latch(world):
    """On any skin without control 9000, the infolabel is "". That must disable
    the feature, not misfire it."""
    mod = world.load()
    state = _machine(mod)
    world.on_home("")  # infolabel returns empty, as on a non-Estuary skin
    state.tick()
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    for _ in range(6):
        state.tick()
    assert world.updates() == [], "fired on a skin that never reported a Home item id"


def test_home_focus_alone_does_not_fire(world):
    """Focusing Movies on Home is not clicking it."""
    mod = world.load()
    state = _machine(mod)
    world.on_home("movies")
    for _ in range(6):
        state.tick()
    assert world.updates() == [], "fired without the user ever opening Videos"


# --------------------------------------------------------------------------- #
# 4. Both stock branches, and nothing else
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("stock_path", ALL_STOCK_TARGETS)
def test_every_stock_movies_branch_redirects(world, stock_path):
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state, path=stock_path)
    updates = world.updates()
    assert len(updates) == 1, (
        "stock Estuary sends Movies to %s on one of its branches; all of them "
        "must redirect. Got %r" % (stock_path, updates)
    )
    assert TARGET_URL in updates[0]


@pytest.mark.parametrize(
    "path",
    [
        "videodb://tvshows/titles/",
        "videodb://musicvideos/titles/",
        "sources://music/",
        "plugin://plugin.video.something/",
        "special://profile/",
        "",
    ],
)
def test_unrelated_folders_are_left_alone(world, path):
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state, path=path)
    assert world.updates() == [], "redirected from a non-Movies folder %r" % (path,)


def test_does_not_fire_while_the_container_is_still_updating(world):
    """CGUIMediaWindow::OnInitWindow posts a DEFERRED plugin refresh, so a
    redirect fired too early is clobbered by the window's own update."""
    mod = world.load()
    state = _machine(mod)
    world.container_updating = True
    _visit_movies(world, state, ticks=8)
    assert world.updates() == [], "fired while Container.IsUpdating was true"
    world.container_updating = False
    for _ in range(4):
        state.tick()
    assert len(world.updates()) == 1, "never fired once the container settled"


def test_requires_the_target_to_be_stable_across_two_ticks(world):
    """One sighting is not enough; a transient path must not trigger."""
    mod = world.load()
    state = _machine(mod)
    world.on_home("movies")
    state.tick()
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    state.tick()  # first sighting only
    assert world.updates() == [], (
        "fired on a single sighting; the deferred plugin refresh can still "
        "clobber a redirect sent this early"
    )
    state.tick()
    assert len(world.updates()) == 1, "never fired after the path proved stable"


# --------------------------------------------------------------------------- #
# 5. POV absent is a silent no-op, never an error
# --------------------------------------------------------------------------- #
def test_pov_absent_does_not_fire_and_does_not_loop(world):
    world.pov_installed = False
    mod = world.load()
    mon = _Monitor(ticks=10)
    mod.run(mon)
    assert world.updates() == [], (
        "redirected to POV while POV is absent; that strands the user on an "
        "error dialog with no working Movies item to go back to"
    )
    assert mon.waits == [], "polled forever on a box that has no POV"


def test_pov_disabled_is_treated_as_absent(world):
    world.pov_enabled = False
    mod = world.load()
    mon = _Monitor(ticks=10)
    mod.run(mon)
    assert world.updates() == []
    assert mon.waits == [], "polled forever on a box where POV is disabled"


def test_pov_absent_logs_once_not_every_second(world):
    world.pov_installed = False
    mod = world.load()
    mod.run(_Monitor(ticks=10))
    assert len(world.logs) <= 1, (
        "logged %d times for an absent add-on; that fills kodi.log on every box "
        "in the fleet that does not have POV" % len(world.logs)
    )


def test_pov_absent_raises_nothing(world):
    world.pov_installed = False
    mod = world.load()
    mod.run(_Monitor(ticks=4))  # must not raise


# --------------------------------------------------------------------------- #
# 6. Shutdown safety
# --------------------------------------------------------------------------- #
def test_sleeps_on_wait_for_abort_never_xbmc_sleep(world):
    mod = world.load()
    mon = _Monitor(ticks=5)
    mod.run(mon)
    assert mon.waits, (
        "the loop never called monitor.waitForAbort(); Kodi cannot signal "
        "shutdown and will hang waiting for this service"
    )
    assert world.sleeps == [], (
        "the loop used xbmc.sleep(%r); that ignores abort and hangs shutdown"
        % (world.sleeps,)
    )


def test_loop_exits_promptly_when_abort_is_requested(world):
    mod = world.load()
    mon = _Monitor(ticks=1)  # abort on the very first wait
    mod.run(mon)
    assert len(mon.waits) == 1, (
        "loop ignored the abort signal and kept waiting: %r" % (mon.waits,)
    )


def test_idle_poll_is_not_a_busy_loop(world):
    """Nothing on screen: the interval must not burn a Firestick."""
    mod = world.load()
    world.window = "Screensaver"
    mon = _Monitor(ticks=5)
    mod.run(mon)
    assert mon.waits, "no wait recorded"
    assert all(w >= 1 for w in mon.waits), (
        "idle poll interval %r is under 1s; that is a busy loop while nobody is "
        "even looking at the screen" % (mon.waits,)
    )


def test_a_tick_that_raises_does_not_kill_the_loop(world):
    """The loop hosts a feature; it must not become a way to crash the thread."""
    mod = world.load()
    boom = {"n": 0}

    def _explode(_label):
        boom["n"] += 1
        raise RuntimeError("infolabel exploded")

    mod.xbmc.getInfoLabel = _explode
    mon = _Monitor(ticks=4)
    mod.run(mon)  # must not raise
    assert boom["n"] >= 1, "the harness never reached the failing call"
    assert len(mon.waits) == 4, "the loop died instead of surviving the exception"


def test_switching_the_setting_off_stops_the_running_loop(world):
    """Toggle-off is the primary rollback path, so it must not need a restart."""
    mod = world.load()
    world.setting_on = False
    mon = _Monitor(ticks=200)
    mod.run(mon)
    assert len(mon.waits) < 200, (
        "the loop never re-read its setting; switching the feature off would "
        "require a Kodi restart, which breaks the documented rollback"
    )


# --------------------------------------------------------------------------- #
# 7. OFF lives in service.py: the lazy-import seam
# --------------------------------------------------------------------------- #
def _toplevel_imported_names(tree):
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            for a in node.names:
                names.add((base + "." + a.name) if base else a.name)
                if base:
                    names.add(base)
    return names


def test_service_does_not_import_the_redirect_at_module_level():
    """service.py runs on EVERY box at startup, opted in or not.

    A module-level import means an ImportError or a syntax error in the redirect
    takes down scheduled maintenance, the post-restore check and the stale-key
    migration on boxes that never enabled the feature. Default-OFF does not
    protect against that; a lazy import does.
    """
    src = SERVICE_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    stem = MODULE_PATH.stem
    offenders = sorted(n for n in _toplevel_imported_names(tree) if n and stem in n)
    assert not offenders, (
        "service.py imports the redirect module at module level (%r). It must be "
        "imported lazily, inside the branch gated by the setting." % (offenders,)
    )


def test_service_actually_wires_the_redirect_in():
    src = SERVICE_PY.read_text(encoding="utf-8")
    assert MODULE_PATH.stem in src, (
        "service.py never references the redirect module, so the feature cannot "
        "run on any box no matter what the setting says"
    )


def test_service_reads_the_setting_without_importing_the_module():
    """The gate must be readable with the module absent, which is why the
    setting id is duplicated in service.py rather than imported from it."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    mod_src = MODULE_PATH.read_text(encoding="utf-8")
    setting_id = None
    for node in ast.walk(ast.parse(mod_src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SETTING_ID":
                    setting_id = ast.literal_eval(node.value)
    assert setting_id, "module must expose SETTING_ID"
    assert setting_id in src, (
        "service.py does not carry the setting id %r. It must read the setting "
        "DIRECTLY, so a box that never opted in never imports the module."
        % (setting_id,)
    )


def test_setting_id_matches_between_service_and_module():
    """The module's own comment says to keep these equal. Assert it, because a
    comment cannot fail a build and a silently mismatched id makes the feature
    permanently unreachable while every test still passes."""
    mod_src = MODULE_PATH.read_text(encoding="utf-8")
    svc_src = SERVICE_PY.read_text(encoding="utf-8")

    def _const(src, name):
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == name:
                        return ast.literal_eval(node.value)
        return None

    mod_id = _const(mod_src, "SETTING_ID")
    svc_id = _const(svc_src, "_REDIRECT_SETTING_ID")
    assert mod_id is not None, "module must expose SETTING_ID"
    assert svc_id is not None, (
        "service.py must define _REDIRECT_SETTING_ID so the gate is readable "
        "without importing the module"
    )
    assert mod_id == svc_id, (
        "setting id drifted: module says %r, service.py says %r. The feature "
        "would be unreachable and nothing else would notice." % (mod_id, svc_id)
    )


def test_service_call_site_is_inside_a_try(world):
    """Belt to the lazy-import braces: an exploding redirect is contained."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    stem = MODULE_PATH.stem
    tree = ast.parse(src)
    guarded = any(
        isinstance(node, ast.Try) and stem in ast.dump(node)
        for node in ast.walk(tree)
    )
    assert guarded, (
        "the redirect call site in service.py is not inside a try/except; an "
        "exception there kills the service thread that also runs maintenance"
    )


def test_setting_is_declared_in_settings_xml():
    """A feature with no way to turn it on is not shippable, and a feature with
    no way to turn it OFF has no rollback."""
    mod_src = MODULE_PATH.read_text(encoding="utf-8")
    setting_id = None
    for node in ast.walk(ast.parse(mod_src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SETTING_ID":
                    setting_id = ast.literal_eval(node.value)
    assert setting_id
    xml = SETTINGS_XML.read_text(encoding="utf-8")
    assert setting_id in xml, (
        "%r is not declared in resources/settings.xml, so the user can never "
        "toggle it and the documented rollback path does not exist" % (setting_id,)
    )


def test_setting_defaults_to_off():
    """Every box in the fleet auto-installs this release. Default-on would ship
    a behaviour change to boxes nobody asked."""
    mod_src = MODULE_PATH.read_text(encoding="utf-8")
    setting_id = None
    for node in ast.walk(ast.parse(mod_src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "SETTING_ID":
                    setting_id = ast.literal_eval(node.value)
    import xml.etree.ElementTree as ET

    root = ET.parse(str(SETTINGS_XML)).getroot()
    found = [el for el in root.iter("setting") if el.get("id") == setting_id]
    assert found, "setting %r not found in settings.xml" % (setting_id,)
    el = found[0]
    default = el.get("default")
    if default is None:
        node = el.find("default")
        default = node.text if node is not None else None
    assert str(default).strip().lower() in ("false", "0"), (
        "setting %r defaults to %r; it MUST default to off" % (setting_id, default)
    )


# --------------------------------------------------------------------------- #
# 8. Thread hygiene: onSettingsChanged fires on EVERY settings change
# --------------------------------------------------------------------------- #
def test_start_does_not_spawn_a_second_thread_while_one_runs(world):
    """service.py calls the starter from onSettingsChanged, which Kodi fires on
    every settings change, not just this one. Without an at-most-one guard the
    box accumulates a poller per visit to the settings dialog."""
    mod = world.load()
    first = mod.start(_Monitor(ticks=2))
    assert first is not None, "the first start() must return a thread"
    for _ in range(10):
        mod.start(_Monitor(ticks=2))
    assert len(world.threads) == 1, (
        "spawned %d pollers; every trip through the settings dialog leaks one"
        % len(world.threads)
    )


def test_start_can_respawn_after_the_poller_has_exited(world):
    """Switching off then on again must work without a Kodi restart, which is
    what ROLLBACK.md promises."""
    mod = world.load()
    first = mod.start(_Monitor(ticks=2))
    assert first is not None
    first._alive = False  # the poller noticed the setting go off and returned
    second = mod.start(_Monitor(ticks=2))
    assert second is not None, (
        "after the poller exited, turning the feature back on did nothing; that "
        "would need a Kodi restart and breaks the documented toggle"
    )
    assert len(world.threads) == 2


def test_started_thread_is_a_daemon(world):
    """A non-daemon poller can hold Kodi open at shutdown."""
    mod = world.load()
    t = mod.start(_Monitor(ticks=2))
    assert t.daemon is True, "poller thread must be a daemon"


def test_service_restarts_the_redirect_on_settings_changed():
    """ROLLBACK.md tells the owner the toggle takes effect without a restart.
    That promise has to be wired, not assumed."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    starter = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and "redirect" in node.name:
            if node.name.startswith("_maybe") or "start" in node.name:
                starter = node.name
                break
    assert starter, "no redirect starter function found in service.py"
    on_changed = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "onSettingsChanged"
    ]
    assert on_changed, "service.py has no onSettingsChanged"
    assert starter in ast.dump(on_changed[0]), (
        "onSettingsChanged does not start the redirect, so turning the setting "
        "on would need a Kodi restart"
    )


def test_one_home_tick_is_enough_to_rearm_the_latch(world):
    """Pins the ORDER of the two latch operations inside tick().

    This defect has been introduced twice. If the "Videos closed" clear runs
    AFTER the Home sample, the single tick on which the user returns to Home
    arms the latch and then wipes it in the same pass, because Videos was still
    visible on the previous tick. Home polls at IDLE_POLL, so the user gets a
    whole second in which clicking Movies quietly does nothing and they land on
    the stock empty sources list instead of POV.

    One tick on Home must be enough. Do not relax this to two.
    """
    mod = world.load()
    state = _machine(mod)
    _visit_movies(world, state)
    assert len(world.updates()) == 1

    world.on_home("movies")
    state.tick()  # EXACTLY one tick back on Home
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    for _ in range(4):
        state.tick()
    assert len(world.updates()) == 2, (
        "the latch was armed and cleared in the same tick; move the "
        "videos_was_visible clear ABOVE the Container(9000) sample in tick()"
    )


def test_empty_home_id_never_disarms_a_live_latch(world):
    """MEASURED: Container(9000).ListItem.Property(id) reads "" while Videos is
    active. If tick() treated that empty reading as "not Movies" and cleared the
    latch, the redirect would disarm itself the instant the window it waits for
    opened, and the feature would never fire at all."""
    mod = world.load()
    state = _machine(mod)
    world.on_home("movies")
    state.tick()
    world.enter_videos(STOCK_EMPTY_LIBRARY)
    assert world.info_label("Container(9000).ListItem.Property(id)") == "", (
        "harness no longer models the measured empty reading"
    )
    for _ in range(4):
        state.tick()
    assert len(world.updates()) == 1, (
        "an empty Home id disarmed the latch, so the redirect can never fire"
    )
