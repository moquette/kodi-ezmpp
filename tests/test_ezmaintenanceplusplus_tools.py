"""Coverage for script.ezmaintenanceplusplus tools.py after the IPTV removal.

EZ Maintenance++ has ZERO IPTV behavior. The former post-restore IPTV auto-enable intent
flag and the unattended boot gate (autoenable_iptv_after_restore) were REMOVED - they
auto-enabled an IPTV client that crashed natively on a real box. These tests prove, by
construction, that none of that machinery remains, and that the surviving buffer-prompt
marker helpers still work. Real tools.py is imported against faked Kodi modules.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).parent.parent / "script.ezmaintenanceplusplus"


@pytest.fixture
def tools(monkeypatch, tmp_path):
    settings = {}  # the fake Addon settings store (shared across Addon() calls)

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 3
    xbmc.LOGERROR = 1
    xbmc.LOGWARNING = 2
    xbmc.log = lambda *a, **k: None
    xbmc.sleep = lambda ms: None
    xbmc.translatePath = lambda p: p
    xbmc.getInfoLabel = lambda *a, **k: ""
    xbmc.executeJSONRPC = lambda *a, **k: "{}"
    monkeypatch.setitem(sys.modules, "xbmc", xbmc)

    xbmcaddon = types.ModuleType("xbmcaddon")

    class _Addon:
        def setSetting(self, k, v):
            settings[k] = v

        def getSetting(self, k):
            return settings.get(k, "")

        def getAddonInfo(self, _k):
            return ""

    xbmcaddon.Addon = lambda *a, **k: _Addon()
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon)

    xbmcgui = types.ModuleType("xbmcgui")

    class _DP:
        def create(self, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def close(self, *a, **k):
            pass

        def iscanceled(self):
            return False

    xbmcgui.DialogProgress = _DP
    xbmcgui.Dialog = lambda *a, **k: types.SimpleNamespace(
        select=lambda *a, **k: -1,
        ok=lambda *a, **k: None,
        notification=lambda *a, **k: None,
        input=lambda *a, **k: "",
    )
    monkeypatch.setitem(sys.modules, "xbmcgui", xbmcgui)

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda p: str(tmp_path / p.replace("special://home/", ""))
    xbmcvfs.exists = lambda p: True
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs)

    control = types.ModuleType("resources.lib.modules.control")
    control.USERDATA = str(tmp_path / "userdata")
    monkeypatch.setitem(sys.modules, "resources.lib.modules.control", control)

    # ui is imported at module top; give a minimal stub.
    ui = types.ModuleType("resources.lib.modules.ui")
    monkeypatch.setitem(sys.modules, "resources.lib.modules.ui", ui)
    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.unicode = str
    b2f.PY2 = False
    monkeypatch.setitem(sys.modules, "resources.lib.modules.backtothefuture", b2f)

    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if name.endswith(".tools") and "ezmaintenance" in str(
            getattr(sys.modules[name], "__file__", "")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "resources.lib.modules.tools", raising=False)
    mod = importlib.import_module("resources.lib.modules.tools")

    return types.SimpleNamespace(mod=mod, settings=settings)


def test_no_iptv_autoenable_api_remains(tools):
    # By construction: every IPTV auto-enable symbol is gone from tools.
    for gone in (
        "autoenable_iptv_after_restore",
        "mark_iptv_autoenable_pending",
        "iptv_autoenable_pending",
        "clear_iptv_autoenable_pending",
        "IPTV_PENDING",
    ):
        assert not hasattr(tools.mod, gone), "tools must not expose %s" % gone


def test_tools_source_has_no_iptv_tokens():
    src = (ADDON_ROOT / "resources" / "lib" / "modules" / "tools.py").read_text(
        encoding="utf-8"
    )
    for token in ("autoenable", "stage_iptv", "pvr_is_enabled", "set_pvr_enabled"):
        assert token not in src, "tools.py must not contain %r" % token


def test_no_post_restore_prompt_machinery_survives(tools):
    """The deleted popup must stay deleted, in code and not just in behaviour.

    Every name here was load-bearing for a boot-time modal that asked the user to
    repair values the restore had just cloned. They are gone together with the flow;
    a partial resurrection (say, re-adding the marker "just to record state") is how
    an unattended boot dialog comes back."""
    src = (ADDON_ROOT / "resources" / "lib" / "modules" / "tools.py").read_text(
        encoding="utf-8"
    )
    for token in (
        "BUFFER_PROMPT_MARKER",
        "mark_buffer_prompt_pending",
        "buffer_prompt_pending",
        "clear_buffer_prompt_marker",
        "prompt_buffer_after_restore",
        "prompt_devicename_after_restore",
        "prompt_after_restore",
        "arm_first_run_tuneup",
        "FIRST_RUN_FLAG",
        "_PROMPT_MAX_ATTEMPTS",
        "_PROMPT_MAX_BOOTS",
    ):
        assert token not in src, (
            "tools.py must not contain %r - the post-restore prompt was deleted, "
            "not disabled" % token
        )


def test_capture_device_identity_reads_the_live_device_name(tools, monkeypatch):
    """The capture reads THIS box's own name from the live settings.

    The cache buffer is deliberately NOT in the result. It is RESET to Kodi's default
    rather than preserved (owner decision 2026-07-31), so capturing it would only hand
    wiz._preserve_device_settings's write-back loop an inherited number to smuggle past
    the reset. Asserting the WHOLE dict, not just the name, is what keeps that true."""
    monkeypatch.setattr(tools.mod, "_get_devicename", lambda: "Living Room")
    monkeypatch.setattr(tools.mod, "_get_cache_mb", lambda: 96)
    assert tools.mod.capture_device_identity() == {"services.devicename": "Living Room"}


def test_capture_device_identity_omits_what_it_could_not_read(tools, monkeypatch):
    """An unreadable value is OMITTED, never defaulted.

    A default here would be written back over the archive as though it were this
    box's own value, which is worse than leaving the archive's: it would invent a
    name nobody chose."""
    monkeypatch.setattr(tools.mod, "_get_devicename", lambda: "")
    monkeypatch.setattr(tools.mod, "_get_cache_mb", lambda: None)
    assert tools.mod.capture_device_identity() == {}


# --------------------------------------------------------------------------- #
# reset_cache_buffer: the video cache buffer lands on Kodi's default after a restore
# or a wipe (owner decision 2026-07-31). Not the archive's number and not this box's
# own previous one - the fleet mixes device classes whose right buffer differs, so no
# inherited value may survive. The per-device recommendation is offered on demand in
# advancedSettings(); there is no prompt.
# --------------------------------------------------------------------------- #
def _rpc_recorder(tools, monkeypatch, current=150):
    """Drive the REAL _get_cache_mb/_set_cache_mb against a scripted JSON-RPC store.

    Patching the private helpers instead would test nothing: the setting id and the
    int coercion live in them, and a reset that named the wrong id would still pass."""
    calls = []
    state = {"mb": current}

    def _fake(method, params):
        calls.append((method, params))
        if method == "Settings.GetSettingValue":
            return {"result": {"value": state["mb"]}}
        if method == "Settings.SetSettingValue":
            state["mb"] = params.get("value")
            return {"result": True}
        return {}

    monkeypatch.setattr(tools.mod, "_jsonrpc", _fake)
    return calls, state


def _capture_vectors(monkeypatch, path):
    """Replace nsud.persist_one with a recorder that snapshots the buffer AT THE
    MOMENT it was called (on tvOS the vectored bytes are what the box boots)."""
    seen = []
    nsud = importlib.import_module("resources.lib.modules.nsud")

    def _fake(rel, log=None, **k):
        value = None
        if Path(path).exists():
            value = _read_setting(path, "filecache.memorysize")
        seen.append((rel, value))
        return True

    monkeypatch.setattr(nsud, "persist_one", _fake)
    return seen


def _read_setting(path, sid):
    import xml.etree.ElementTree as ET

    for n in ET.parse(str(path)).getroot().iter("setting"):
        if n.get("id") == sid:
            return (n.text or "").strip()
    return None


def _guisettings(path, buffer_mb=150):
    Path(path).write_text(
        '<settings version="2">'
        '<setting id="filecache.memorysize">%d</setting>'
        '<setting id="audiooutput.volumesteps">90</setting>'
        "</settings>" % buffer_mb
    )


def test_reset_cache_buffer_writes_the_live_store_the_file_and_the_vector(
    tools, monkeypatch, tmp_path
):
    """All THREE layers, because any one alone is silently reverted.

    Live only: on Fire TV / Android an unclean kill (power pull, task-swipe) never
    flushes it to disk. File only: Kodi's clean-shutdown flush writes live memory back
    over the file, the kodi-settings-clobber class this project has already reproduced
    on hardware. Vector missing: on tvOS the NSUserDefaults key SHADOWS the file and
    Kodi never copies a key back, so the Apple TV boots the old buffer."""
    _calls, state = _rpc_recorder(tools, monkeypatch, current=150)
    gs = tmp_path / "guisettings.xml"
    _guisettings(gs, 150)
    seen = _capture_vectors(monkeypatch, gs)

    res = tools.mod.reset_cache_buffer(str(gs))

    assert state["mb"] == tools.mod.KODI_DEFAULT_MB, (
        "Kodi's LIVE buffer is %r, so the clean-shutdown flush will write this box's "
        "old number straight back over the file" % state["mb"]
    )
    assert _read_setting(gs, "filecache.memorysize") == "20"
    assert _read_setting(gs, "audiooutput.volumesteps") == "90", (
        "the reset rewrote a setting that is none of its business"
    )
    assert seen == [("guisettings.xml", "20")], (
        "guisettings.xml must be vectored exactly once, AFTER the value is in it; "
        "saw %r" % seen
    )
    assert res == {"live": True, "file": True, "before": 150}


def test_reset_cache_buffer_can_defer_the_vector_to_its_caller(
    tools, monkeypatch, tmp_path
):
    """vector=False exists for one caller and one reason.

    wiz._preserve_device_settings writes the device name into the SAME file and owns a
    single persist_one at the end. A vector taken here would, on tvOS, drop the POSIX
    copy out from under that write (persist_one removes it once the read-back
    confirms), so the reset must be able to stay out of the way."""
    _rpc_recorder(tools, monkeypatch, current=150)
    gs = tmp_path / "guisettings.xml"
    _guisettings(gs, 150)
    seen = _capture_vectors(monkeypatch, gs)

    tools.mod.reset_cache_buffer(str(gs), vector=False)

    assert seen == [], "vector=False must not vector"
    assert _read_setting(gs, "filecache.memorysize") == "20", (
        "the file write is not optional; only the vector is"
    )


def test_reset_cache_buffer_on_a_wiped_box_reports_the_missing_file(
    tools, monkeypatch, tmp_path
):
    """The Fresh Start shape: the wipe already removed guisettings.xml.

    That is EXPECTED, not a failure - Kodi's own default is what a missing file means -
    so the live set must still take and the result must say `file: False` plainly
    rather than raise or claim success."""
    _calls, state = _rpc_recorder(tools, monkeypatch, current=150)
    missing = tmp_path / "gone" / "guisettings.xml"

    res = tools.mod.reset_cache_buffer(str(missing))

    assert state["mb"] == 20, (
        "the LIVE reset is the half that matters after a wipe: Kodi is still running "
        "on the old buffer, and a flush would write it into the fresh file"
    )
    assert res["live"] is True
    assert res["file"] is False


def test_reset_cache_buffer_never_raises(tools, monkeypatch, tmp_path):
    """It runs mid-restore and mid-wipe. A raise there would abort something that
    matters over a cosmetic setting, so every layer is independently guarded."""
    gs = tmp_path / "guisettings.xml"
    _guisettings(gs, 150)

    def _boom(*a, **k):
        raise RuntimeError("json-rpc down")

    monkeypatch.setattr(tools.mod, "_jsonrpc", _boom)
    kodisettings = importlib.import_module("resources.lib.modules._kodisettings")
    monkeypatch.setattr(kodisettings, "write_guisetting", _boom)
    nsud = importlib.import_module("resources.lib.modules.nsud")
    monkeypatch.setattr(nsud, "persist_one", _boom)

    res = tools.mod.reset_cache_buffer(str(gs))

    assert res == {"live": False, "file": False, "before": None}


def test_reset_cache_buffer_defaults_to_this_box_own_guisettings(tools, monkeypatch):
    """Called with no path (the Fresh Start caller), it targets this box's own file."""
    _rpc_recorder(tools, monkeypatch, current=150)
    targeted = []
    kodisettings = importlib.import_module("resources.lib.modules._kodisettings")
    monkeypatch.setattr(
        kodisettings,
        "write_guisetting",
        lambda path, sid, value: targeted.append((path, sid, value)) or True,
    )
    nsud = importlib.import_module("resources.lib.modules.nsud")
    monkeypatch.setattr(nsud, "persist_one", lambda *a, **k: True)

    tools.mod.reset_cache_buffer()

    assert targeted == [
        (tools.mod.GUISETTINGS_XML, "filecache.memorysize", tools.mod.KODI_DEFAULT_MB)
    ], targeted


