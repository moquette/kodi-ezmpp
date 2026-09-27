"""control.selectDialog -> xbmcgui.Dialog().select, the one hop nothing else tests.

WHY THIS FILE EXISTS
--------------------
Every other test in this suite stubs `control.selectDialog` wholesale, so a test
about what the Backup/Restore menu offered proves one fake called another fake.
The real one-line hop in control.py was covered by nothing, and it carries traps
a file-content test reads straight past:

Kodi's C++ signature is select(heading, list, autoclose, preselect, useDetails)
and this wrapper's is (list, heading) - reversed. A swap yields a dialog headed
with a Python list repr, and any third positional argument becomes `autoclose`
in milliseconds, i.e. a dialog that shuts itself before it can be read. Neither
mistake is visible to a test that stubs selectDialog, which every other test in
this suite does.

These tests drive the REAL control module against the shared dialog fake, which
records (args, kwargs) per call.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).resolve().parents[1] / "script.ezmaintenanceplusplus"


@pytest.fixture
def control_mod(monkeypatch, fake_kodi):
    """Import the real control.py under the fake Kodi modules."""
    for pkg in ("resources", "resources.lib", "resources.lib.modules"):
        if pkg not in sys.modules:
            m = types.ModuleType(pkg)
            m.__path__ = []
            monkeypatch.setitem(sys.modules, pkg, m)

    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.PY2 = False
    b2f.unicode = str
    monkeypatch.setitem(sys.modules, "resources.lib.modules.backtothefuture", b2f)

    ui = types.ModuleType("resources.lib.modules.ui")
    ui.HEADING = "EZ Maintenance++"
    monkeypatch.setitem(sys.modules, "resources.lib.modules.ui", ui)
    setattr(sys.modules["resources.lib.modules"], "ui", ui)

    sys.modules.pop("ezm_control_under_test", None)
    spec = importlib.util.spec_from_file_location(
        "ezm_control_under_test",
        ADDON_ROOT / "resources" / "lib" / "modules" / "control.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_control_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _calls(fake_kodi):
    return fake_kodi.xbmcgui.Dialog.select_calls


def test_the_heading_is_still_first_and_the_list_second(control_mod, fake_kodi):
    """Kodi takes (heading, list) in that order; control.selectDialog takes them
    the other way round. Swapping them yields a dialog headed with a Python list
    repr, which no fake would notice on its own."""
    rows = ["Backup", "Restore"]
    control_mod.selectDialog(rows, heading="Backup/Restore")

    args, kwargs = _calls(fake_kodi)[-1]
    assert args[0] == "Backup/Restore"
    assert args[1] == rows
    # Kodi's third positional is `autoclose`, in MILLISECONDS. Anything passed
    # through there shuts the dialog before it can be read, and no menu-level
    # test would see it because they all stub selectDialog.
    assert len(args) == 2, "a third positional argument is autoclose: %r" % (args,)
    assert not kwargs, kwargs


# --------------------------------------------------------------------------- #
# openSettings / openSettingsTab: the counter and the WAIT
# --------------------------------------------------------------------------- #
def test_open_settings_counts_its_own_calls(control_mod):
    """The count is what the looping Backup/Restore menu trusts instead of racing.

    Addon.OpenSettings is ASYNC: it returns long before the window paints, so
    "Window.IsActive(addonsettings)" right afterwards is a coin flip on an appliance.
    default._safe_to_re_present therefore compares this counter across a sub-action.
    If openSettings stops counting, the menu is back to losing that race silently, so
    the increment is asserted against the REAL function, not a stub."""
    before = control_mod.open_settings_count()
    control_mod.openSettings()
    control_mod.openSettings()
    assert control_mod.open_settings_count() == before + 2, (
        "openSettings must count every call, including the bail paths in wiz.py"
    )


def _script_settings_window(control_mod, fake_kodi, monkeypatch, appear_after):
    """Make the settings window appear only after `appear_after` probes, and record an
    ORDERED event log of everything openSettingsTab does.

    A fake where the window is active the instant the builtin returns models a desktop
    that paints faster than the next statement, and it CANNOT tell "waited until the
    dialog was up" from "fired SetFocus blindly": both orderings look identical when
    the flip is synchronous. On a Fire TV or an Apple TV the dialog is still painting,
    the category control does not exist yet, and a blind SetFocus is swallowed - the
    owner lands on Maintenance instead of Backup/Restore. So the window is LATE here,
    which is the real timing."""
    events = []
    polls = [0]
    aborting = [False]

    def _cond(cond):
        if cond != "Window.IsActive(addonsettings)":
            return False
        polls[0] += 1
        active = polls[0] > appear_after
        events.append("poll:active" if active else "poll:inactive")
        return active

    def _exec(command, *a, **k):
        events.append("exec:%s" % command)

    class _Monitor:
        def waitForAbort(self, timeout=0):
            events.append("wait")
            return aborting[0]

    monkeypatch.setattr(fake_kodi.xbmc, "getCondVisibility", _cond, raising=False)
    monkeypatch.setattr(fake_kodi.xbmc, "executebuiltin", _exec, raising=False)
    monkeypatch.setattr(fake_kodi.xbmc, "Monitor", _Monitor, raising=False)
    # control binds `execute = xbmc.executebuiltin` at import, so the module-level
    # alias has to move too or the SetFocus goes to the old fake.
    monkeypatch.setattr(control_mod, "execute", _exec)
    return events, polls, aborting


def test_open_settings_tab_waits_for_the_window_before_focusing_the_tab(
    control_mod, fake_kodi, monkeypatch
):
    """SetFocus must fire AFTER the settings window reports active, never before.

    Addon.OpenSettings and SetFocus are both ASYNC builtins. If SetFocus goes out
    while the dialog is still painting, control -199 does not exist yet, the builtin
    is swallowed, and the owner is left on the first tab. The wait loop is the entire
    reason this function exists, so the assertion is on ORDER: every probe that came
    back inactive, then the first active probe, then SetFocus."""
    events, polls, _ab = _script_settings_window(
        control_mod, fake_kodi, monkeypatch, appear_after=3
    )

    assert control_mod.openSettingsTab(1, timeout=5.0, poll=0.1) is True

    active_at = events.index("poll:active")
    focus_at = events.index("exec:SetFocus(-199)")
    assert focus_at == active_at + 1, (
        "SetFocus must be the very next thing after the window goes active, and must "
        "not appear before it: %r" % (events,)
    )
    assert polls[0] == 4, "expected 4 probes (3 inactive, then active): %r" % (events,)
    assert events.count("wait") == 3, (
        "each inactive probe must be followed by a real poll interval: %r" % (events,)
    )
    assert events.count("exec:SetFocus(-199)") == 1, events


def test_open_settings_tab_focuses_the_control_id_for_the_index_it_was_given(
    control_mod, fake_kodi, monkeypatch
):
    """Tab N is control -200 + N (CONTROL_SETTINGS_START_BUTTONS, GUIDialogSettingsBase.h).

    Pins the arithmetic itself, so a regression to the Krypton-era 100/200 form in
    openSettings(query) - which addresses nothing on Omega - fails here rather than
    quietly leaving the owner on whichever tab was already showing."""
    events, _polls, _ab = _script_settings_window(
        control_mod, fake_kodi, monkeypatch, appear_after=0
    )

    assert control_mod.openSettingsTab(2, timeout=1.0, poll=0.1) is True
    assert "exec:SetFocus(-198)" in events, events


def test_open_settings_tab_gives_up_quietly_when_the_window_never_appears(
    control_mod, fake_kodi, monkeypatch
):
    """The timeout path is best effort by design: no SetFocus, no exception.

    Firing SetFocus into whatever window IS on screen is worse than doing nothing - it
    can move focus somewhere the owner did not ask for. Settings were still opened, so
    she is one click from where she wanted to be."""
    events, polls, _ab = _script_settings_window(
        control_mod, fake_kodi, monkeypatch, appear_after=10**6
    )
    before = control_mod.open_settings_count()

    assert control_mod.openSettingsTab(1, timeout=0.5, poll=0.1) is False

    assert control_mod.open_settings_count() == before + 1, "settings never opened"
    assert not [e for e in events if "SetFocus" in e], (
        "no tab may be focused once the window never came up: %r" % (events,)
    )
    assert polls[0] == 5, events


def test_open_settings_tab_bails_out_when_kodi_is_shutting_down(
    control_mod, fake_kodi, monkeypatch
):
    """waitForAbort True means Kodi is tearing down. Stop probing, fire nothing."""
    events, polls, aborting = _script_settings_window(
        control_mod, fake_kodi, monkeypatch, appear_after=10**6
    )
    aborting[0] = True

    assert control_mod.openSettingsTab(1, timeout=5.0, poll=0.1) is False

    assert polls[0] == 1, "the abort must end the loop after one probe: %r" % (events,)
    assert events.count("wait") == 1, events
    assert not [e for e in events if "SetFocus" in e], events


def test_open_settings_tab_survives_a_probe_that_throws(
    control_mod, fake_kodi, monkeypatch
):
    """An odd platform where the probe raises must not take the caller down with it.

    Every caller is a bail path already telling the owner something is unset; an
    exception there would turn "set your path" into a stack trace."""
    events, _polls, _ab = _script_settings_window(
        control_mod, fake_kodi, monkeypatch, appear_after=1
    )

    def _boom(cond):
        raise RuntimeError("no such window condition")

    monkeypatch.setattr(fake_kodi.xbmc, "getCondVisibility", _boom, raising=False)
    before = control_mod.open_settings_count()

    assert control_mod.openSettingsTab(1, timeout=5.0, poll=0.1) is False
    assert control_mod.open_settings_count() == before + 1, "settings never opened"
    assert not [e for e in events if "SetFocus" in e], events


def test_importing_control_creates_no_kodi_object(control_mod, monkeypatch):
    """control.py is imported on every plugin run. After the in-process speed
    test Kodi reported its module-level objects as "left several classes in
    memory" (office, 2026-09-26: Addon, Addon, Addon, Dialog), so the module
    keeps none: re-importing it under recording Kodi classes builds nothing."""
    import xbmcaddon
    import xbmcgui

    made = []

    class Addon:
        def __init__(self, *a, **k):
            made.append("Addon")

        def getSetting(self, key):
            return "v:" + key

    class Dialog:
        def __init__(self, *a, **k):
            made.append("Dialog")

        def select(self, heading, items):
            return 0

    monkeypatch.setattr(xbmcaddon, "Addon", Addon)
    monkeypatch.setattr(xbmcgui, "Dialog", Dialog)
    spec = importlib.util.spec_from_file_location(
        "ezm_control_import_probe",
        ADDON_ROOT / "resources" / "lib" / "modules" / "control.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert made == [], made
    assert mod.setting("x") == "v:x"
    assert mod.selectDialog(["a"]) == 0
    assert made == ["Addon", "Dialog"]
