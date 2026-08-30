"""The Kodi-version restore gate (versiongate.py + wiz.py wiring).

THE DEFECT THIS PINS: a full backup zips special://home/addons wholesale and,
before this gate, restore() extracted it wholesale - so restoring a Kodi 21
(Omega) archive onto a rebuilt Kodi 22 (Piers) box overwrote every
Piers-native add-on and skin with Omega-era ones, silently. The manifest
recorded created/source_os/entries/failed and no Kodi version at all, and
wiz.backup() computed KODIV without ever reading it.

THE CONTRACT (owner-approved 2026-08-30):
  1. backup() stamps the Kodi MAJOR into the manifest (kodi_version).
  2. A same-major restore behaves exactly as before: addons/ extracts.
  3. A cross-major restore withholds addons/ members, restores userdata in
     full, and says why in ONE plain dialog - never reported as a failure.
  4. An UNSTAMPED archive (every backup in existence before this shipped) is
     treated as cross-major: the owner's real archives are all Omega and the
     boxes are moving to Piers. No heuristics from source_os or content.
  5. The explanation dialog fires ONCE per restore, even when the attempt
     loop runs a second pass.
  6. A userdata-anchored archive (no addons/ members) is never gated and
     never nagged.
  7. versiongate stays importable with NO Kodi modules present (owner
     directive: EZM++ features stay separable pieces).

The fixture stubs the Kodi runtime the same way
tests/test_archive_anchor_edge_layouts.py does (fake xbmc* modules, the real
wiz.py imported underneath, special:// mapped to tmp_path), and builds real
zips with zipfile.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import zipfile as _zip
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
ADDON_ROOT = REPO_ROOT / "script.ezmaintenanceplusplus"

# What a real box reports: xbmc.getInfoLabel("System.BuildVersion") on Piers.
# wiz.get_Kodi_Version() takes [:4] and floats it -> 22.0 -> major 22.
PIERS_BUILDVERSION = "22.0 (22.0.0) Git:20260830-abcdef1234"

GATE_MARKER = "will NOT be restored"  # the one phrase every gate dialog carries


# --------------------------------------------------------------------------- #
# Harness: fake Kodi modules, real wiz.py (same pattern as
# test_archive_anchor_edge_layouts.py's `wiz` fixture)
# --------------------------------------------------------------------------- #
@pytest.fixture
def wiz(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if name == "resources" or name.startswith("resources."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    xbmc = types.ModuleType("xbmc")
    xbmc.translatePath = lambda p: p.replace("special://", str(tmp_path) + "/")
    xbmc.getLocalizedString = lambda i: str(i)
    xbmc.getInfoLabel = lambda s: ""
    xbmc.getCondVisibility = lambda s: False
    xbmc.getSkinDir = lambda: "skin.estuary"
    xbmc.log = lambda *a, **k: None
    xbmc.executebuiltin = lambda *a, **k: None
    xbmc.executeJSONRPC = lambda cmd: "{}"
    xbmc.LOGERROR = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGINFO = 3
    xbmc.LOGDEBUG = 4
    xbmc.LOGFATAL = 0
    xbmc.LOGNONE = 5
    xbmc.LOGNOTICE = 3
    xbmc.PLAYLIST_VIDEO = 1
    xbmc.sleep = lambda ms: None
    xbmc.Keyboard = lambda *a, **k: types.SimpleNamespace(
        doModal=lambda: None, isConfirmed=lambda: False, getText=lambda: ""
    )
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
            return -1

    xbmcgui.DialogProgress = _FakeDialogProgress
    xbmcgui.DialogProgressBG = _FakeDialogProgress
    xbmcgui.Dialog = _FakeDialog
    xbmcgui.ListItem = lambda *a, **k: types.SimpleNamespace(
        setArt=lambda *a, **k: None
    )
    xbmcgui.ControlButton = lambda *a, **k: None
    xbmcgui.ControlImage = lambda *a, **k: None

    class _FakeWindow:
        def __init__(self, *a, **k):
            pass

        def getProperty(self, k):
            return ""

        def setProperty(self, k, v):
            pass

        def clearProperty(self, k):
            pass

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

    return importlib.import_module("resources.lib.modules.wiz")


def _prep_restore(wiz, monkeypatch, tmp_path):
    """control.HOME + control.USERDATA as real tmp dirs; record every dialog."""
    home = tmp_path / "home"
    (home / "userdata").mkdir(parents=True)
    monkeypatch.setattr(wiz.control, "HOME", str(home))
    monkeypatch.setattr(wiz.control, "USERDATA", str(home / "userdata"))

    rep = types.SimpleNamespace(
        restart_statuses=[], ok_calls=[], notifications=[], result=None
    )

    def _ask_restart(status="", heading=None, **k):
        rep.restart_statuses.append(str(status))

    monkeypatch.setattr(wiz.ui, "ask_restart", _ask_restart)

    class _RecordingDialog:
        def ok(self, *a, **k):
            rep.ok_calls.append(a)
            return True

        def yesno(self, *a, **k):
            return True

        def notification(self, *a, **k):
            rep.notifications.append(a)

        def select(self, *a, **k):
            return -1

        def textviewer(self, *a, **k):
            rep.ok_calls.append(a)

    monkeypatch.setattr(wiz, "dialog", _RecordingDialog())
    return home, rep


def _make_zip(path, files, manifest=None):
    """A real backup-shaped zip; manifest=dict embeds backup_manifest.json."""
    with _zip.ZipFile(path, "w") as z:
        for name, body in files:
            z.writestr(name, body)
        if manifest is not None:
            z.writestr("backup_manifest.json", json.dumps(manifest))
    return path


def _gate_dialogs(rep):
    """Every recorded dialog that is the gate's explanation, and only those."""
    return [c for c in rep.ok_calls if any(GATE_MARKER in str(a) for a in c)]


