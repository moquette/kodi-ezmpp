"""Verification suite for the post-restore popup removal + the device-name/buffer rules.

Owner decision 2026-07-19: DELETE the post-restore popup and stop cloning the SOURCE
box's `services.devicename` and `filecache.memorysize` onto the target.

Owner decision 2026-07-31: the two ids DIVERGE in how that is resolved, and this suite
is where the divergence is pinned. Same principle - the archive's value never wins -
but:

  * `services.devicename` is PRESERVED. The box keeps the name it already answers to.
  * `filecache.memorysize` is RESET to Kodi's own default, 20 MB. NOT the archive's
    number and NOT this box's own previous one either: the fleet mixes device classes
    whose right buffer differs, so no inherited number survives a restore or a wipe.
    The per-device recommendation is offered where it already lives, in the add-on's
    Video Cache Buffer menu (`tools.advancedSettings`), and the buffer only moves off
    20 when the user accepts that or types his own value. There is STILL no prompt.

An implementation that keeps treating the two ids identically is the failure mode this
suite exists to catch, which is why several tests below assert both facts about the
SAME restore run rather than one each.

What these tests hold the change to, one section each:

1. BOTH HALVES, FOR BOTH IDS. Skipping the live-apply is not enough on its own - the
   archive's value still sits in the restored guisettings.xml and wins at the next boot.
   Writing the file is not enough on its own either - Kodi's clean-shutdown flush writes
   live memory back over the file (the kodi-settings-clobber class, hardware-proven),
   which is why the RESET has to reach the live store too and not only the file. The
   end-to-end tests below fail if EITHER half is missing, for either id.
2. THE COUPLING IS GONE. An AST guard: EZM++'s BEHAVIOUR may not name a skin id or read
   a skin-internal window property. It parses CODE, not prose - comments are dropped by
   the parser and docstrings are excluded explicitly, so the comments that explain WHY
   the coupling is forbidden do not trip it.
3. NOTHING LEGITIMATE WAS STRIPPED. Pins on the things that must survive the deletion.
4. THE MARKER PATHS ARE FULLY UNWOUND. No dangling references, and a box carrying a
   stale marker file from an older build still boots cleanly. The buffer reset must not
   reopen any of this: it is silent by construction, asserted in section 1.
"""

import ast
import importlib
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).resolve().parent.parent / "script.ezmaintenanceplusplus"
MODULES = ADDON_ROOT / "resources" / "lib" / "modules"

# This box's live values: the NAME is what the preserve must keep; the BUFFER is what
# the reset must throw away just as firmly as the archive's.
LIVE_DEVICENAME = "Living Room ATV"
LIVE_BUFFER_MB = 150
# The archive's values: the SOURCE box's, discarded either way.
ARCHIVE_DEVICENAME = "Golden Image Box"
ARCHIVE_BUFFER_MB = 96
# Kodi's factory default, and the ONLY value a restored/wiped box may end on. Three
# DISTINCT numbers on purpose: with 20 equal to either of the others, "the reset ran"
# would be indistinguishable from "the restore bailed and left the box's own file" or
# from "the archive happened to carry the default", and the assertion would prove
# nothing. tools.KODI_DEFAULT_MB is pinned against this in section 1.
KODI_DEFAULT_BUFFER_MB = 20

DEVICENAME_SETTING = "services.devicename"
CACHE_SETTING = "filecache.memorysize"


# --------------------------------------------------------------------------- #
# A stateful fake of Kodi's live settings store, reached over JSON-RPC.
# --------------------------------------------------------------------------- #
class FakeLiveSettings:
    """Kodi's in-memory settings store, as Settings.* JSON-RPC sees it.

    Records every SetSettingValue so a test can assert what was live-applied - which is
    the whole of half A. `Settings.GetSettings` returns the expert-level list that
    _kodisettings.apply_guisettings reads for types and change detection.
    """

    def __init__(self):
        self.settings = {
            DEVICENAME_SETTING: {
                "id": DEVICENAME_SETTING,
                "type": "string",
                "value": LIVE_DEVICENAME,
            },
            CACHE_SETTING: {
                "id": CACHE_SETTING,
                "type": "integer",
                "value": LIVE_BUFFER_MB,
            },
            "lookandfeel.skin": {
                "id": "lookandfeel.skin",
                "type": "string",
                "value": "skin.estuary",
            },
            "audiooutput.volumesteps": {
                "id": "audiooutput.volumesteps",
                "type": "integer",
                "value": 90,
            },
        }
        self.set_calls = []

    def executeJSONRPC(self, req):
        try:
            d = json.loads(req)
        except Exception:
            return "{}"
        method = d.get("method")
        params = d.get("params") or {}
        if method == "Settings.GetSettings":
            return json.dumps({"result": {"settings": list(self.settings.values())}})
        if method == "Settings.GetSettingValue":
            sid = params.get("setting")
            if sid in self.settings:
                return json.dumps({"result": {"value": self.settings[sid]["value"]}})
            return json.dumps({"error": {"message": "unknown"}})
        if method == "Settings.SetSettingValue":
            sid = params.get("setting")
            val = params.get("value")
            self.set_calls.append((sid, val))
            if sid in self.settings:
                self.settings[sid]["value"] = val
                return json.dumps({"result": True})
            return json.dumps({"result": False})
        if method == "Addons.GetAddonDetails":
            return json.dumps({"result": {"addon": {"enabled": False}}})
        return json.dumps({"result": {}})

    def applied(self, sid):
        """Every value live-applied to `sid`, in order."""
        return [v for (s, v) in self.set_calls if s == sid]


