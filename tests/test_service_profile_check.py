# -*- coding: utf-8 -*-
"""service._maybe_profile_check: the first-boot-after-apply profile check.

The one thing Apply Settings Profile cannot confirm in-session is the merged
file manager sources - Kodi reads sources.xml at startup only, so an in-flow
Files.GetSources of a correct write reads back ABSENT (the false-PARTIAL trap
plan 7.6 names). This boot check closes that loop, under the same rules the
restore check already lives by: silent on success, marker consumed exactly
once and regardless of outcome, GUI wait outside the consume path, and never
a blocking NFS call on the abort-gated service thread.

Plus the one rule that is NEW here: the marker is STAMPED with the writing
box's MAC, because a full backup carries this add-on's addon_data and would
otherwise make a SECOND box run the first box's pending check.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SERVICE_PY = HERE.parent / "script.ezmaintenanceplusplus" / "service.py"

LOGINFO, LOGWARNING = 1, 2


class _Env:
    def __init__(self, tmp_path):
        self.tmp = tmp_path
        self.logs = []
        self.notifications = []
        self.ready = True
        self.mac = "aa:bb:cc:dd:ee:ff"
        self.live_sources = []  # what Files.GetSources returns (paths)
        self.live_settings = {}  # sid -> live value
        self.rpc_calls = []
        self.marker = (
            tmp_path
            / "userdata/addon_data/script.ezmaintenanceplusplus/.ezm_profile_check"
        )

    def arm(self, payload):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text(json.dumps(payload))

    def log_lines(self, level):
        return [m for lv, m in self.logs if lv == level]


def _load_service(monkeypatch, env):
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 1, 2, 3
    xbmc.LOGNOTICE = 1
    xbmc.log = lambda msg, level=0: env.logs.append((level, msg))
    xbmc.translatePath = lambda p: p.replace("special://home/", str(env.tmp) + "/")
    xbmc.getCondVisibility = lambda cond: True
    xbmc.executebuiltin = lambda *a, **k: None
    xbmc.getInfoLabel = lambda label: (
        env.mac if label == "Network.MacAddress" else ""
    )
    xbmc.sleep = lambda ms: None
    xbmc.Player = lambda *a, **k: types.SimpleNamespace(isPlayingVideo=lambda: False)
    xbmc.Monitor = type(
        "Monitor",
        (),
        {"abortRequested": lambda self: False, "waitForAbort": lambda self, t: False},
    )

    def _rpc(raw):
        req = json.loads(raw)
        env.rpc_calls.append(req["method"])
        if req["method"] == "Files.GetSources":
            return json.dumps(
                {
                    "result": {
                        "sources": [{"file": p, "label": p} for p in env.live_sources]
                    }
                }
            )
        if req["method"] == "Settings.GetSettingValue":
            sid = req["params"]["setting"]
            if sid not in env.live_settings:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps({"result": {"value": env.live_settings[sid]}})
        return json.dumps({"result": {}})

    xbmc.executeJSONRPC = _rpc

    xbmcaddon = types.ModuleType("xbmcaddon")

    class _FakeAddon:
        def __init__(self, *a, **k):
            pass

        def getSetting(self, key):
            return ""

        def setSetting(self, key, value):
            pass

        def getAddonInfo(self, key):
            return {"id": "script.ezmaintenanceplusplus", "version": "0"}.get(key, "")

    xbmcaddon.Addon = _FakeAddon

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.Dialog = lambda *a, **k: types.SimpleNamespace(
        yesno=lambda *a, **k: 0,
        select=lambda *a, **k: -1,
        ok=lambda *a, **k: None,
        notification=lambda heading, message, *a, **k: env.notifications.append(
            (heading, message)
        ),
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
    for fn in (
        "logMaintenance",
        "determineNextMaintenance",
        "getNextMaintenance",
        "clearCache",
        "purgePackages",
        "deleteThumbnails",
    ):
        setattr(maintenance, fn, lambda *a, **k: None)

    control = types.ModuleType("resources.lib.modules.control")
    control.USERDATA = str(env.tmp / "userdata")

    tools = types.ModuleType("resources.lib.modules.tools")
    tools.profile_check_pending = lambda: env.marker.exists()

    def _read():
        try:
            data = json.loads(env.marker.read_text())
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    tools.read_profile_check = _read

    def _clear():
        try:
            env.marker.unlink()
        except OSError:
            pass

    tools.clear_profile_check_marker = _clear
    tools.restore_check_pending = lambda: False

    profile = types.ModuleType("resources.lib.modules.profile")

    def _values_match(live, text):
        if isinstance(live, bool):
            return str(text).strip().lower() == str(live).lower()
        if isinstance(live, int):
            try:
                return live == int(str(text).strip())
            except ValueError:
                return False
        return str(live) == str(text)

    profile.values_match = _values_match

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
            "resources.lib.modules.tools": tools,
            "resources.lib.modules.profile": profile,
        }
    )
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    pkgs["resources"].lib = pkgs["resources.lib"]
    pkgs["resources.lib"].modules = pkgs["resources.lib.modules"]
    for attr in ("backtothefuture", "maintenance", "control", "tools", "profile"):
        setattr(
            pkgs["resources.lib.modules"], attr, mods["resources.lib.modules." + attr]
        )

    monkeypatch.delitem(sys.modules, "ezm_service_profile_check_uut", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_service_profile_check_uut", SERVICE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod._wait_kodi_ready = lambda monitor, *a, **k: env.ready
    return mod


@pytest.fixture
def env(monkeypatch, tmp_path):
    e = _Env(tmp_path)
    e.load = lambda: _load_service(monkeypatch, e)
    return e


class _Mon:
    def abortRequested(self):
        return False

    def waitForAbort(self, t):
        return False


_PAYLOAD = {
    "box": "aa:bb:cc:dd:ee:ff",
    "sources": ["nfs://192.168.7.2/x/Share/", "https://tony7bones.github.io/"],
    "settings": {"services.webserver": "true", "epg.selectaction": "1"},
}


def test_no_marker_is_a_noop(env):
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.notifications == []
    assert env.logs == []
    assert env.rpc_calls == []


def test_clean_pass_is_silent_but_logs_and_consumes(env):
    env.arm(_PAYLOAD)
    env.live_sources = list(_PAYLOAD["sources"]) + ["/some/other/source/"]
    env.live_settings = {"services.webserver": True, "epg.selectaction": 1}
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.notifications == [], "a clean profile check must not speak"
    assert any("verified live" in m for m in env.log_lines(LOGINFO))
    assert not env.marker.exists(), "the one-shot marker is consumed"


def test_missing_source_raises_the_notification(env):
    env.arm(_PAYLOAD)
    env.live_sources = ["https://tony7bones.github.io/"]  # the NFS one is gone
    env.live_settings = {"services.webserver": True, "epg.selectaction": 1}
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert len(env.notifications) == 1
    heading, message = env.notifications[0]
    assert "EZ Maintenance++" in heading
    assert "Apply Settings Profile" in message, (
        "the toast must name the action the owner can take"
    )
    for jargon in ("NSUserDefaults", "tvOS", "nfs://", "JSON"):
        assert jargon.lower() not in message.lower(), jargon
    assert any(
        "ATTENTION" in m and "nfs://192.168.7.2/x/Share/" in m
        for m in env.log_lines(LOGWARNING)
    ), "the log carries the path detail the toast deliberately omits"
    assert not env.marker.exists(), "cleared regardless of outcome"


def test_setting_drift_raises_the_notification(env):
    env.arm(_PAYLOAD)
    env.live_sources = list(_PAYLOAD["sources"])
    env.live_settings = {"services.webserver": False, "epg.selectaction": 1}
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert len(env.notifications) == 1
    assert any(
        "services.webserver" in m for m in env.log_lines(LOGWARNING)
    )
    assert not env.marker.exists()


def test_unknown_live_setting_is_not_a_finding(env):
    """An id the running Kodi does not know was already reported by the apply
    as unknown-id; the boot check skipping it keeps the two verdicts from
    disagreeing."""
    env.arm(_PAYLOAD)
    env.live_sources = list(_PAYLOAD["sources"])
    env.live_settings = {"services.webserver": True}  # epg.selectaction absent
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.notifications == []
    assert not env.marker.exists()


def test_foreign_box_stamp_clears_without_running(env):
    """The marker rode a backup onto a different box: cleared, no check, no
    RPC traffic, an INFO line for the log reader (plan 7.7)."""
    foreign = dict(_PAYLOAD, box="11:22:33:44:55:66")
    env.arm(foreign)
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.notifications == []
    assert env.rpc_calls == [], "a foreign marker must not drive any check"
    assert not env.marker.exists()
    assert any("another box" in m for m in env.log_lines(LOGINFO))


def test_busy_stamp_is_unstamped_not_foreign(env):
    """Network.MacAddress returns the literal "Busy" while the info system
    warms up, and the first full bench run wrote exactly that into the stamp.
    A non-MAC stamp must read as UNSTAMPED (run the check), never as another
    box (clear it unrun)."""
    env.arm(dict(_PAYLOAD, box="Busy"))
    env.live_sources = list(_PAYLOAD["sources"])
    env.live_settings = {"services.webserver": True, "epg.selectaction": 1}
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.rpc_calls, "the check must RUN for a non-MAC stamp"
    assert env.notifications == []
    assert any("verified live" in m for m in env.log_lines(LOGINFO))
    assert not env.marker.exists()


def test_aborted_boot_does_not_burn_the_marker(env):
    """The GUI wait sits OUTSIDE the try/finally: an aborted boot must NOT
    consume the one-shot marker - the check never ran, so it is still owed
    (the _maybe_restore_check precedent, service.py)."""
    env.arm(_PAYLOAD)
    env.ready = False
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.marker.exists(), "an aborted boot burned the one-shot marker"
    assert env.notifications == []


def test_unreadable_marker_clears_silently(env):
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text("{not json")
    svc = env.load()
    svc._maybe_profile_check(_Mon())
    assert env.notifications == []
    assert not env.marker.exists()


def test_startup_sequence_runs_the_profile_check(env):
    """_startup_sequence exists so the ORDER is testable; the profile check
    must actually be in it, after the restore check."""
    svc = env.load()
    import inspect

    src = inspect.getsource(svc._startup_sequence)
    assert "_maybe_profile_check" in src
    assert src.index("_maybe_restore_check") < src.index("_maybe_profile_check")