def _report_text(rep):
    parts = list(rep.restart_statuses)
    for call in rep.ok_calls + rep.notifications:
        parts.extend(str(a) for a in call)
    return " | ".join(parts)


HOME_MEMBERS = [
    ("userdata/guisettings.xml", "<settings/>"),
    ("userdata/sources.xml", "<sources/>"),
    ("addons/plugin.video.old/addon.xml", "<addon id='plugin.video.old'/>"),
    ("addons/skin.oldskin/addon.xml", "<addon id='skin.oldskin'/>"),
]


def _manifest(kodi_version=None, entries=len(HOME_MEMBERS)):
    m = {"created": "2026-08-30T00:00:00", "source_os": "android",
         "entries": entries, "failed": []}
    if kodi_version is not None:
        m["kodi_version"] = kodi_version
    return m


# --------------------------------------------------------------------------- #
# 1. backup() stamps the Kodi major into the manifest
# --------------------------------------------------------------------------- #
def test_backup_manifest_stamps_kodi_major(wiz, monkeypatch, tmp_path):
    """CreateZip's manifest carries kodi_version, parsed from the REAL
    System.BuildVersion shape end to end (no monkeypatch of get_Kodi_Version:
    this is the exact path a box takes)."""
    import contextlib
    import unittest.mock as mock

    monkeypatch.setattr(
        sys.modules["xbmc"],
        "getInfoLabel",
        lambda s: PIERS_BUILDVERSION if s == "System.BuildVersion" else "",
    )
    home = tmp_path / "srchome"
    (home / "userdata").mkdir(parents=True)
    (home / "userdata" / "guisettings.xml").write_text("<settings/>")
    out = tmp_path / "out.zip"

    class _NoopProgress:
        def items(self, *a, **k):
            pass

        def update(self, *a, **k):
            pass

        def cancelled(self):
            return False

    @contextlib.contextmanager
    def _prog(*a, **k):
        yield _NoopProgress()

    with mock.patch.object(wiz.ui, "Progress", _prog):
        wiz.CreateZip(str(home), str(out), "h", "m", ["temp"], [".log"])

    with _zip.ZipFile(out) as z:
        manifest = json.loads(z.read(wiz.MANIFEST_NAME).decode("utf-8"))
    assert manifest["kodi_version"] == 22, (
        "the manifest must record the Kodi MAJOR the backup was made on; "
        "got %r" % manifest.get("kodi_version")
    )