@pytest.fixture
def env(monkeypatch, tmp_path):
    """The real wiz module over faked Kodi, with a STATEFUL settings store.

    Modeled on tests/test_roundtrip_backup_restore.py's fixture; the difference that
    matters is executeJSONRPC, which here is a real store instead of a stub returning
    "{}". Without that, half A is untestable: nothing records what was live-applied.
    """
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if name == "resources" or name.startswith("resources."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    live = FakeLiveSettings()
    # Every channel this add-on could ask the user a question through. The buffer reset
    # must be SILENT: the owner deleted the post-restore popup on 2026-07-19 and the
    # 2026-07-31 reset explicitly does not bring one back (a prompt here would also
    # reopen the keyboard-input defect class that took two sessions to diagnose). These
    # counters make "no prompt" an assertion instead of a claim.
    asked = {"select": 0, "input": 0, "keyboard": 0}

    xbmc = types.ModuleType("xbmc")
    xbmc.translatePath = lambda p: p.replace("special://", str(tmp_path) + "/")
    xbmc.getLocalizedString = lambda i: str(i)
    xbmc.getInfoLabel = lambda s: ""
    xbmc.getCondVisibility = lambda s: False
    xbmc.getSkinDir = lambda: "skin.estuary"
    xbmc.log = lambda *a, **k: None
    xbmc.executebuiltin = lambda *a, **k: None
    xbmc.executeJSONRPC = live.executeJSONRPC
    xbmc.LOGERROR = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGINFO = 3
    xbmc.LOGDEBUG = 4
    xbmc.LOGFATAL = 0
    xbmc.LOGNONE = 5
    xbmc.LOGNOTICE = 3
    xbmc.PLAYLIST_VIDEO = 1
    xbmc.sleep = lambda ms: None
    def _keyboard(*a, **k):
        asked["keyboard"] += 1
        return types.SimpleNamespace(
            doModal=lambda: None, isConfirmed=lambda: False, getText=lambda: ""
        )

    xbmc.Keyboard = _keyboard
    xbmc.PlayList = lambda *a, **k: types.SimpleNamespace(
        clear=lambda: None, add=lambda *a: None
    )
    xbmc.Player = lambda *a, **k: types.SimpleNamespace(play=lambda *a, **k: None)
    xbmc.Monitor = type(
        "Monitor",
        (),
        {"abortRequested": lambda self: False, "waitForAbort": lambda self, t: False},
    )

    xbmcaddon = types.ModuleType("xbmcaddon")

    class _FakeAddon:
        def getLocalizedString(self, i):
            return str(i)

        def getSetting(self, key):
            return ""

        def setSetting(self, key, value):
            pass

        def getAddonInfo(self, key):
            return {
                "id": "script.ezmaintenanceplusplus",
                "name": "EZ Maintenance++",
                "path": str(ADDON_ROOT),
                "profile": "special://profile/",
                "version": "0.0.0",
            }.get(key, "")

    xbmcaddon.Addon = _FakeAddon

    xbmcgui = types.ModuleType("xbmcgui")

    class _FakeDialogProgress:
        def create(self, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def close(self):
            pass

        def iscanceled(self):
            return False

    class _FakeDialog:
        def ok(self, *a, **k):
            return False

        def yesno(self, *a, **k):
            return False

        def notification(self, *a, **k):
            pass

        def select(self, *a, **k):
            asked["select"] += 1
            return -1

        def input(self, *a, **k):
            asked["input"] += 1
            return ""

    xbmcgui.DialogProgress = _FakeDialogProgress
    xbmcgui.DialogProgressBG = _FakeDialogProgress
    xbmcgui.Dialog = _FakeDialog
    xbmcgui.ListItem = lambda *a, **k: types.SimpleNamespace(
        setArt=lambda *a, **k: None, setProperty=lambda *a, **k: None
    )
    xbmcgui.ControlButton = lambda *a, **k: None
    xbmcgui.ControlImage = lambda *a, **k: None

    props = {}

    class _FakeWindow:
        def __init__(self, *a, **k):
            pass

        def getProperty(self, k):
            return props.get(k, "")

        def setProperty(self, k, v):
            props[k] = v

        def clearProperty(self, k):
            props.pop(k, None)

    xbmcgui.Window = _FakeWindow
    xbmcgui.WindowDialog = _FakeWindow

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = xbmc.translatePath
    xbmcvfs.exists = lambda p: Path(p).exists()
    xbmcvfs.mkdirs = lambda p: Path(p).mkdir(parents=True, exist_ok=True)
    xbmcvfs.mkdir = lambda p: Path(p).mkdir(parents=True, exist_ok=True)
    xbmcvfs.rmdir = lambda p: None
    xbmcvfs.delete = lambda p: None
    xbmcvfs.listdir = lambda p: ([], [])
    xbmcvfs.copy = lambda s, d: True
    xbmcvfs.File = lambda *a, **k: types.SimpleNamespace(
        read=lambda *a: b"", write=lambda *a: True, close=lambda: None, size=lambda: 0
    )

    xbmcplugin = types.ModuleType("xbmcplugin")
    xbmcplugin.addDirectoryItem = lambda *a, **k: None
    xbmcplugin.endOfDirectory = lambda *a, **k: None
    xbmcplugin.setContent = lambda *a, **k: None
    xbmcplugin.setProperty = lambda *a, **k: None
    xbmcplugin.setResolvedUrl = lambda *a, **k: None

    for name, mod in (
        ("xbmc", xbmc),
        ("xbmcaddon", xbmcaddon),
        ("xbmcgui", xbmcgui),
        ("xbmcvfs", xbmcvfs),
        ("xbmcplugin", xbmcplugin),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    wiz = importlib.import_module("resources.lib.modules.wiz")
    tools = importlib.import_module("resources.lib.modules.tools")
    return types.SimpleNamespace(
        wiz=wiz, tools=tools, live=live, tmp_path=tmp_path, props=props, asked=asked
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _guisettings_xml(devicename, buffer_mb, skin="skin.estuary"):
    """A guisettings.xml. `skin=None` omits lookandfeel.skin entirely, which is what a
    "kodi_settings"-style archive that never carried a skin looks like."""
    skin_line = '  <setting id="lookandfeel.skin">%s</setting>\n' % skin if skin else ""
    return (
        '<settings version="2">\n' + skin_line + '  <setting id="%s">%s</setting>\n'
        '  <setting id="%s">%d</setting>\n'
        '  <setting id="audiooutput.volumesteps">91</setting>\n'
        "</settings>\n" % (DEVICENAME_SETTING, devicename, CACHE_SETTING, buffer_mb)
    )


def _read_setting(path, sid):
    """The value of <setting id=sid> in a guisettings.xml on disk, or None."""
    import xml.etree.ElementTree as ET

    root = ET.parse(str(path)).getroot()
    for n in root.iter("setting"):
        if n.get("id") == sid:
            return (n.text or "").strip()
    return None


def _make_archive(
    tmp_path,
    devicename=ARCHIVE_DEVICENAME,
    buffer_mb=ARCHIVE_BUFFER_MB,
    skin="skin.estuary",
    name="backup.zip",
):
    """A minimal HOME-anchored full backup carrying the SOURCE box's guisettings."""
    zip_path = tmp_path / name
    with zipfile.ZipFile(str(zip_path), "w") as z:
        z.writestr(
            "userdata/guisettings.xml", _guisettings_xml(devicename, buffer_mb, skin)
        )
        z.writestr("userdata/sources.xml", "<sources/>\n")
        z.writestr(
            "userdata/backup_manifest.json",
            json.dumps(
                {
                    "created": "2026-07-19",
                    "source_os": "android",
                    "entries": 2,
                    "failed": [],
                }
            ),
        )
    return zip_path


def _drive_restore(env, monkeypatch, home, zip_path):
    """Run the real wiz.restore() against `home`, silencing the user-facing channels."""
    wiz = env.wiz
    home.mkdir(parents=True, exist_ok=True)
    (home / "userdata").mkdir(parents=True, exist_ok=True)
    # The box's PRE-restore guisettings.xml carries this box's own values, exactly as a
    # real target does. The archive is about to be extracted over it.
    (home / "userdata" / "guisettings.xml").write_text(
        _guisettings_xml(LIVE_DEVICENAME, LIVE_BUFFER_MB)
    )
    monkeypatch.setattr(wiz.control, "HOME", str(home))
    monkeypatch.setattr(wiz.control, "USERDATA", str(home / "userdata"))
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: None)
    monkeypatch.setattr(wiz.dialog, "ok", lambda *a, **k: True)
    monkeypatch.setattr(wiz.dialog, "notification", lambda *a, **k: None)
    if hasattr(wiz.ui, "error"):
        monkeypatch.setattr(wiz.ui, "error", lambda *a, **k: None)
    ret = wiz.restore(str(zip_path), confirm=False)
    gs = home / "userdata" / "guisettings.xml"
    _assert_the_archive_actually_landed(home, gs)
    return ret, gs


def _snapshotting_persist_one(monkeypatch, home):
    """Replace nsud.persist_one with a fake that records WHAT THE FILE SAID at the
    moment it was called, and return the recording list.

    Both settled values are snapshotted together, as a (device name, buffer) pair. On
    tvOS the vectored bytes ARE what the box boots, so a vector taken between the two
    writes would publish one correct value and one wrong one - a pair makes that
    visible, where a single-value snapshot could not distinguish it from a correct run.
    wiz imports nsud INSIDE the function, so the module object itself is patched.
    """
    seen = []

    def _fake_persist_one(rel, log=None, **k):
        gs = home / "userdata" / "guisettings.xml"
        snap = (None, None)
        if gs.exists():
            try:
                snap = (
                    _read_setting(gs, DEVICENAME_SETTING),
                    _read_setting(gs, CACHE_SETTING),
                )
            except Exception:
                snap = (None, None)
        seen.append((rel, snap))
        return True

    nsud_mod = importlib.import_module("resources.lib.modules.nsud")
    monkeypatch.setattr(nsud_mod, "persist_one", _fake_persist_one)
    return seen


def _assert_the_archive_actually_landed(home, gs):
    """Witness: prove the extract really happened before judging what it left behind.

    The preserve assertions here compare the file against THIS box's device name, and
    the box's pre-restore file already holds that name. So a restore that silently
    bailed - a bad zip, an early return, a fixture that never reached the extract -
    leaves the original file in place and every preserve test passes for entirely the
    wrong reason. That is the false-green class this project has already been bitten by
    twice.

    The RESET assertions have the same hazard from the other direction, which is why
    the three buffer numbers are all distinct (LIVE 150, ARCHIVE 96, DEFAULT 20): a
    "buffer is 20" assertion can be satisfied neither by the box's untouched
    pre-restore file nor by the archive's own value, so only a real reset can pass it.

    The archive carries audiooutput.volumesteps=91 while the box is live on 90, and a
    sources.xml the box does not have. Both must be present, or the extract did not run.
    """
    assert (home / "userdata" / "sources.xml").exists(), (
        "the restore never extracted the archive (no sources.xml on the box) - every "
        "preserve assertion in this test would pass vacuously against the box's own "
        "pre-restore guisettings.xml"
    )
    assert gs.exists(), "the restore left no guisettings.xml on the box"
    assert _read_setting(gs, "audiooutput.volumesteps") == "91", (
        "guisettings.xml was not replaced by the archive's copy (volumesteps is %r, "
        "the archive carries 91) - the extract did not land, so a matching device "
        "name below would prove nothing" % _read_setting(gs, "audiooutput.volumesteps")
    )


# --------------------------------------------------------------------------- #
# 1. BOTH HALVES, FOR BOTH IDS
# --------------------------------------------------------------------------- #
def test_kodi_default_buffer_is_what_this_suite_asserts(env):
    """Pin the number. Every reset assertion below is written against 20; if
    tools.KODI_DEFAULT_MB ever moves, they must move with it rather than silently
    start asserting a value the add-on no longer targets."""
    assert env.tools.KODI_DEFAULT_MB == KODI_DEFAULT_BUFFER_MB
    assert env.tools.CACHE_SETTING == CACHE_SETTING
    assert LIVE_BUFFER_MB != KODI_DEFAULT_BUFFER_MB != ARCHIVE_BUFFER_MB, (
        "the three buffer values must be distinct or a reset assertion can be "
        "satisfied by a restore that never ran"
    )


def test_half_a_archive_values_are_never_live_applied(env, monkeypatch, tmp_path):
    """HALF A, UNCHANGED BY THE RESET: apply_guisettings must not push the ARCHIVE's
    device name or buffer into Kodi's live store.

    Live-applying them is what makes the wrong values authoritative in memory; the
    clean-shutdown flush then writes them over whatever the file says, so half B alone
    cannot survive without this. Deleting either id from _kodisettings._BOOT_STATE_ONLY
    reddens this test - the buffer included, because "reset to 20" and "let the archive
    set it" are different wrong-value problems and only the first is fixed downstream.
    """
    home = tmp_path / "home"
    _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    assert ARCHIVE_DEVICENAME not in env.live.applied(DEVICENAME_SETTING), (
        "the archive's device name was live-applied - _BOOT_STATE_ONLY is not "
        "covering %s, so the SOURCE box's name became authoritative in memory and "
        "the clean-shutdown flush will write it back over the restored file"
        % DEVICENAME_SETTING
    )
    assert ARCHIVE_BUFFER_MB not in env.live.applied(CACHE_SETTING), (
        "the archive's cache buffer was live-applied - _BOOT_STATE_ONLY is not "
        "covering %s, so this box briefly ran the SOURCE box's number and only the "
        "reset's ordering saved it" % CACHE_SETTING
    )
    # Half A must be surgical: an ordinary setting still restores normally.
    assert 91 in env.live.applied("audiooutput.volumesteps"), (
        "an ordinary setting stopped being live-applied - _BOOT_STATE_ONLY was "
        "widened too far and the restore no longer restores"
    )


def test_half_b_the_file_keeps_the_name_and_resets_the_buffer(
    env, monkeypatch, tmp_path
):
    """HALF B, THE DIVERGENCE: the guisettings.xml left on disk after the restore must
    carry THIS box's device name, and Kodi's DEFAULT buffer.

    Skipping the live-apply alone is insufficient: the extract writes the archive's
    guisettings.xml onto the box, and that file is what the next boot reads.

    Until 2026-07-31 this asserted the buffer was PRESERVED (== LIVE_BUFFER_MB). The
    owner changed the contract: the fleet mixes device types whose right buffer differs,
    so no inherited number may survive a restore - the archive's or this box's own. The
    assertion below is the new contract, and it is deliberately hostile to both old
    answers, which is what makes it worth writing.
    """
    home = tmp_path / "home"
    _, gs = _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    assert gs.exists(), "the restore left no guisettings.xml on the box"
    assert _read_setting(gs, DEVICENAME_SETTING) == LIVE_DEVICENAME, (
        "the restored guisettings.xml carries %r, not this box's %r - the archive's "
        "device name wins at the next boot"
        % (_read_setting(gs, DEVICENAME_SETTING), LIVE_DEVICENAME)
    )
    got = _read_setting(gs, CACHE_SETTING)
    assert got == str(KODI_DEFAULT_BUFFER_MB), (
        "the restored guisettings.xml carries buffer %r, not Kodi's default %r. %s"
        % (
            got,
            KODI_DEFAULT_BUFFER_MB,
            {
                str(LIVE_BUFFER_MB): "It is this box's OWN previous buffer, so the "
                "reset was not applied and the old preserve behaviour is back.",
                str(ARCHIVE_BUFFER_MB): "It is the ARCHIVE's buffer, so the restore "
                "cloned the SOURCE box's number - neither half of the rule ran.",
            }.get(got, "It is neither box's value nor the default."),
        )
    )


def test_the_reset_reaches_kodis_live_store_too(env, monkeypatch, tmp_path):
    """The reset must land in Kodi's LIVE settings, not only in the file.

    This is the whole lesson of the kodi-settings-clobber class, already proven on
    hardware in this project: Kodi's clean-shutdown flush serializes its in-memory
    settings over guisettings.xml, so a file-only reset is written straight back to the
    box's old buffer at the very next exit and the user never sees 20 MB. A restore that
    only writes the file passes the half-B test above and still ships the bug, which is
    exactly why this assertion is separate from it.
    """
    home = tmp_path / "home"
    _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    assert KODI_DEFAULT_BUFFER_MB in env.live.applied(CACHE_SETTING), (
        "%s was never set to %d through Settings.SetSettingValue (values applied: "
        "%r). The file may say 20, but Kodi's live store still holds this box's old "
        "buffer and the clean-shutdown flush will write it back over the file."
        % (CACHE_SETTING, KODI_DEFAULT_BUFFER_MB, env.live.applied(CACHE_SETTING))
    )
    assert env.live.settings[CACHE_SETTING]["value"] == KODI_DEFAULT_BUFFER_MB, (
        "Kodi's live buffer ended on %r, not %d - something re-applied a value after "
        "the reset"
        % (env.live.settings[CACHE_SETTING]["value"], KODI_DEFAULT_BUFFER_MB)
    )


def test_the_name_is_preserved_in_the_same_run_the_buffer_is_reset(
    env, monkeypatch, tmp_path
):
    """THE DIVERGENCE PIN. One restore, four facts, asserted together.

    `services.devicename` and `filecache.memorysize` were treated IDENTICALLY until
    2026-07-31 - both in _BOOT_STATE_ONLY, both written back from the pre-extract
    capture. They now share only the PRINCIPLE (the archive's value never wins) and
    differ in the RESOLUTION. The cheapest way for a future edit to break this is to
    collapse them back into one rule, in either direction: re-preserve the buffer, or
    "simplify" by resetting both.

    Asserting all four in a single run is what makes that impossible to do quietly - a
    per-id test elsewhere could stay green while the other id silently changed sides.
    """
    home = tmp_path / "home"
    _, gs = _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    name = _read_setting(gs, DEVICENAME_SETTING)
    buffer_mb = _read_setting(gs, CACHE_SETTING)

    assert name == LIVE_DEVICENAME, (
        "the device name is no longer PRESERVED (got %r, this box is %r). The two ids "
        "were collapsed back together - the buffer's reset must not spread to the "
        "name, which the box answers to on the network." % (name, LIVE_DEVICENAME)
    )
    assert name != ARCHIVE_DEVICENAME, "the archive's device name won"
    assert buffer_mb == str(KODI_DEFAULT_BUFFER_MB), (
        "the buffer is no longer RESET (got %r, expected %r). The two ids were "
        "collapsed back together - preserving the buffer is exactly the behaviour the "
        "owner replaced on 2026-07-31."
        % (buffer_mb, str(KODI_DEFAULT_BUFFER_MB))
    )
    assert buffer_mb != str(LIVE_BUFFER_MB), (
        "the buffer was preserved from this box, not reset"
    )


def test_the_reset_asks_the_user_nothing(env, monkeypatch, tmp_path):
    """No prompt. Menu only.

    The owner deleted the post-restore popup on 2026-07-19 and the 2026-07-31 reset
    does not bring one back in any form - no dialog, no keyboard, no chooser. The
    recommendation reaches the user only where it already lives, in the Video Cache
    Buffer menu, on demand. A prompt here would also reopen the keyboard-input defect
    class that took two sessions to diagnose (section 4 records that machinery).
    """
    home = tmp_path / "home"
    _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    assert env.asked == {"select": 0, "input": 0, "keyboard": 0}, (
        "the restore asked the user something (%r). The buffer reset is silent by "
        "contract; the recommendation is offered on demand in the menu, never pushed."
        % (env.asked,)
    )


def test_the_reset_helper_names_no_user_facing_channel():
    """The same guarantee by construction, so it cannot be reintroduced behind a
    condition the runtime test above never reaches.

    tools.reset_cache_buffer runs mid-restore and mid-wipe, where any modal is either
    invisible or destructive. It may log; it may not ask."""
    src = (MODULES / "tools.py").read_text()
    fn = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.FunctionDef) and node.name == "reset_cache_buffer":
            fn = node
    assert fn is not None, "tools.reset_cache_buffer is gone"
    forbidden = ("dialog", "Dialog", "_get_keyboard", "Keyboard", "notification")
    offenders = []
    for node in ast.walk(fn):
        name = None
        if isinstance(node, ast.Attribute):
            name = node.attr
        elif isinstance(node, ast.Name):
            name = node.id
        if name in forbidden:
            offenders.append((getattr(node, "lineno", 0), name))
    assert not offenders, (
        "reset_cache_buffer touches a user-facing channel at %r - it must be silent"
        % offenders
    )


def test_the_reset_leaves_advancedsettings_xml_alone(env, monkeypatch, tmp_path):
    """The MENU's "Reset to Kodi default" deletes a stale advancedsettings.xml <cache>;
    the restore/wipe reset deliberately does NOT, and this pins that decision.

    Three reasons, none of them cosmetic: the block is deprecated and IGNORED on Omega
    so deleting it moves nothing towards 20 MB; _clean_stale_advancedsettings deletes
    the WHOLE FILE, which on a restore would destroy archive-carried network/video
    settings that have nothing to do with the cache; and on tvOS xbmcvfs.delete cannot
    delete a userdata xml at all - it drops the NSUserDefaults key, reports success and
    leaves the POSIX file, which is precisely the one-layer asymmetry the backup/restore
    contract forbids.
    """
    calls = []
    monkeypatch.setattr(
        env.tools, "_clean_stale_advancedsettings", lambda: calls.append(1)
    )
    home = tmp_path / "home_adv"
    _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    assert calls == [], (
        "the restore called _clean_stale_advancedsettings. It deletes the whole "
        "advancedsettings.xml, so on a restore it would destroy settings the archive "
        "just delivered - and on tvOS it would leave the POSIX file behind anyway."
    )


def test_the_wipe_path_keeps_the_name_and_resets_the_buffer(
    env, monkeypatch, tmp_path
):
    """The clean-clone path wipes the box before extracting. The capture must happen
    BEFORE the wipe, or there is nothing left to preserve - and the reset must still
    land afterwards, on a box whose guisettings.xml was destroyed and re-extracted.

    Same divergence as above, driven through wipe=True.
    """
    wiz = env.wiz
    home = tmp_path / "home_wipe"
    zip_path = _make_archive(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "userdata").mkdir(parents=True, exist_ok=True)
    (home / "userdata" / "guisettings.xml").write_text(
        _guisettings_xml(LIVE_DEVICENAME, LIVE_BUFFER_MB)
    )

    wiped = {"done": False}

    # The REAL wipe entry point: restore()'s internal wipe pass calls onetap._wipe.
    # Patching speculative names on wiz with raising=False would silently match
    # nothing and quietly reduce this test to a duplicate of half B.
    onetap = importlib.import_module("resources.lib.modules.onetap")

    def _fake_wipe(*a, **k):
        wiped["done"] = True
        # A wipe destroys the box's guisettings.xml; only a value captured BEFORE this
        # point can survive to be written back.
        gs = home / "userdata" / "guisettings.xml"
        if gs.exists():
            gs.unlink()
        return True

    monkeypatch.setattr(onetap, "_wipe", _fake_wipe)

    monkeypatch.setattr(wiz.control, "HOME", str(home))
    monkeypatch.setattr(wiz.control, "USERDATA", str(home / "userdata"))
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: None)
    monkeypatch.setattr(wiz.dialog, "ok", lambda *a, **k: True)
    monkeypatch.setattr(wiz.dialog, "notification", lambda *a, **k: None)
    if hasattr(wiz.ui, "error"):
        monkeypatch.setattr(wiz.ui, "error", lambda *a, **k: None)

    wiz.restore(str(zip_path), confirm=False, wipe=True)
    gs = home / "userdata" / "guisettings.xml"

    assert wiped["done"], (
        "the wipe never ran, so this test never exercised the clean-clone path and "
        "was only a second copy of the half-B test"
    )
    _assert_the_archive_actually_landed(home, gs)
    assert _read_setting(gs, DEVICENAME_SETTING) == LIVE_DEVICENAME, (
        "the device name did not survive the wipe path - the capture must happen "
        "before the wipe, not after"
    )
    assert _read_setting(gs, CACHE_SETTING) == str(KODI_DEFAULT_BUFFER_MB), (
        "the wipe path left buffer %r on the box, not Kodi's default %r"
        % (_read_setting(gs, CACHE_SETTING), KODI_DEFAULT_BUFFER_MB)
    )
    assert KODI_DEFAULT_BUFFER_MB in env.live.applied(CACHE_SETTING), (
        "the wipe path never reset Kodi's LIVE buffer; the shutdown flush will write "
        "this box's old number back over the restored file"
    )


def test_a_never_named_box_is_not_given_an_empty_device_name(
    env, monkeypatch, tmp_path
):
    """The no-first-run-special-case claim, tested rather than trusted.

    The design rests on "a fresh box is already named Kodi, so preserving covers every
    case". If the live read ever yields an empty string (a box that has never been
    named, a JSON-RPC failure), the preserve must NOT write an empty device name over
    the restored one - an empty name is worse than the archive's name.
    """
    env.live.settings[DEVICENAME_SETTING]["value"] = ""
    home = tmp_path / "home_unnamed"
    _, gs = _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    got = _read_setting(gs, DEVICENAME_SETTING)
    # Assert the DOCUMENTED behaviour: an unreadable value is OMITTED from the capture,
    # so the write-back leaves the archive's value standing. Asserting merely
    # "not empty" would be satisfied by the archive's own value even if the whole
    # preserve feature were deleted, and would prove nothing.
    assert got == ARCHIVE_DEVICENAME, (
        "with an unreadable live device name the restored file should keep the "
        "ARCHIVE's %r (capture omits what it cannot read), but it holds %r. An "
        "empty or invented device name is worse than the archive's."
        % (ARCHIVE_DEVICENAME, got)
    )
    assert got not in ("", None), "the preserve blanked the device name"
    # The RESET depends on nothing captured, so an empty capture must not disable it.
    # The old code returned early when the capture came back empty; keeping that early
    # return would silently leave the SOURCE box's buffer on every box whose device
    # name could not be read.
    assert _read_setting(gs, CACHE_SETTING) == str(KODI_DEFAULT_BUFFER_MB), (
        "an unreadable device name suppressed the buffer reset (buffer is %r). The "
        "reset uses no captured value and must run regardless."
        % _read_setting(gs, CACHE_SETTING)
    )


def test_preserve_write_is_vectored_into_nsuserdefaults_on_tvos(
    env, monkeypatch, tmp_path
):
    """On tvOS the NSUserDefaults key SHADOWS guisettings.xml and Kodi NEVER copies a
    key back to disk. A write that only reaches the POSIX file is therefore INVISIBLE
    on an Apple TV: the stale key wins at the next boot and the box comes back with the
    SOURCE box's device name and whatever buffer the key holds.

    The sanctioned pattern already used by _apply_boot_skin is write_guisetting()
    followed by nsud.persist_one("guisettings.xml"). This test asserts the vector
    happens AFTER BOTH settled values are in the file - a persist_one that ran between
    them publishes one correct value and one wrong one, which is worse than none.

    This is the bug class that destroyed user data on 2026-07-08 and 2026-07-14. The
    raw-writer lint cannot catch it, because write_guisetting is allowlisted.
    """
    wiz = env.wiz
    home = tmp_path / "home_tvos"
    monkeypatch.setattr(wiz, "_source_os", lambda *a, **k: "tvos", raising=False)

    seen = _snapshotting_persist_one(monkeypatch, home)

    _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    gs_calls = [pair for (rel, pair) in seen if str(rel).endswith("guisettings.xml")]
    assert gs_calls, (
        "the restore never vectored guisettings.xml into NSUserDefaults. On tvOS the "
        "settled device name and buffer are written to a POSIX file that the stale "
        "key shadows - the Apple TV boots with the SOURCE box's values."
    )
    assert (LIVE_DEVICENAME, str(KODI_DEFAULT_BUFFER_MB)) in gs_calls, (
        "guisettings.xml was vectored, but never once while it carried BOTH this "
        "box's preserved device name %r and the reset buffer %r (vectored "
        "(name, buffer) snapshots: %r). Whichever value was missing is the one "
        "NSUserDefaults does not hold, and on tvOS the key is what the box boots."
        % (LIVE_DEVICENAME, str(KODI_DEFAULT_BUFFER_MB), gs_calls)
    )


def test_preserve_vectors_on_tvos_even_when_the_archive_carries_no_skin(
    env, monkeypatch, tmp_path
):
    """The preserve must vector guisettings.xml ITSELF, not lean on _apply_boot_skin.

    _apply_boot_skin returns early when the archive names no skin (`if not target:
    return`), BEFORE its own persist_one. So on an archive with no lookandfeel.skin -
    a "kodi_settings" style backup - the preserve's vector is the ONLY one that runs.
    Without it, an Apple TV keeps a stale NSUserDefaults key that SHADOWS the file and
    boots wearing the SOURCE box's name.

    The with-skin case cannot detect this: _apply_boot_skin's later vector masks a
    missing preserve vector entirely, which is exactly why that mutation went uncaught
    until this test existed.
    """
    wiz = env.wiz
    home = tmp_path / "home_noskin"
    monkeypatch.setattr(wiz, "_source_os", lambda *a, **k: "tvos", raising=False)

    seen = _snapshotting_persist_one(monkeypatch, home)

    zip_path = _make_archive(tmp_path, skin=None)
    _drive_restore(env, monkeypatch, home, zip_path)

    gs_calls = [pair for (rel, pair) in seen if str(rel).endswith("guisettings.xml")]
    assert (LIVE_DEVICENAME, str(KODI_DEFAULT_BUFFER_MB)) in gs_calls, (
        "with no skin in the archive, guisettings.xml was never vectored while it "
        "carried this box's preserved device name AND the reset buffer (snapshots: "
        "%r). _apply_boot_skin returns early here, so the preserve MUST vector on its "
        "own - otherwise the stale NSUserDefaults key shadows the disk file and wins "
        "on tvOS." % gs_calls
    )


def test_preserve_captures_the_setting_not_the_decorated_friendly_name(
    env, monkeypatch, tmp_path
):
    """The capture must read Settings.GetSettingValue, not a friendly-name API.

    Kodi source (Omega, xbmc/utils/SystemInfo.cpp:1327-1338): CSysInfo::GetDeviceName()
    returns "Kodi (hostname)" whenever the setting still equals the app name, while the
    SETTING itself stays "Kodi". Capturing the decorated form and writing it back
    permanently bakes the hostname into the setting - after which Kodi stops decorating
    and the name silently diverges across boxes.

    A default-named box is exactly the first-ever-run case, so this is where it bites.
    """
    env.live.settings[DEVICENAME_SETTING]["value"] = "Kodi"
    monkeypatch.setitem(
        sys.modules["xbmc"].__dict__,
        "getInfoLabel",
        lambda s: "Kodi (livingroom.local)" if "FriendlyName" in s else "",
    )
    home = tmp_path / "home_friendly"
    _, gs = _drive_restore(env, monkeypatch, home, _make_archive(tmp_path))

    got = _read_setting(gs, DEVICENAME_SETTING)
    assert got == "Kodi", (
        "the preserve stored %r. It must capture the SETTING via "
        "Settings.GetSettingValue, not the decorated friendly name - writing the "
        "hostname-suffixed form back into services.devicename is permanent." % got
    )


# --------------------------------------------------------------------------- #
# 2. THE COUPLING IS GONE (AST: code, not prose)
# --------------------------------------------------------------------------- #
def _addon_py_files():
    files = [ADDON_ROOT / "service.py", ADDON_ROOT / "default.py"]
    files += sorted(MODULES.glob("*.py"))
    files = [f for f in files if f.exists()]
    # Without this, a wrong ADDON_ROOT makes every scan below iterate an empty list
    # and pass vacuously - the guards would report "no coupling" having read nothing.
    assert len(files) > 5, (
        "found only %d add-on source files under %s - the scan target is wrong and "
        "every guard built on it would pass without reading anything"
        % (len(files), ADDON_ROOT)
    )
    return files


def _non_docstring_strings(tree):
    """Every string CONSTANT in the module that is not a docstring.

    Comments never reach the AST at all, and docstrings are the first statement of a
    module / class / function, so excluding those leaves only strings the code actually
    evaluates. This is the distinction the guard depends on: a comment explaining why
    the skin coupling is forbidden must not trip the guard that forbids it.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = getattr(node, "body", None)
            if not body:
                continue
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                if isinstance(first.value.value, str):
                    docstrings.add(id(first.value))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append((getattr(node, "lineno", 0), node.value))
    return out


# Skin-internal names EZM++'s behaviour may never mention. These are the skin's private
# vocabulary: its alarm name, its skinshortcuts build flag, and its reload builtin.
_FORBIDDEN_TOKENS = ("t7bbuild", "skinshortcuts-isrunning", "ReloadSkin")

# A hardcoded skin ID, e.g. "skin.estuary7". Note this does NOT match the core Kodi
# setting "lookandfeel.skin" (no dot follows "skin" there) nor the add-on data path
# "addon_data/script.skinshortcuts/" (that is "script.", not "skin."), so the two
# legitimate uses need no allowlist.
import re  # noqa: E402

# A file called skin.xml is a Kodi resource filename, not a skin add-on id; the
# negative lookahead keeps the guard on identities rather than filenames.
_SKIN_ID_RE = re.compile(
    r"\bskin\.(?!xml\b|py\b|json\b|txt\b|zip\b|png\b|jpg\b)[A-Za-z0-9_]+"
)

# EZM++ may read and write its OWN namespaced home-window properties, and nothing else.
_WINDOW_PROP_RE = re.compile(r"Window\(10000\)\.Property\(([^)]*)\)")
_OWN_PROP_PREFIXES = ("ezm", "ezmaintenance")


# The ONE module allowed to name a skin id, by owner decision 2026-09-26 (the
# Estuary POV to Estuary++ rename, .claude/plans/estuary-plusplus-rename.md
# section 3.3): every box migrates itself, and this add-on's boot service is
# the only thing that runs on every box. skinmigrate.py exists to name exactly
# the two ids of that rename and nothing else; it is transitional and goes
# when the old id is retired (plan stage E). The guard still holds for every
# other file, and the guard's own teeth test below is unchanged.
_RENAME_EXEMPT = {"skinmigrate.py"}


def test_addon_behaviour_names_no_skin_id_and_no_skin_internal_property():
    """The owner's decoupling test, mechanised.

    Could this skin run with NO EZM++ installed, and could EZM++ run under ANY skin,
    both correct with zero knowledge of the other? EZM++'s side of that is: its
    executed code never names a skin id, never reads a skin-internal window property,
    and never invokes a skin builtin. Prose about the coupling is fine and is
    deliberately not matched - only evaluated strings are checked.
    """
    offenders = []
    for path in _addon_py_files():
        if path.name in _RENAME_EXEMPT:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, s in _non_docstring_strings(tree):
            for tok in _FORBIDDEN_TOKENS:
                if tok in s:
                    offenders.append((path.name, lineno, "skin-internal name %r" % tok))
            m = _SKIN_ID_RE.search(s)
            if m:
                offenders.append(
                    (path.name, lineno, "hardcoded skin id %r" % m.group(0))
                )
            for prop in _WINDOW_PROP_RE.findall(s):
                name = prop.strip().strip("'\"").lower()
                if not name.startswith(_OWN_PROP_PREFIXES):
                    offenders.append(
                        (path.name, lineno, "foreign window property %r" % prop.strip())
                    )
    assert not offenders, (
        "EZM++'s BEHAVIOUR still knows about the skin:\n"
        + "\n".join("  %s:%s  %s" % o for o in offenders)
        + "\nEZM++ must run correctly under ANY skin. Move the knowledge out of "
        "executed code; a comment explaining the hazard is fine."
    )


def test_the_coupling_guard_actually_has_teeth():
    """Self-test: the guard must catch the exact coupling that was deleted, and must
    NOT catch the prose explaining it.

    Without this, a guard that silently matches nothing passes forever.
    """
    coupled = (
        "import xbmc\n"
        "def f():\n"
        '    return xbmc.getCondVisibility("String.IsEqual('
        'Window(10000).Property(skinshortcuts-isrunning),True)")\n'
    )
    prose = (
        "# skin.estuary7 arms AlarmClock(t7bbuild,...) whose rebuild ends in\n"
        "# ReloadSkin(). We must never depend on that.\n"
        "def f():\n"
        '    """Waits. skin.estuary7\'s t7bbuild alarm ends in ReloadSkin()."""\n'
        "    return True\n"
    )

    def scan(src):
        found = []
        tree = ast.parse(src)
        for lineno, s in _non_docstring_strings(tree):
            if any(t in s for t in _FORBIDDEN_TOKENS):
                found.append(s)
            if _SKIN_ID_RE.search(s):
                found.append(s)
            for prop in _WINDOW_PROP_RE.findall(s):
                if not prop.strip().lower().startswith(_OWN_PROP_PREFIXES):
                    found.append(s)
        return found

    assert scan(coupled), "the guard does not catch the coupling it exists to forbid"
    assert not scan(prose), (
        "the guard fires on COMMENTS and DOCSTRINGS - it must parse code, not prose, "
        "or it will block the note explaining why the coupling is forbidden"
    )