def test_capture_device_identity_never_raises(tools, monkeypatch):
    """It runs as the first statement of restore(); a raise there would abort a
    restore over a cosmetic setting."""

    def boom():
        raise RuntimeError("json-rpc down")

    monkeypatch.setattr(tools.mod, "_get_devicename", boom)
    monkeypatch.setattr(tools.mod, "_get_cache_mb", boom)
    assert tools.mod.capture_device_identity() == {}


def test_restore_check_marker_round_trips_the_expected_skin(tools):
    """The marker must carry the archive's skin so the boot check has an expectation.

    Defect A3: the restore writes the archive's skin to disk and Kodi's shutdown flush
    then serializes the PRE-restore skin from live memory over it, so the box can
    reopen on the wrong one. The restore finishes BEFORE that restart, so the boot
    check is the only place the outcome is observable - and it can only report a
    mismatch if the expectation was recorded here."""
    t = tools.mod
    assert t.mark_restore_check_pending("skin.estuary.pov") is True
    assert t.restore_check_pending() is True
    assert t.restore_check_expected_skin() == "skin.estuary.pov"
    t.clear_restore_check_marker()
    assert t.restore_check_pending() is False
    assert t.restore_check_expected_skin() is None


def test_legacy_marker_carries_no_expectation(tools):
    """Markers written before A3 hold "1". Reading that as a skin name would make
    every pre-existing marker report a false wrong-skin finding on upgrade."""
    t = tools.mod
    assert t.mark_restore_check_pending() is True
    assert t.restore_check_pending() is True
    assert t.restore_check_expected_skin() is None, (
        'a legacy "1" marker must record NO expectation, never a skin named "1"'
    )
    t.clear_restore_check_marker()
    assert t.mark_restore_check_pending("") is True
    assert t.restore_check_expected_skin() is None, (
        "an empty skin (a restore that did not change the skin) records no expectation"
    )