def test_backup_manifest_stamps_zero_when_version_unreadable(wiz, tmp_path):
    """An unreadable BuildVersion stamps 0 (honest unknown), never raises and
    never invents a number. The default fixture getInfoLabel returns ''."""
    import contextlib
    import unittest.mock as mock

    home = tmp_path / "srchome"
    (home / "userdata").mkdir(parents=True)
    (home / "userdata" / "a.xml").write_text("<a/>")
    out = tmp_path / "out.zip"

    @contextlib.contextmanager
    def _prog(*a, **k):
        yield types.SimpleNamespace(
            items=lambda *a, **k: None,
            update=lambda *a, **k: None,
            cancelled=lambda: False,
        )

    with mock.patch.object(wiz.ui, "Progress", _prog):
        wiz.CreateZip(str(home), str(out), "h", "m", ["temp"], [".log"])

    with _zip.ZipFile(out) as z:
        manifest = json.loads(z.read(wiz.MANIFEST_NAME).decode("utf-8"))
    assert manifest["kodi_version"] == 0


# --------------------------------------------------------------------------- #
# 2. Same major: addons/ extracts exactly as it always has, no gate dialog
# --------------------------------------------------------------------------- #
def test_same_major_restore_extracts_addons(wiz, monkeypatch, tmp_path):
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)
    src = _make_zip(
        tmp_path / "kodi_backup_202608300000.zip",
        HOME_MEMBERS,
        manifest=_manifest(kodi_version=22),
    )

    rep.result = wiz.restore(str(src), confirm=False)

    assert (home / "addons/plugin.video.old/addon.xml").is_file(), (
        "same-major restore must extract addons/ exactly as before the gate"
    )
    assert (home / "userdata/guisettings.xml").is_file()
    assert _gate_dialogs(rep) == [], (
        "no gate dialog on a same-major restore; dialogs were: %r" % rep.ok_calls
    )


# --------------------------------------------------------------------------- #
# 3. Cross major: addons/ withheld, userdata restored, one plain dialog,
#    and NEVER reported as a failed/partial restore
# --------------------------------------------------------------------------- #
def test_cross_major_restore_withholds_addons_restores_userdata(
    wiz, monkeypatch, tmp_path
):
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)
    src = _make_zip(
        tmp_path / "kodi_backup_202608300000.zip",
        HOME_MEMBERS,
        manifest=_manifest(kodi_version=21),
    )

    rep.result = wiz.restore(str(src), confirm=False)

    assert not (home / "addons").exists() or not any(
        (home / "addons").rglob("*")
    ), "cross-major restore must not lay ANY addons/ member on the box"
    assert (home / "userdata/guisettings.xml").is_file(), (
        "userdata must still restore in full on a cross-major restore"
    )
    assert (home / "userdata/sources.xml").is_file()

    gate = _gate_dialogs(rep)
    assert len(gate) == 1, (
        "exactly one explanation dialog; got %d in %r" % (len(gate), rep.ok_calls)
    )
    text = " ".join(str(a) for a in gate[0])
    assert "Kodi 21" in text and "Kodi 22" in text, (
        "the dialog must name both versions plainly; said: %r" % text
    )
    assert "reinstall" in text.lower(), (
        "the dialog must tell the user what to do next; said: %r" % text
    )

    # The withheld members are POLICY, not loss: the restore must not claim
    # members failed or went unmapped, and the structured result must agree.
    assert "did NOT restore" not in _report_text(rep)
    assert isinstance(rep.result, dict)
    assert rep.result.get("failed") == 0
    assert rep.result.get("unmapped") == 0, (
        "gated addons/ members must not be counted as unmapped hard failures"
    )


# --------------------------------------------------------------------------- #
# 4. Stampless archives: every backup in existence today. Treated cross-major.
# --------------------------------------------------------------------------- #
def test_stampless_manifest_treated_as_cross_major(wiz, monkeypatch, tmp_path):
    """A manifest WITHOUT kodi_version (every 2026.07/08 backup) gates."""
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)
    src = _make_zip(
        tmp_path / "kodi_backup_202607190000.zip",
        HOME_MEMBERS,
        manifest=_manifest(kodi_version=None),
    )

    rep.result = wiz.restore(str(src), confirm=False)

    assert not (home / "addons/plugin.video.old/addon.xml").exists()
    assert (home / "userdata/guisettings.xml").is_file()
    gate = _gate_dialogs(rep)
    assert len(gate) == 1
    text = " ".join(str(a) for a in gate[0])
    assert "does not say which version" in text, (
        "an unstamped archive gets the 'version not recorded' wording, "
        "never an invented number; said: %r" % text
    )