# --------------------------------------------------------------------------- #
# 3. NOTHING LEGITIMATE WAS STRIPPED
# --------------------------------------------------------------------------- #
def test_restorecheck_still_probes_the_skinshortcuts_duplicate():
    """restorecheck.py's duplicate probing is add-on DATA, not skin internals, and must
    survive the decoupling."""
    src = (MODULES / "restorecheck.py").read_text()
    assert "special://profile/addon_data/script.skinshortcuts/" in src, (
        "the skinshortcuts duplicate probe was stripped - it is an addon_data path, "
        "not a skin coupling, and it is how a duplicate listing is detected"
    )


def test_skin_settings_reapply_and_boot_skin_survive():
    """wiz's restored-skin handling must survive: it is generic (it reads whatever skin
    the ARCHIVE names), not knowledge of any particular skin."""
    src = (MODULES / "wiz.py").read_text()
    for fn in ("_read_skin_settings", "_apply_skin_settings", "_apply_boot_skin"):
        assert ("def %s(" % fn) in src, "%s was stripped from wiz.py" % fn
    assert 'node.get("id") == "lookandfeel.skin"' in src, (
        "the lookandfeel.skin READ was stripped - restore can no longer tell which "
        "skin the archive wants"
    )
    # The WRITE is a distinct fact from the READ, so assert it distinctly: find the
    # write_guisetting call that persists the skin. Grepping for the bare id would be
    # satisfied by the READ assertion's own substring and prove nothing new.
    tree = ast.parse(src)
    wrote_skin = any(
        isinstance(n, ast.Call)
        and getattr(n.func, "attr", "") == "write_guisetting"
        and any(
            isinstance(a, ast.Constant) and a.value == "lookandfeel.skin"
            for a in n.args
        )
        for n in ast.walk(tree)
    )
    assert wrote_skin, (
        "the lookandfeel.skin WRITE was stripped - no write_guisetting(..., "
        "'lookandfeel.skin', ...) call remains, so the restored skin is never "
        "persisted to disk and the box reopens on the old one"
    )


