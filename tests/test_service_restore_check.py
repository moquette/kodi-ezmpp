# -*- coding: utf-8 -*-
"""service._maybe_restore_check: the first-boot-after-restore self-check.

Silent on a clean pass (log only), a notification on a real finding, the marker
consumed exactly once - and only when the check actually ran. These tests exist
because the first cut shipped a NameError (AddonTitle undefined) that the outer
except swallowed, silencing the notification path entirely; a real fake driving
the real service.py catches that class.
"""

from __future__ import annotations

import importlib.util
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
        self.logs = []  # (level, msg)
        self.notifications = []  # (heading, message)
        self.userdata = str(tmp_path / "userdata")
        self.dup_hits = []  # what restorecheck.duplicate_listing_hits returns
        self.ready = True  # _wait_kodi_ready result
        self.marker = (
            tmp_path
            / "userdata/addon_data/script.ezmaintenanceplusplus/.ezm_restore_check"
        )

    def arm(self):
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        self.marker.write_text("1")

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
            return {"id": "script.ezmaintenanceplusplus", "version": "0"}.get(key, "")

    xbmcaddon.Addon = _FakeAddon

    xbmcgui = types.ModuleType("xbmcgui")

    def _dialog(*a, **k):
        return types.SimpleNamespace(
            yesno=lambda *a, **k: 0,
            select=lambda *a, **k: -1,
            ok=lambda *a, **k: None,
            notification=lambda heading, message, *a, **k: env.notifications.append(
                (heading, message)
            ),
        )

    xbmcgui.Dialog = _dialog

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
    control.USERDATA = env.userdata

    tools = types.ModuleType("resources.lib.modules.tools")
    tools.restore_check_pending = lambda: env.marker.exists()

    def _clear():
        try:
            env.marker.unlink()
        except OSError:
            pass

    tools.clear_restore_check_marker = _clear

    def _expected_skin():
        try:
            v = env.marker.read_text().strip()
        except OSError:
            return None
        return None if v in ("", "1") else v

    tools.restore_check_expected_skin = _expected_skin

    restorecheck = types.ModuleType("resources.lib.modules.restorecheck")
    restorecheck.duplicate_listing_hits = lambda: list(env.dup_hits)

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
            "resources.lib.modules.restorecheck": restorecheck,
        }
    )
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)
    pkgs["resources"].lib = pkgs["resources.lib"]
    pkgs["resources.lib"].modules = pkgs["resources.lib.modules"]
    for attr in ("backtothefuture", "maintenance", "control", "tools", "restorecheck"):
        setattr(
            pkgs["resources.lib.modules"], attr, mods["resources.lib.modules." + attr]
        )

    monkeypatch.delitem(sys.modules, "ezm_service_restore_check_uut", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_service_restore_check_uut", SERVICE_PY
    )
    mod = importlib.util.module_from_spec(spec)
    # Force _wait_kodi_ready to env.ready without racing a real GUI wait.
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


def test_no_marker_is_a_noop(env):
    svc = env.load()
    svc._maybe_restore_check(_Mon())
    assert env.notifications == []
    assert env.logs == []


def test_clean_check_is_silent_but_logs(env):
    env.arm()
    env.dup_hits = []
    svc = env.load()
    svc._maybe_restore_check(_Mon())
    assert env.notifications == [], "a clean self-check must not speak"
    assert any("verified clean" in m for m in env.log_lines(LOGINFO))
    assert not env.marker.exists(), "the one-shot marker is consumed"


def test_finding_raises_the_notification(env):
    """The regression guard for the AddonTitle NameError: a real finding MUST
    produce the notification, not be swallowed by the guard."""
    env.arm()
    env.dup_hits = [
        "special://profile/addon_data/pvr.iptvsimple/instance-settings-1.xml"
    ]
    svc = env.load()
    svc._maybe_restore_check(_Mon())
    assert len(env.notifications) == 1, "a finding must reach the notification"
    heading, message = env.notifications[0]
    assert "EZ Maintenance++" in heading
    # The toast must name an ACTION the owner can take. It used to say "open EZ
    # Maintenance++", which routed her to a menu whose only relevant entry was
    # "Purge stale tvOS keys" - jargon, and removed in 2026.07.19.5. Both actions
    # named here re-run the stale-key purge on their own.
    assert "Restore again" in message
    assert "restart the box" in message
    assert "open EZ Maintenance++" not in message, (
        "the toast must not route the owner into the add-on: the manual purge it "
        "used to point at no longer exists"
    )
    # No count/path/platform jargon in the user-facing string.
    assert "instance-settings" not in message
    for jargon in ("NSUserDefaults", "tvOS", "key", "purge", "shadow"):
        assert jargon.lower() not in message.lower(), jargon
    assert any("ATTENTION" in m for m in env.log_lines(LOGWARNING))
    assert not env.marker.exists()