def test_manifestless_archive_treated_as_cross_major(wiz, monkeypatch, tmp_path):
    """No manifest at all (pre-2026-07-16 backups): same gate, same words."""
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)
    src = _make_zip(tmp_path / "kodi_backup_202606010000.zip", HOME_MEMBERS)

    rep.result = wiz.restore(str(src), confirm=False)

    assert not (home / "addons/plugin.video.old/addon.xml").exists()
    assert (home / "userdata/guisettings.xml").is_file()
    assert len(_gate_dialogs(rep)) == 1


# --------------------------------------------------------------------------- #
# 5. The dialog fires ONCE even when the attempt loop runs a second pass
# --------------------------------------------------------------------------- #
def test_gate_dialog_fires_once_across_retry_passes(wiz, monkeypatch, tmp_path):
    """Force the attention auto-retry (apply_guisettings raising) and prove
    the gate spoke once: the decision lives OUTSIDE _restore_pass."""
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)

    ks = importlib.import_module("resources.lib.modules._kodisettings")
    calls = {"n": 0}

    def _boom(path):
        calls["n"] += 1
        raise RuntimeError("forced for the retry test")

    monkeypatch.setattr(ks, "apply_guisettings", _boom)

    src = _make_zip(
        tmp_path / "kodi_backup_202608300000.zip",
        HOME_MEMBERS,
        manifest=_manifest(kodi_version=21),
    )
    rep.result = wiz.restore(str(src), confirm=False)

    assert calls["n"] >= 2, (
        "the forced attention finding must actually drive a second pass "
        "(otherwise this test proves nothing); passes seen: %d" % calls["n"]
    )
    assert len(_gate_dialogs(rep)) == 1, (
        "the gate explanation must fire once per restore, not once per pass"
    )
    assert not (home / "addons/plugin.video.old/addon.xml").exists(), (
        "the retry pass must stay gated too"
    )


# --------------------------------------------------------------------------- #
# 6. Userdata-anchored archives are never gated and never nagged
# --------------------------------------------------------------------------- #
def test_userdata_backup_never_gated(wiz, monkeypatch, tmp_path):
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz, "get_Kodi_Version", lambda: 22.0)
    members = [
        ("guisettings.xml", "<settings/>"),
        ("addon_data/pvr.iptvsimple/instance-settings-1.xml", "<i/>"),
    ]
    src = _make_zip(tmp_path / "kodi_settings_202607190000.zip", members)

    rep.result = wiz.restore(str(src), confirm=False)

    for name, _ in members:
        assert (home / "userdata" / name).is_file()
    assert _gate_dialogs(rep) == [], (
        "a settings backup has no addons/ tree: nothing to gate, nothing to say"
    )


# --------------------------------------------------------------------------- #
# 7. The documented harness disarm: running major unknown -> gate stands down
# --------------------------------------------------------------------------- #
def test_unknown_running_version_disarms_the_gate(wiz, monkeypatch, tmp_path):
    """get_Kodi_Version() == 0 never happens on a real box (BuildVersion is a
    compile-time constant); when it does (harness, diagnostic import), the
    gate must stand down rather than block restores the contract protects.
    This is the behavior the whole pre-existing suite relies on."""
    home, rep = _prep_restore(wiz, monkeypatch, tmp_path)
    assert wiz.get_Kodi_Version() == 0  # the fixture's '' BuildVersion
    src = _make_zip(tmp_path / "kodi_backup_202608300000.zip", HOME_MEMBERS)

    rep.result = wiz.restore(str(src), confirm=False)

    assert (home / "addons/plugin.video.old/addon.xml").is_file()
    assert _gate_dialogs(rep) == []