def test_boot_state_only_keeps_the_skin_and_its_reason():
    """_kodisettings.py:56-63 - the lookandfeel.skin boot-state rule and the comment
    recording the atv2 reproduction that produced it."""
    src = (MODULES / "_kodisettings.py").read_text()
    # Scope the membership check to the _BOOT_STATE_ONLY ASSIGNMENT itself. Grepping the
    # whole module would be satisfied by any passing mention of the id in a comment,
    # so it would still pass with lookandfeel.skin removed from the frozenset.
    members = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_BOOT_STATE_ONLY" for t in node.targets
        ):
            members = {
                c.value
                for c in ast.walk(node.value)
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            }
    assert members is not None, (
        "_BOOT_STATE_ONLY is no longer a module-level assignment"
    )
    assert "lookandfeel.skin" in members, (
        "lookandfeel.skin was dropped from _BOOT_STATE_ONLY (members: %r) - "
        "live-applying it starts Kodi's unanswerable keep-skin countdown and REVERTS "
        "the restored skin" % sorted(members)
    )
    assert {DEVICENAME_SETTING, CACHE_SETTING} <= members, (
        "a hardware-scoped id is not in _BOOT_STATE_ONLY (members: %r) - half A is "
        "gone. The BUFFER belongs here too even though it is now RESET rather than "
        "preserved: half A stops the archive's number from becoming authoritative in "
        "Kodi's live memory, and the reset downstream is not a substitute for that."
        % sorted(members)
    )
    assert "keep this" in src and "countdown" in src, (
        "the comment recording WHY lookandfeel.skin is boot-state-only was stripped; "
        "it is the record of a hardware reproduction on atv2 (2026-07-17)"
    )