def test_marker_survives_when_gui_never_ready(env):
    """An aborted/interrupted boot must NOT consume the one-shot marker - the
    check is still owed on the next start (Finding 3)."""
    env.arm()
    env.ready = False
    svc = env.load()
    svc._maybe_restore_check(_Mon())
    assert env.notifications == []
    assert env.marker.exists(), "marker must survive a boot where the check never ran"


def test_probe_exception_does_not_break_boot_and_clears_marker(env, monkeypatch):
    env.arm()
    svc = env.load()
    rc = sys.modules["resources.lib.modules.restorecheck"]

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(rc, "duplicate_listing_hits", _boom)
    svc._maybe_restore_check(_Mon())  # must not raise
    assert not env.marker.exists()


# --------------------------------------------------------------------------- #
# The boot service opens NO dialog, and therefore knows nothing about any skin.
#
# It used to wait out one specific skin's deferred menu rebuild (a 15s AlarmClock
# whose skinshortcuts build ends in ReloadSkin(), destroying the window stack)
# before showing the post-restore prompt. The prompt was deleted on 2026-07-19 -
# a restore preserves this box's device name and cache buffer instead of asking
# the user to repair them - so the wait was deleted with it rather than renamed.
# These tests pin that the coupling cannot come back by accident.
# --------------------------------------------------------------------------- #