# --------------------------------------------------------------------------- #
# 8. versiongate itself: pure, separable, honest about unknowns
# --------------------------------------------------------------------------- #
def test_versiongate_imports_with_no_kodi_at_all(monkeypatch):
    """Owner directive: EZM++ features stay separable pieces. The gate must
    import and work with no xbmc* module anywhere in sys.modules."""
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if (
            name == "resources"
            or name.startswith("resources.")
            or name.startswith("xbmc")
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    vg = importlib.import_module("resources.lib.modules.versiongate")
    assert not any(n.startswith("xbmc") for n in sys.modules), (
        "importing versiongate must not drag any Kodi module in"
    )
    d = vg.evaluate({"kodi_version": 21}, 22.0, ["addons/x/addon.xml"])
    assert d.blocked and d.archive_major == 21 and d.running_major == 22


def test_versiongate_major_parsing_is_tolerant():
    sys.path.insert(0, str(ADDON_ROOT))
    try:
        vg = importlib.import_module("resources.lib.modules.versiongate")
    finally:
        sys.path.remove(str(ADDON_ROOT))
    assert vg.major(21.9) == 21  # get_Kodi_Version()'s float shape
    assert vg.major("22.0") == 22  # a string that made it into a manifest
    assert vg.major(22) == 22
    assert vg.major(0) == 0
    assert vg.major(-1) == 0  # never a valid major
    assert vg.major(None) == 0
    assert vg.major("") == 0
    assert vg.major("garbage") == 0
    assert vg.archive_major(None) == 0
    assert vg.archive_major({}) == 0
    assert vg.archive_major({"kodi_version": "21.5"}) == 21
    assert vg.archive_major([1, 2]) == 0  # foreign-shaped manifest


def test_versiongate_decision_matrix():
    sys.path.insert(0, str(ADDON_ROOT))
    try:
        vg = importlib.import_module("resources.lib.modules.versiongate")
    finally:
        sys.path.remove(str(ADDON_ROOT))
    addons = ["addons/x/addon.xml", "userdata/guisettings.xml"]
    userdata_only = ["guisettings.xml", "addon_data/x/settings.xml"]

    # same major -> open
    assert not vg.evaluate({"kodi_version": 22}, 22.0, addons).blocked
    # cross major -> blocked, both majors named in the message
    d = vg.evaluate({"kodi_version": 21}, 22.0, addons)
    assert d.blocked and "Kodi 21" in d.message and "Kodi 22" in d.message
    # unstamped -> blocked with the honest wording, no invented number
    d = vg.evaluate({"created": "x"}, 22.0, addons)
    assert d.blocked and "does not say which version" in d.message
    # no manifest at all -> blocked the same way
    assert vg.evaluate(None, 22.0, addons).blocked
    # nothing under addons/ -> nothing to gate, whatever the versions say
    assert not vg.evaluate({"kodi_version": 19}, 22.0, userdata_only).blocked
    assert not vg.evaluate(None, 22.0, []).blocked
    # running unknown -> stands down (harness disarm, documented)
    assert not vg.evaluate(None, 0, addons).blocked
    # NEWER archive on an older box is still cross-major, still gated
    assert vg.evaluate({"kodi_version": 23}, 22.0, addons).blocked


def test_versiongate_wrap_skip_composes_not_replaces():
    sys.path.insert(0, str(ADDON_ROOT))
    try:
        vg = importlib.import_module("resources.lib.modules.versiongate")
    finally:
        sys.path.remove(str(ADDON_ROOT))
    base_hits = []

    def base(name):
        base_hits.append(name)
        return name == "temp/x"

    gated = vg.wrap_skip(base)
    assert gated("addons/plugin.x/addon.xml") is True  # gate skip
    assert gated("/addons/plugin.x/addon.xml") is True  # leading slash
    assert gated("addons\\plugin.x\\addon.xml") is True  # backslash zip
    assert gated("temp/x") is True  # base skip still fires
    assert gated("userdata/guisettings.xml") is False  # everything else lands
    assert "userdata/guisettings.xml" in base_hits  # base was consulted
    # 'addonsfoo/...' is NOT the addons tree - no prefix false-positives
    assert gated("addonsfoo/file") is False