def test_the_buffer_recommendation_still_lives_in_the_menu(env, monkeypatch):
    """The reset removed the buffer from the restore's OUTPUT, not from the product.

    "No prompt. Menu only." is only half a decision if the menu stops offering the
    recommendation. tools.advancedSettings() is where the user meets it, and it must
    keep all three routes off Kodi's default: the per-device recommendation, a typed
    value, and an explicit reset. Driven through the real function with the chooser
    cancelled, so this reads what the user is actually shown.
    """
    tools = env.tools
    offered = []
    monkeypatch.setattr(
        tools.dialog,
        "select",
        lambda heading, options, *a, **k: offered.append((heading, options)) or -1,
    )
    monkeypatch.setattr(tools, "_get_cache_mb", lambda: KODI_DEFAULT_BUFFER_MB)
    monkeypatch.setattr(tools, "_recommended_mb", lambda: 64)

    tools.advancedSettings()

    assert offered, "the Video Cache Buffer menu never opened its chooser"
    heading, options = offered[0]
    assert len(options) == 3, options
    assert "recommended" in options[0].lower() and "64" in options[0], (
        "the per-device recommendation is gone from the menu (%r). It is now the ONLY "
        "place the user is offered one, because the restore no longer asks." % options
    )
    assert "enter a value" in options[1].lower(), options
    assert "%d" % KODI_DEFAULT_BUFFER_MB in options[2], options
    assert "%d" % KODI_DEFAULT_BUFFER_MB in heading, (
        "the heading must still name Kodi's default, which is where a restored box "
        "now sits: %r" % heading
    )


