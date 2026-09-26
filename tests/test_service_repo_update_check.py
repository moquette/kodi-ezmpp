"""service.py's scheduled repository update check (2026.09.26.1).

A release is on the hub within minutes; the box then waited up to 24 hours for
Kodi's own repository timer, or for someone to press Check for updates. The
service's 60 s maintenance tick now runs xbmc.executebuiltin("UpdateAddonRepos")
itself, every N minutes (setting repo.check_minutes: default 60, floor 15, 0
off), with the last-check stamp persisted as a file in addon_data so a restart
inside the interval does not fire an extra check.

What is pinned here: fires after the interval and not before, waits out video
playback and fires on the next tick after it stops, 0 disables, the 15 minute
floor, the stamp survives a simulated service restart, the exact builtin string,
and an exception in the feature never escapes into the maintenance loop.

Same fixture approach as tests/test_service_pov_reuse_invoker.py: fake just
enough of xbmc*/resources.* for the real service.py to import, then call the
real functions. No network, no Kodi.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).resolve().parent.parent / "script.ezmaintenanceplusplus"
SERVICE_PY = ADDON_ROOT / "service.py"

LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR, LOGNOTICE = 0, 1, 2, 3, 4

T0 = 1_800_000_000.0  # an arbitrary epoch anchor; the code only takes differences


class _Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.logs = []  # (level, msg)
        self.builtins = []
        self.settings = {}
        self.playing = False
        self.builtin_raises = None
        self.now = T0


def _load_service(monkeypatch, env):
    """Install the fakes and import the real service.py fresh. Each load is a
    simulated service (re)start: module state is new, the tmp tree persists."""
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = LOGDEBUG
    xbmc.LOGINFO = LOGINFO
    xbmc.LOGWARNING = LOGWARNING
    xbmc.LOGERROR = LOGERROR
    xbmc.LOGNOTICE = LOGNOTICE
    xbmc.log = lambda msg, level=LOGDEBUG: env.logs.append((level, msg))
    xbmc.translatePath = lambda p: p.replace("special://home/", str(env.tmp) + "/")
    xbmc.getCondVisibility = lambda cond: True

    def _builtin(cmd):
        if env.builtin_raises is not None:
            raise env.builtin_raises
        env.builtins.append(cmd)

    xbmc.executebuiltin = _builtin
    xbmc.executeJSONRPC = lambda *a, **k: "{}"
    xbmc.sleep = lambda ms: None
    xbmc.Player = lambda *a, **k: types.SimpleNamespace(
        isPlayingVideo=lambda: env.playing
    )
    xbmc.Monitor = type(
        "Monitor",
        (),
        {"abortRequested": lambda self: False, "waitForAbort": lambda self, t: False},
    )

    xbmcaddon = types.ModuleType("xbmcaddon")

    class _FakeAddon:
        def __init__(self, *a, **k):
            pass

        def getSetting(self, key):
            return env.settings.get(key, "")

        def setSetting(self, key, value):
            env.settings[key] = value

        def getAddonInfo(self, key):
            return {"id": "script.ezmaintenanceplusplus", "version": "2026.9.26.1"}.get(
                key, ""
            )

    xbmcaddon.Addon = _FakeAddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Dialog = lambda *a, **k: types.SimpleNamespace(
        yesno=lambda *a, **k: 0,
        select=lambda *a, **k: -1,
        ok=lambda *a, **k: None,
        notification=lambda *a, **k: None,
    )

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = xbmc.translatePath
    xbmcvfs.exists = lambda p: Path(p).exists()

    pkgs = {}
    for name in ("resources", "resources.lib", "resources.lib.modules"):
        m = types.ModuleType(name)
        m.__path__ = []
        pkgs[name] = m

    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.PY2 = False
    b2f.unicode = str

    maintenance = types.ModuleType("resources.lib.modules.maintenance")
    maintenance.logMaintenance = lambda *a, **k: None
    maintenance.determineNextMaintenance = lambda *a, **k: None
    maintenance.getNextMaintenance = lambda *a, **k: 0
    maintenance.clearCache = lambda *a, **k: None
    maintenance.purgePackages = lambda *a, **k: None
    maintenance.deleteThumbnails = lambda *a, **k: None

    control = types.ModuleType("resources.lib.modules.control")
    control.USERDATA = str(env.tmp / "userdata")

    nsud = types.ModuleType("resources.lib.modules.nsud")
    nsud.purge_stale_keys = lambda userdata_root: (0, 0, 0, 0)

    mods = dict(pkgs)
    mods.update(
        {
            "xbmc": xbmc,
            "xbmcaddon": xbmcaddon,
            "xbmcgui": xbmcgui,
            "xbmcvfs": xbmcvfs,
            "resources.lib.modules.backtothefuture": b2f,
            "resources.lib.modules.maintenance": maintenance,
            "resources.lib.modules.control": control,
            "resources.lib.modules.nsud": nsud,
        }
    )
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    pkgs["resources"].lib = pkgs["resources.lib"]
    pkgs["resources.lib"].modules = pkgs["resources.lib.modules"]
    for attr in ("backtothefuture", "maintenance", "control", "nsud"):
        setattr(
            pkgs["resources.lib.modules"], attr, mods["resources.lib.modules." + attr]
        )

    monkeypatch.delitem(sys.modules, "ezm_service_repo_check_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_service_repo_check_under_test", SERVICE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # The feature's own clock, so a test can move time without sleeping.
    monkeypatch.setattr(mod.time, "time", lambda: env.now)
    return mod


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = _Env(tmp_path)
    e.load = lambda: _load_service(monkeypatch, e)
    return e


def _stamp(mod):
    return mod._read_repo_check_stamp()


def _trigger_lines(env):
    return [m for _, m in env.logs if "repository update check triggered" in m]


# --------------------------------------------------------------------------- #
# The interval
# --------------------------------------------------------------------------- #
def test_default_interval_is_60_and_floor_is_15(env):
    mod = env.load()
    assert mod.REPO_CHECK_DEFAULT_MINUTES == 60
    assert mod.REPO_CHECK_MIN_MINUTES == 15
    assert mod._repo_check_interval_minutes() == 60  # unset setting -> default


def test_first_check_after_boot_is_one_full_interval_later_not_at_boot(env):
    mod = env.load()
    mod._arm_repo_check_clock()  # service start, no stamp yet
    assert _stamp(mod) == T0

    env.now = T0 + 60  # the first tick
    assert mod._maybe_update_addon_repos() is False
    env.now = T0 + 59 * 60
    assert mod._maybe_update_addon_repos() is False
    assert env.builtins == []


def test_fires_once_the_interval_has_elapsed(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)
    assert mod._maybe_update_addon_repos() is True
    assert env.builtins == ["UpdateAddonRepos"]
    assert _stamp(mod) == T0  # the clock restarts from the fire
    assert _trigger_lines(env) == [
        "ezmaintenanceplus: repository update check triggered (every 60 min)"
    ]
    assert env.logs[-1][0] == LOGINFO  # the add-on's normal loglevel


def test_does_not_fire_before_the_interval(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 59 * 60)
    assert mod._maybe_update_addon_repos() is False
    assert env.builtins == []
    assert _stamp(mod) == T0 - 59 * 60  # and the clock is not disturbed
    assert _trigger_lines(env) == []


def test_fires_only_once_per_interval_across_many_ticks(env):
    mod = env.load()
    mod._arm_repo_check_clock()
    for minute in range(1, 3 * 60 + 1):  # three hours of 60 s ticks
        env.now = T0 + minute * 60
        mod._maybe_update_addon_repos()
    assert env.builtins == ["UpdateAddonRepos"] * 3


def test_exact_builtin_string_is_UpdateAddonRepos(env):
    """The add-on browser's Check for updates runs UpdateAddonRepos and nothing
    else. This is not UpdateLocalAddons, and it is not a settings write."""
    mod = env.load()
    assert mod.REPO_CHECK_BUILTIN == "UpdateAddonRepos"
    mod._write_repo_check_stamp(T0 - 24 * 60 * 60)
    mod._maybe_update_addon_repos()
    assert env.builtins == ["UpdateAddonRepos"]
    assert all(cmd == "UpdateAddonRepos" for cmd in env.builtins)
    assert env.settings == {}  # never writes a setting, its own or Kodi's


# --------------------------------------------------------------------------- #
# Playback
# --------------------------------------------------------------------------- #
def test_does_not_fire_during_playback_then_fires_on_the_next_tick_after(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)

    env.playing = True
    assert mod._maybe_update_addon_repos() is False
    env.now += 60
    assert mod._maybe_update_addon_repos() is False
    assert env.builtins == []
    assert _stamp(mod) == T0 - 60 * 60  # deferred, not skipped: the clock waits

    env.playing = False
    env.now += 60
    assert mod._maybe_update_addon_repos() is True
    assert env.builtins == ["UpdateAddonRepos"]
    assert _stamp(mod) == env.now


def test_loop_passes_the_player_state_it_already_checked(env):
    """_service_loop only reaches the check when video is not playing (the
    AutoClean gate), and says so, so the check does not ask the player twice.
    Three ticks: video plays during the first two, stops before the third."""
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)
    env.playing = True
    ticks = iter([False, False, False, True])
    seen = []
    real = mod._maybe_update_addon_repos

    def _spy(now=None, playing=None):
        seen.append(playing)
        return real(now=now, playing=playing)

    mod._maybe_update_addon_repos = _spy

    class _Mon:
        def abortRequested(self):
            return False

        def waitForAbort(self, t):
            env.now += t
            if env.now >= T0 + 180:
                env.playing = False
            return next(ticks)

    mod._service_loop(_Mon())
    assert seen == [False]  # only the tick where video was not playing
    assert env.builtins == ["UpdateAddonRepos"]


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #
def test_zero_disables(env):
    mod = env.load()
    env.settings["repo.check_minutes"] = "0"
    assert mod._repo_check_interval_minutes() == 0
    mod._write_repo_check_stamp(T0 - 7 * 24 * 60 * 60)  # a week stale
    for _ in range(10):
        env.now += 60
        assert mod._maybe_update_addon_repos() is False
    assert env.builtins == []
    assert _trigger_lines(env) == []


@pytest.mark.parametrize("raw", ["1", "5", "14"])
def test_values_below_15_are_clamped_up_to_15(env, raw):
    mod = env.load()
    env.settings["repo.check_minutes"] = raw
    assert mod._repo_check_interval_minutes() == 15
    mod._write_repo_check_stamp(T0 - 14 * 60)
    assert mod._maybe_update_addon_repos() is False  # not at the raw value
    env.now = T0 + 60  # 15 min since the stamp
    assert mod._maybe_update_addon_repos() is True
    assert _trigger_lines(env) == [
        "ezmaintenanceplus: repository update check triggered (every 15 min)"
    ]


def test_values_at_or_above_15_are_honoured(env):
    mod = env.load()
    env.settings["repo.check_minutes"] = "15"
    assert mod._repo_check_interval_minutes() == 15
    env.settings["repo.check_minutes"] = "240"
    assert mod._repo_check_interval_minutes() == 240


@pytest.mark.parametrize("raw", ["", "abc", "-5", "12.5"])
def test_garbage_or_negative_setting_falls_back_safely(env, raw):
    mod = env.load()
    env.settings["repo.check_minutes"] = raw
    assert mod._repo_check_interval_minutes() in (0, 60)
    if raw == "-5":
        assert mod._repo_check_interval_minutes() == 0  # negative means off
    else:
        assert mod._repo_check_interval_minutes() == 60  # unparseable: default


def test_setting_is_read_every_tick_so_a_change_applies_live(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 20 * 60)
    assert mod._maybe_update_addon_repos() is False  # 60 min default
    env.settings["repo.check_minutes"] = "15"
    assert mod._maybe_update_addon_repos() is True  # 20 min > 15 min


# --------------------------------------------------------------------------- #
# Persistence across a restart
# --------------------------------------------------------------------------- #
def test_stamp_lives_in_addon_data_not_in_settings(env):
    mod = env.load()
    mod._arm_repo_check_clock()
    expected = (
        env.tmp / "userdata" / "addon_data" / "script.ezmaintenanceplusplus"
    ) / ".ezm_repo_check"
    assert Path(mod.REPO_CHECK_STAMP) == expected
    assert expected.read_text() == "%d" % T0
    assert env.settings == {}


def test_restart_inside_the_interval_keeps_the_stamp_and_fires_no_extra_check(env):
    mod = env.load()
    mod._arm_repo_check_clock()
    env.now = T0 + 60 * 60
    assert mod._maybe_update_addon_repos() is True  # first check, at T0 + 60 min
    assert env.builtins == ["UpdateAddonRepos"]

    # Kodi restarts 10 minutes later: a fresh service process, same addon_data.
    env.now = T0 + 70 * 60
    env.builtins.clear()
    mod2 = env.load()
    mod2._arm_repo_check_clock()
    assert _stamp(mod2) == T0 + 60 * 60  # kept, not reset to boot

    for minute in range(1, 50):  # every tick up to 49 min after the restart
        env.now = T0 + 70 * 60 + minute * 60
        assert mod2._maybe_update_addon_repos() is False
    assert env.builtins == []  # no extra check from the restart

    env.now = T0 + 120 * 60  # 60 min after the last check, on the original clock
    assert mod2._maybe_update_addon_repos() is True
    assert env.builtins == ["UpdateAddonRepos"]


def test_restart_after_a_long_off_period_waits_a_full_interval(env):
    """Box was off for a day: Kodi ran its own check at boot, so the service
    re-arms rather than firing on its first tick."""
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 24 * 60 * 60)
    mod._arm_repo_check_clock()
    assert _stamp(mod) == T0
    env.now = T0 + 60
    assert mod._maybe_update_addon_repos() is False
    env.now = T0 + 60 * 60
    assert mod._maybe_update_addon_repos() is True


def test_stamp_from_the_future_is_reset(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 + 10 * 24 * 60 * 60)  # clock went backwards
    mod._arm_repo_check_clock()
    assert _stamp(mod) == T0
    mod._write_repo_check_stamp(T0 + 10 * 24 * 60 * 60)
    assert mod._maybe_update_addon_repos() is False  # the tick heals it too
    assert _stamp(mod) == T0


def test_unreadable_stamp_file_is_treated_as_absent(env):
    mod = env.load()
    Path(mod.REPO_CHECK_STAMP).parent.mkdir(parents=True, exist_ok=True)
    Path(mod.REPO_CHECK_STAMP).write_text("not a number")
    assert mod._read_repo_check_stamp() == 0.0
    assert mod._maybe_update_addon_repos() is False  # re-arms, does not fire
    assert _stamp(mod) == T0


# --------------------------------------------------------------------------- #
# It can never take the maintenance loop down
# --------------------------------------------------------------------------- #
def test_builtin_raising_is_logged_as_a_warning_and_swallowed(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)
    env.builtin_raises = RuntimeError("boom")
    assert mod._maybe_update_addon_repos() is False
    warnings = [m for lvl, m in env.logs if lvl == LOGWARNING]
    assert warnings == [
        "ezmaintenanceplus: repository update check failed RuntimeError: boom"
    ]
    assert _stamp(mod) == T0 - 60 * 60  # a failed fire does not restart the clock


def test_stamp_write_failure_does_not_raise(env, monkeypatch):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)
    monkeypatch.setattr(
        mod, "REPO_CHECK_STAMP", str(env.tmp / "nope" / "sub" / "x" / "stamp")
    )
    monkeypatch.setattr(mod.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert mod._read_repo_check_stamp() == 0.0
    assert mod._maybe_update_addon_repos() is False
    assert mod._write_repo_check_stamp(T0) is False
    mod._arm_repo_check_clock()  # must not raise either


def test_loop_keeps_running_when_the_check_raises(env, monkeypatch):
    """A broken check is a warning per tick, never the end of AutoClean."""
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)
    env.builtin_raises = RuntimeError("boom")
    ticks = iter([False, False, False, True])

    class _Mon:
        def abortRequested(self):
            return False

        def waitForAbort(self, t):
            env.now += t
            return next(ticks)

    mod._service_loop(_Mon())  # returns on abort, not on the exception
    warnings = [m for lvl, m in env.logs if lvl == LOGWARNING]
    assert len(warnings) == 3


def test_loop_exits_on_abort_before_any_work(env):
    mod = env.load()
    mod._write_repo_check_stamp(T0 - 60 * 60)

    class _Mon:
        def abortRequested(self):
            return False

        def waitForAbort(self, t):
            return True

    mod._service_loop(_Mon())
    assert env.builtins == []


def test_arming_is_skipped_when_the_service_is_already_stopping(env):
    """A service aborted inside its boot window (update landing, add-on being
    disabled) must not touch the stamp or ask Kodi for the add-on: on office
    2026-09-26 10:53:10 that produced Kodi's "EXCEPTION: Unknown addon id"."""
    mod = env.load()
    addon_calls = []
    real_addon = sys.modules["xbmcaddon"].Addon

    def _counting(*a, **k):
        addon_calls.append(1)
        return real_addon(*a, **k)

    sys.modules["xbmcaddon"].Addon = _counting

    class _Stopping:
        def abortRequested(self):
            return True

    mod._arm_repo_check_clock(monitor=_Stopping())
    assert not Path(mod.REPO_CHECK_STAMP).exists()
    assert addon_calls == []

    class _Running:
        def abortRequested(self):
            return False

    mod._arm_repo_check_clock(monitor=_Running())
    assert _stamp(mod) == T0


def test_main_arms_the_clock_then_runs_the_loop(env):
    """The wiring in __main__: arm first (so a stamp-less boot starts a full
    interval), then the loop. Read from the source, since __main__ cannot be
    imported."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    main = src[src.index('if __name__ == "__main__":') :]
    assert main.index("_arm_repo_check_clock(monitor=monitor)") < main.index(
        "_service_loop(monitor)"
    )
    assert main.count("while not monitor.abortRequested()") == 0  # the loop moved


def test_never_reads_or_writes_kodis_update_mode(env):
    """Owner rule: pressing the check is fine, changing the setting is not. The
    feature must not even LOOK at addons.updatemode or general.addonupdates."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    start = src.index("REPO_CHECK_STAMP = ")
    end = src.index("def _jsonrpc_service(")
    feature = src[start:end]
    assert "updatemode" not in feature
    assert "addonupdates" not in feature
    assert "Settings.SetSettingValue" not in feature
    assert "Settings.GetSettingValue" not in feature
