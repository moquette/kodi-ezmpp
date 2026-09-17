"""service.py boot-time self-heal of plugin.video.pov's reuse_language_invoker.

Watch item: .claude/memory/project-watch-python-invoker-sigabrt.md. Kodi 22's
current nightly reuses one embedded Python interpreter across POV's script
invocations when reuse_language_invoker is true, corrupting interpreter-global
state (a SIGABRT on office 2026-08-31, a TypeError deep in stdlib enum.py on
bedroom 2026-09-16). A one-time manual flip does not stick, POV's own
settings-apply path or a fresh install regenerates both files it consults from
its shipped default (measured reverted on office by 2026-09-16), so
service.py reasserts it every boot.

This file is about the WIRING (does _startup_sequence call it, in what order,
does it log the right thing, does it never raise), not profile.py's own
read/patch logic, which tests/test_settings_profile.py already covers against
the real module. resources.lib.modules.profile is stubbed here on purpose.

Same fixture approach as tests/test_service_stale_key_migration.py: fake just
enough of xbmc*/resources.* for the real service.py to import, then call the
real function.
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


class _Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.tvos = True
        self.version = "2026.9.16.1"
        self.logs = []  # (level, msg)


def _nsud_stub():
    m = types.ModuleType("resources.lib.modules.nsud")
    m.purge_stale_keys = lambda userdata_root: (0, 0, 0, 0)
    return m


def _load_service(monkeypatch, env, nsud_mod):
    """Install the fakes and import the real service.py fresh. Returns the module."""
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG = LOGDEBUG
    xbmc.LOGINFO = LOGINFO
    xbmc.LOGWARNING = LOGWARNING
    xbmc.LOGERROR = LOGERROR
    xbmc.LOGNOTICE = LOGNOTICE
    xbmc.log = lambda msg, level=LOGDEBUG: env.logs.append((level, msg))
    xbmc.translatePath = lambda p: p.replace("special://home/", str(env.tmp) + "/")
    xbmc.getCondVisibility = lambda cond: bool(env.tvos) if "TVOS" in cond else True
    xbmc.executebuiltin = lambda *a, **k: None
    xbmc.executeJSONRPC = lambda *a, **k: "{}"
    xbmc.sleep = lambda ms: None
    xbmc.Player = lambda *a, **k: types.SimpleNamespace(isPlayingVideo=lambda: False)
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
            return ""

        def setSetting(self, key, value):
            pass

        def getAddonInfo(self, key):
            if key == "version":
                return env.version
            return {"id": "script.ezmaintenanceplusplus"}.get(key, "")

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
            "resources.lib.modules.nsud": nsud_mod,
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

    monkeypatch.delitem(sys.modules, "ezm_service_pov_invoker_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_service_pov_invoker_under_test", SERVICE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _StubMon:
    def abortRequested(self):
        return False

    def waitForAbort(self, t):
        return False


def _profile_stub(result):
    """A stand-in resources.lib.modules.profile exposing just the outcome
    vocabulary and ensure_pov_reuse_invoker_disabled the real function calls."""
    m = types.ModuleType("resources.lib.modules.profile")
    m.APPLIED = "applied"
    m.ALREADY = "already-correct"
    m.ERROR = "error"
    m.ensure_pov_reuse_invoker_disabled = lambda log=None: result
    return m


def _inject_profile(monkeypatch, mod, profile_mod):
    monkeypatch.setitem(sys.modules, "resources.lib.modules.profile", profile_mod)
    setattr(sys.modules["resources.lib.modules"], "profile", profile_mod)


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = _Env(tmp_path)
    e.load = lambda: _load_service(monkeypatch, e, _nsud_stub())
    return e


def test_maybe_fix_pov_reuse_invoker_logs_when_healed(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch,
        mod,
        _profile_stub(
            {"settings": "applied", "addon_xml": "already-correct", "detail": ""}
        ),
    )
    mod._maybe_fix_pov_reuse_invoker()
    assert any(
        "healed" in m and level in (LOGINFO, LOGNOTICE, LOGDEBUG)
        for level, m in env.logs
    ), env.logs


def test_maybe_fix_pov_reuse_invoker_silent_when_already_correct(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch,
        mod,
        _profile_stub(
            {
                "settings": "already-correct",
                "addon_xml": "already-correct",
                "detail": "",
            }
        ),
    )
    mod._maybe_fix_pov_reuse_invoker()
    assert not any("POV" in m for _level, m in env.logs), (
        "already-correct on both halves must be silent"
    )


def test_maybe_fix_pov_reuse_invoker_logs_warning_on_error(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch,
        mod,
        _profile_stub(
            {"settings": "error", "addon_xml": "already-correct", "detail": "boom"}
        ),
    )
    mod._maybe_fix_pov_reuse_invoker()
    assert any(
        "POV" in m and level == LOGWARNING for level, m in env.logs
    ), env.logs


def test_maybe_fix_pov_reuse_invoker_never_raises_on_import_failure(monkeypatch, env):
    mod = env.load()

    # Force the lazy `from resources.lib.modules import profile` to fail by
    # removing any stub and NOT providing one, so Python's real import
    # machinery raises ModuleNotFoundError, which the function must swallow.
    monkeypatch.delitem(sys.modules, "resources.lib.modules.profile", raising=False)
    mod._maybe_fix_pov_reuse_invoker()  # must not raise
    assert any(
        "crashed" in m and level == LOGWARNING for level, m in env.logs
    ), env.logs


def test_startup_sequence_calls_pov_fix_between_pvr_resume_and_restore_check(env):
    svc = env.load()
    order = []
    for name in (
        "_maybe_purge_stale_nsud_keys",
        "_purge_stale_bytecode",
        "_maybe_resume_paused_pvr",
        "_maybe_fix_pov_reuse_invoker",
        "_maybe_restore_check",
    ):
        setattr(svc, name, (lambda n: lambda *a, **k: order.append(n))(name))

    svc._startup_sequence(_StubMon())

    assert "_maybe_fix_pov_reuse_invoker" in order
    assert order.index("_maybe_resume_paused_pvr") < order.index(
        "_maybe_fix_pov_reuse_invoker"
    )
    assert order.index("_maybe_fix_pov_reuse_invoker") < order.index(
        "_maybe_restore_check"
    )


# --------------------------------------------------------------------------- #
# AutoCompletion hidden self-heal wiring (service._maybe_hide_autocompletion)
# --------------------------------------------------------------------------- #
def _autocompletion_profile_stub(outcome, detail=""):
    """A stand-in resources.lib.modules.profile exposing just the outcome
    vocabulary and ensure_autocompletion_hidden the real function calls."""
    m = types.ModuleType("resources.lib.modules.profile")
    m.APPLIED = "applied"
    m.ALREADY = "already-correct"
    m.ERROR = "error"
    m.ensure_autocompletion_hidden = lambda: (outcome, detail)
    return m


def test_maybe_hide_autocompletion_logs_when_healed(monkeypatch, env):
    mod = env.load()
    _inject_profile(monkeypatch, mod, _autocompletion_profile_stub("applied"))
    mod._maybe_hide_autocompletion()
    assert any(
        "AutoCompletion" in m and "hidden" in m and level in (LOGINFO, LOGNOTICE, LOGDEBUG)
        for level, m in env.logs
    ), env.logs


def test_maybe_hide_autocompletion_silent_when_already_correct(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch, mod, _autocompletion_profile_stub("already-correct")
    )
    mod._maybe_hide_autocompletion()
    assert not any("AutoCompletion" in m for _level, m in env.logs), (
        "already-correct must be silent"
    )


def test_maybe_hide_autocompletion_logs_warning_on_error(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch, mod, _autocompletion_profile_stub("error", detail="boom")
    )
    mod._maybe_hide_autocompletion()
    assert any(
        "AutoCompletion" in m and level == LOGWARNING for level, m in env.logs
    ), env.logs


def test_maybe_hide_autocompletion_never_raises_on_import_failure(monkeypatch, env):
    mod = env.load()
    monkeypatch.delitem(sys.modules, "resources.lib.modules.profile", raising=False)
    mod._maybe_hide_autocompletion()  # must not raise
    assert any(
        "crashed" in m and level == LOGWARNING for level, m in env.logs
    ), env.logs


def test_startup_sequence_calls_autocompletion_hide_after_pov_fix(env):
    svc = env.load()
    order = []
    for name in (
        "_maybe_purge_stale_nsud_keys",
        "_purge_stale_bytecode",
        "_maybe_resume_paused_pvr",
        "_maybe_fix_pov_reuse_invoker",
        "_maybe_hide_autocompletion",
        "_maybe_restore_check",
    ):
        setattr(svc, name, (lambda n: lambda *a, **k: order.append(n))(name))

    svc._startup_sequence(_StubMon())

    assert "_maybe_hide_autocompletion" in order
    assert order.index("_maybe_fix_pov_reuse_invoker") < order.index(
        "_maybe_hide_autocompletion"
    )
    assert order.index("_maybe_hide_autocompletion") < order.index(
        "_maybe_restore_check"
    )


# --------------------------------------------------------------------------- #
# POV resume seek self-heal wiring (service._maybe_fix_pov_resume_seek)
# --------------------------------------------------------------------------- #
def _resume_seek_profile_stub(outcome, detail=""):
    m = types.ModuleType("resources.lib.modules.profile")
    m.APPLIED = "applied"
    m.ALREADY = "already-correct"
    m.ERROR = "error"
    m.ensure_pov_resume_seek_applied = lambda: (outcome, detail)
    return m


def test_maybe_fix_pov_resume_seek_logs_when_healed(monkeypatch, env):
    mod = env.load()
    _inject_profile(monkeypatch, mod, _resume_seek_profile_stub("applied"))
    mod._maybe_fix_pov_resume_seek()
    assert any(
        "resume seek patched" in m and level in (LOGINFO, LOGNOTICE, LOGDEBUG)
        for level, m in env.logs
    ), env.logs


def test_maybe_fix_pov_resume_seek_silent_when_already_correct(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch, mod, _resume_seek_profile_stub("already-correct")
    )
    mod._maybe_fix_pov_resume_seek()
    assert not any("resume seek" in m for _level, m in env.logs), (
        "already-correct must be silent"
    )


def test_maybe_fix_pov_resume_seek_logs_warning_on_error(monkeypatch, env):
    mod = env.load()
    _inject_profile(
        monkeypatch, mod, _resume_seek_profile_stub("error", detail="boom")
    )
    mod._maybe_fix_pov_resume_seek()
    assert any(
        "resume seek patch failed" in m and level == LOGWARNING
        for level, m in env.logs
    ), env.logs


def test_maybe_fix_pov_resume_seek_never_raises_on_import_failure(monkeypatch, env):
    mod = env.load()
    monkeypatch.delitem(sys.modules, "resources.lib.modules.profile", raising=False)
    mod._maybe_fix_pov_resume_seek()  # must not raise
    assert any(
        "resume seek patch crashed" in m and level == LOGWARNING
        for level, m in env.logs
    ), env.logs


def test_startup_sequence_calls_resume_seek_fix_after_autocompletion_hide(env):
    svc = env.load()
    order = []
    for name in (
        "_maybe_purge_stale_nsud_keys",
        "_purge_stale_bytecode",
        "_maybe_resume_paused_pvr",
        "_maybe_fix_pov_reuse_invoker",
        "_maybe_hide_autocompletion",
        "_maybe_fix_pov_resume_seek",
        "_maybe_restore_check",
    ):
        setattr(svc, name, (lambda n: lambda *a, **k: order.append(n))(name))

    svc._startup_sequence(_StubMon())

    assert "_maybe_fix_pov_resume_seek" in order
    assert order.index("_maybe_hide_autocompletion") < order.index(
        "_maybe_fix_pov_resume_seek"
    )
    assert order.index("_maybe_fix_pov_resume_seek") < order.index(
        "_maybe_restore_check"
    )