def test_ezmpp_keeps_its_own_window_properties():
    """EZM++'s own namespaced Window(10000) properties are its diagnostics surface and
    are not a skin coupling."""
    wiz_src = (MODULES / "wiz.py").read_text()
    for prop in ("ezm_skin_reapply", "ezm_boot_skin", "ezm_restore_verdict"):
        assert prop in wiz_src, "EZM++'s own property %s was stripped" % prop


def test_the_contract_fingerprint_machinery_is_gone_and_stays_gone():
    """ezm_contract_fingerprint had exactly ONE reader, tools/verify_device.py.

    That tool, its verification/*.json artifacts and the test pinning its file list
    against service.py's were all DELETED on 2026-07-21 with the device-verification
    gate. What was left behind was a hash nobody read, published at every boot under a
    comment naming two files that no longer exist - which tells the next agent a gate
    is watching when none is. Removed 2026-07-22.

    Reinstating it needs a READER first, so this fails if the property, the file list
    or the publisher comes back on its own. Asserted over the parsed CODE, not the
    text: the comment recording the removal names all four, and a text scan would
    either fail on the record of the removal or force the record to be deleted."""
    import ast

    svc_src = (ADDON_ROOT / "service.py").read_text()
    tree = ast.parse(svc_src)
    defined = set()
    literals = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            defined.add(node.name)
        elif isinstance(node, ast.Name):
            defined.add(node.id)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            literals.add(node.value)
    for gone in (
        "_CONTRACT_FILES",
        "CONTRACT_FINGERPRINT_PROPERTY",
        "_contract_fingerprint",
        "_publish_contract_fingerprint",
    ):
        assert gone not in defined, (
            "%s is back in service.py with nothing reading it. If a verification gate "
            "is wanted again, land the reader first." % gone
        )
    assert "ezm_contract_fingerprint" not in literals, (
        "the fingerprint property is being published again and nothing reads it"
    )
    # The bytecode purge is NOT part of that and must survive: a stale .pyc makes a
    # box run the OLD code after a correct upgrade (observed on atv2, 2026-07-19).
    assert "_purge_stale_bytecode" in svc_src