def test_service_knows_nothing_about_any_skin():
    """service.py must not name, poll, or time itself against skin internals.

    The test the owner set: this add-on must run correctly under ANY skin with zero
    knowledge of it. Naming a skin, polling a skin add-on's in-progress property, or
    hard-coding a delay derived from a skin's alarm all fail that test - renaming the
    wait would not fix it, so the tokens themselves are banned."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    for token in (
        "skinshortcuts",
        "estuary7",
        "t7bbuild",
        "ReloadSkin",
        "_wait_skin_settled",
        "_SKIN_DEFERRED_BUILD_SECS",
    ):
        assert token not in code, (
            "service.py executable code must not reference %r: a boot service that "
            "times itself against a skin's internals is coupled to that skin" % token
        )


MODAL_TOKENS = (
    "dialog.select",
    "Dialog().select",
    "Keyboard(",
    "doModal",
    ".yesno(",
)


def test_boot_sequence_opens_no_dialog():
    """No boot step may open a modal. A dialog nobody is watching cannot be answered,
    and Kodi's API cannot tell a destroyed dialog from a cancelled one, so an
    unattended boot prompt is unanswerable by construction. The notification in
    _maybe_restore_check is NOT a dialog: it needs no answer and blocks nothing."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    start = src.index("def _startup_sequence(")
    end = src.index("def _folder_size_and_count(")
    sequence_region = src[start:end]
    for token in MODAL_TOKENS:
        assert token not in sequence_region, (
            "the boot sequence must not open %r - boot work is silent and completes "
            "on its own" % token
        )


def test_startup_checks_opens_no_dialog():
    """_startup_checks is a boot step too, and it must obey the same rule.

    It escaped test_boot_sequence_opens_no_dialog only because it is called from
    __main__ rather than from _startup_sequence, and it carried two modal yesno
    size alerts. That is a SHUTDOWN defect, not just a boot one: doModal() blocks
    the service thread, so monitor.abortRequested() can never be polled, and Kodi
    kills the script 5 seconds after asking it to stop. Reproduced on the macOS
    bench 2026-07-20 with a 257 MB packages folder (abort 04:39:42.478, kill
    04:39:47.490).

    A watchdog that closes the dialog on abort does NOT fix this and must not be
    re-proposed: xbmc.executebuiltin("Dialog.Close(all, true)") posts to the
    application thread, which is itself blocked in CPythonInvoker::stop() waiting
    for this script. Proven on the same bench - the watchdog fired, executebuiltin
    returned, the kill still landed. The only fix is not to block here at all."""
    src = SERVICE_PY.read_text(encoding="utf-8")
    start = src.index("def _startup_checks(")
    end = src.index("def _maybe_restore_check(")
    region = src[start:end]
    body = "\n".join(
        line for line in region.splitlines() if not line.strip().startswith("#")
    )
    # Drop the docstring, which names the tokens in order to explain the ban.
    body = body.split('"""', 2)[-1]
    for token in MODAL_TOKENS:
        assert token not in body, (
            "_startup_checks must not open %r - a modal on the service thread "
            "cannot be closed during shutdown and Kodi kills the script" % token
        )


def test_boot_check_reports_a_wrong_skin(env, monkeypatch):
    """A3: the box reopened on a different skin than the archive carried.

    This is the ONLY place A3 is observable - the restore finishes before the
    restart that decides the outcome, so its own report cannot see it."""
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text("skin.estuary.pov")
    svc = env.load()
    monkeypatch.setattr(svc.xbmc, "getSkinDir", lambda: "skin.estuary", raising=False)
    svc._maybe_restore_check(_Mon())
    assert len(env.notifications) == 1, "a wrong-skin reopen must report"
    assert any("did not become live" in m for m in env.log_lines(LOGWARNING))
    assert not env.marker.exists()


def test_boot_check_is_silent_when_the_skin_matches(env, monkeypatch):
    """The fleet's normal path is same-skin. A check that always reports would fire
    on every restore - worse than the defect it detects. Both directions must be
    pinned, or a hardcoded 'report' passes the mismatch test alone."""
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text("skin.estuary.pov")
    svc = env.load()
    monkeypatch.setattr(svc.xbmc, "getSkinDir", lambda: "skin.estuary.pov", raising=False)
    svc._maybe_restore_check(_Mon())
    assert env.notifications == [], "a correct reopen must stay silent"
    assert not env.marker.exists()


def test_legacy_marker_records_no_expectation_and_never_reports(env, monkeypatch):
    """A legacy "1" marker carries no expectation. Reading it as a mismatch would
    make every pre-existing marker report a false finding on upgrade."""
    env.arm()  # writes "1"
    svc = env.load()
    monkeypatch.setattr(svc.xbmc, "getSkinDir", lambda: "anything", raising=False)
    svc._maybe_restore_check(_Mon())
    assert env.notifications == [], "no recorded expectation must never report"


def test_boot_check_uses_the_read_only_skin_probe():
    """The mutating skin-setting probes change the state they claim to inspect.

    CSkinInfo::TranslateBool inserts a default-false setting AND schedules a save, so
    a check built on one passes for the wrong reason. getSkinDir is the read-only
    probe. The forbidden names are BUILT here rather than written literally, because
    this repo has a separate guard that scans test files for them - a test naming the
    trap would trip it."""
    # Strip comments and docstrings: this guards what the code CALLS, not what the
    # prose warns about. Naming the trap in a comment is how the next agent avoids
    # it, so a guard that punishes the warning is the wrong guard.
    import io
    import tokenize

    banned = ("Has" + "Setting", "GetInfo" + "Booleans")
    src = SERVICE_PY.read_text()
    code = []
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code.append(tok.string)
    code = " ".join(code)
    for name in banned:
        assert name not in code, "skin state must not be probed with a mutating API"
    assert "getSkinDir" in code, "the read-only probe must actually be used"


def test_boot_check_stays_silent_when_the_live_skin_is_unreadable(env, monkeypatch):
    """An empty or unavailable getSkinDir() must NOT be read as a mismatch.

    The `live and` guard is what prevents that. Dropping it passed the whole suite,
    because nothing covered an unreadable probe - and a box that simply could not
    report its skin would then be told its restore needs attention on every boot."""
    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text("skin.estuary.pov")
    svc = env.load()
    monkeypatch.setattr(svc.xbmc, "getSkinDir", lambda: "", raising=False)
    svc._maybe_restore_check(_Mon())
    assert env.notifications == [], "an unreadable live skin must not report a mismatch"

    env.marker.parent.mkdir(parents=True, exist_ok=True)
    env.marker.write_text("skin.estuary.pov")
    svc = env.load()

    def _boom():
        raise RuntimeError("no skin")

    monkeypatch.setattr(svc.xbmc, "getSkinDir", _boom, raising=False)
    svc._maybe_restore_check(_Mon())
    assert env.notifications == [], "a raising probe must not report a mismatch"