def test_generic_kodi_api_paragraph_survives():
    """tools.py's paragraph on the generic Kodi API limitation (a destroyed dialog and
    a cancelled one are indistinguishable) is general knowledge, not skin knowledge."""
    src = (MODULES / "tools.py").read_text()
    assert "def _keyboard_result(" in src, (
        "_keyboard_result was stripped - it is the generic non-answer-safe keyboard "
        "helper, not part of the deleted post-restore prompt"
    )
    # Pin the SUBSTANCE, not the wording. The two API facts that make a non-answer
    # undetectable are isConfirmed() and getText(); a docstring that still names both
    # is still carrying the knowledge, however it is phrased. Matching an exact
    # sentence would only test prose - and prose is cheap to reword around a pin.
    tree = ast.parse(src)
    doc = ""
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_keyboard_result":
            doc = ast.get_docstring(node) or ""
    assert len(doc) > 200, (
        "_keyboard_result's docstring was gutted - it is the record of a generic "
        "Kodi API limitation, not commentary on the deleted prompt"
    )
    assert "isConfirmed" in doc and "getText" in doc, (
        "the paragraph no longer records HOW a non-answer is undetectable "
        "(isConfirmed() False with the text still in getText()). That is generic "
        "Kodi behaviour and the reason several guards in this add-on exist."
    )
    # The knowledge must have been GENERALIZED, not merely reworded around the skin.
    assert "skin.estuary7" not in src and "AlarmClock" not in src, (
        "the generic paragraph still cites the skin by name. The limitation is a "
        "property of Kodi's API, not of any one skin; naming the skin here is the "
        "coupling this change exists to remove"
    )


# --------------------------------------------------------------------------- #
# 4. THE MARKER PATHS ARE FULLY UNWOUND
# --------------------------------------------------------------------------- #
_DELETED_SYMBOLS = (
    "prompt_after_restore",
    "prompt_devicename_after_restore",
    "prompt_buffer_after_restore",
    "_maybe_prompt_after_restore",
    "_SKIN_DEFERRED_BUILD_SECS",
    "_wait_skin_settled",
    "BUFFER_PROMPT_MARKER",
    "mark_buffer_prompt_pending",
    "buffer_prompt_pending",
    "clear_buffer_prompt_marker",
    "_prompt_attempts",
    "_record_prompt_attempt",
    "_PROMPT_MAX_ATTEMPTS",
    "_PROMPT_MAX_BOOTS",
)


def test_no_dangling_reference_to_any_deleted_symbol():
    """Every deleted symbol must be gone from EXECUTED code everywhere in the add-on.

    A leftover call site is an AttributeError at boot on a real box. Names inside
    comments and docstrings are fine - that is how the deletion gets explained.
    """
    offenders = []
    for path in _addon_py_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            name = None
            if isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = node.name
            if name in _DELETED_SYMBOLS:
                offenders.append((path.name, getattr(node, "lineno", 0), name))
        # A string reference to the marker filename is just as dangling.
        for lineno, s in _non_docstring_strings(tree):
            if ".ezm_buffer_prompt" in s:
                offenders.append((path.name, lineno, ".ezm_buffer_prompt path"))
    assert not offenders, (
        "deleted popup/marker symbols are still referenced by executed code:\n"
        + "\n".join("  %s:%s  %s" % o for o in offenders)
    )


def test_first_run_arming_no_longer_arms_the_deleted_marker():
    """tools.py armed the SAME marker on the add-on's FIRST-EVER RUN, not only after a
    restore. That path must die cleanly too - it is the one most likely to be missed,
    because it is not on the restore path at all.
    """
    tools_src = (MODULES / "tools.py").read_text()
    tree = ast.parse(tools_src)
    defined = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assigned = {
        t.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    # arm_first_run_tuneup existed ONLY to arm the now-deleted prompt on a brand-new
    # box, and FIRST_RUN_FLAG was the state it kept. Both go with it. Asserting this
    # directly matters: a conditional check keyed on the function's presence would
    # assert nothing at all once the function is gone.
    assert "arm_first_run_tuneup" not in defined, (
        "arm_first_run_tuneup still exists. It armed the deleted buffer-prompt marker "
        "on the add-on's FIRST-EVER RUN - a path off the restore flow entirely, and "
        "the one most likely to be missed."
    )
    assert "FIRST_RUN_FLAG" not in assigned, (
        "FIRST_RUN_FLAG still exists - it is the state arm_first_run_tuneup kept, and "
        "with the prompt gone nothing reads it. A flag nothing reads is stranded state."
    )


def test_a_stale_marker_file_from_an_older_build_is_harmless(env, tmp_path):
    """A box upgrading from a build that armed the marker still has the file. Nothing
    may read it, and its presence must not change any behaviour.

    This is the "leaves nothing stranded" half: not that the file is impossible, but
    that it is inert.

    This is a SOURCE-LEVEL check, stated plainly rather than dressed up as a runtime
    one: the guarantee is that no code path can consult the file, which is proven by
    the symbol and string scans below, not by dropping a file on disk and watching
    nothing happen. A file the add-on never names cannot influence it, so a runtime
    "it still worked" assertion would be theatre.
    """
    tools = importlib.import_module("resources.lib.modules.tools")
    for sym in _DELETED_SYMBOLS:
        assert not hasattr(tools, sym), (
            "tools.%s still exists - the marker machinery was not fully removed, so "
            "a stale marker from an older build is still live state" % sym
        )
    # No module may name the marker file, in code OR in prose that could be copied
    # back into code. The scan covers the whole add-on, not just tools.py.
    namers = []
    for path in _addon_py_files():
        for lineno, s in _non_docstring_strings(ast.parse(path.read_text())):
            if ".ezm_buffer_prompt" in s:
                namers.append("%s:%s" % (path.name, lineno))
    assert not namers, (
        "the stale marker file is still named by executed code at %s - it is not "
        "inert, and a box upgrading from an older build still carries it"
        % ", ".join(namers)
    )
