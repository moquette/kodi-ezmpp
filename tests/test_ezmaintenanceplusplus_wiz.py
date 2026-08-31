"""Coverage for script.ezmaintenanceplusplus's wiz.py port-stripping fix.

wiz.py's backup()/restoreFolder() read download.path/restore.path - both
Kodi "type=folder" settings, browse-only, with no manual text entry at all.
Kodi's own network-browse dialog bakes an explicit port into the nfs:// URL
it hands back (e.g. nfs://host:2049/export/path), and that explicit-port
form breaks Kodi's own NFS client write path - live-proven, independently,
on two different boxes (a VfsCopyError / 0-byte copy every time). Since the
setting can only ever be set via that same dialog, this can recur on any
future box; _strip_nfs_port() defangs it at the two read sites.

This is a large, pre-existing third-party add-on this repo forks/patches
(CLAUDE.md: "standardize on the repo's ++ fork"), with no existing test
harness of its own. The fixture below fakes just enough of xbmc*/xbmcaddon/
xbmcgui/xbmcvfs/xbmcplugin for wiz.py's own import chain (control.py,
maintenance.py, tools.py, ui.py) to succeed, so _strip_nfs_port can be
exercised as the real function inside the real module, not a copy-pasted
reimplementation of its regex.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
ADDON_ROOT = REPO_ROOT / "script.ezmaintenanceplusplus"


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


def test_strip_nfs_port_removes_explicit_port(wiz):
    assert (
        wiz._strip_nfs_port("nfs://192.168.7.2:2049/Users/moquette/Kodi/Backup/atv-2/")
        == "nfs://192.168.7.2/Users/moquette/Kodi/Backup/atv-2/"
    )


def test_strip_nfs_port_no_port_unchanged(wiz):
    path = "nfs://192.168.7.2/Users/moquette/Kodi/Backup/atv-2/"
    assert wiz._strip_nfs_port(path) == path


def test_strip_nfs_port_leaves_non_nfs_paths_alone(wiz):
    assert (
        wiz._strip_nfs_port("smb://192.168.7.2/KodiBackup/atv-2/")
        == "smb://192.168.7.2/KodiBackup/atv-2/"
    )
    assert wiz._strip_nfs_port("/local/path") == "/local/path"


def test_strip_nfs_port_handles_empty_and_none(wiz):
    assert wiz._strip_nfs_port("") == ""
    assert wiz._strip_nfs_port(None) is None


def test_strip_nfs_port_bare_host_no_trailing_slash(wiz):
    # A port on a bare host with no path at all must still be stripped.
    assert wiz._strip_nfs_port("nfs://192.168.7.2:2049") == "nfs://192.168.7.2"


def test_backup_uses_stripped_port_path(wiz, monkeypatch, tmp_path):
    """End-to-end: backup() must pass the STRIPPED path to CreateZip, not the
    raw (possibly port-carrying) download.path setting value."""
    backupdata = tmp_path / "home"
    backupdata.mkdir()
    monkeypatch.setattr(wiz.control, "HOME", str(backupdata))
    monkeypatch.setattr(
        wiz.control,
        "setting",
        lambda key: (
            "nfs://192.168.7.2:2049/Users/moquette/Kodi/Backup/atv-2/"
            if key == "download.path"
            else ""
        ),
    )
    monkeypatch.setattr(wiz.tools, "_get_keyboard", lambda **k: "mybackup")
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True)  # accept the name

    captured = {}

    def _fake_create_zip(src, dst, *a, **k):
        captured["dst"] = dst
        return False

    monkeypatch.setattr(wiz, "CreateZip", _fake_create_zip)
    monkeypatch.setattr(wiz, "_rotate_vfs", lambda *a, **k: None)
    monkeypatch.setattr(
        wiz,
        "xbmcaddon",
        types.SimpleNamespace(
            Addon=lambda: types.SimpleNamespace(getSetting=lambda k: "false")
        ),
    )

    wiz.backup(mode="full")
    assert "dst" in captured, "CreateZip must have been called"
    assert ":2049" not in captured["dst"]
    assert captured["dst"].startswith(
        "nfs://192.168.7.2/Users/moquette/Kodi/Backup/atv-2/"
    )


def _stub_backup_env(wiz, monkeypatch, tmp_path, keyboard_name="mybackup"):
    """Common backup() stubs: a HOME to zip, an nfs download.path, a keyboard
    name, and no-op rotation + addon settings. Returns nothing; callers add the
    ui.confirm / CreateZip stubs they care about."""
    backupdata = tmp_path / "home"
    backupdata.mkdir()
    monkeypatch.setattr(wiz.control, "HOME", str(backupdata))
    monkeypatch.setattr(
        wiz.control,
        "setting",
        lambda key: (
            "nfs://192.168.7.2/Users/moquette/Kodi/Backup/atv-2/"
            if key == "download.path"
            else ""
        ),
    )
    monkeypatch.setattr(wiz.tools, "_get_keyboard", lambda **k: keyboard_name)
    monkeypatch.setattr(wiz, "_rotate_vfs", lambda *a, **k: None)
    monkeypatch.setattr(
        wiz,
        "xbmcaddon",
        types.SimpleNamespace(
            Addon=lambda: types.SimpleNamespace(getSetting=lambda k: "false")
        ),
    )


def test_backup_aborts_when_name_confirm_declined(wiz, monkeypatch, tmp_path):
    """Declining the new name-confirm prompt must abort BEFORE any zip is built -
    parity with restore's confirm, and no partial work on a cancel."""
    _stub_backup_env(wiz, monkeypatch, tmp_path)
    called = {"zip": False}

    def _no_zip(*a, **k):
        called["zip"] = True
        return False

    monkeypatch.setattr(wiz, "CreateZip", _no_zip)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: False)  # user cancels
    wiz.backup(mode="full")
    assert called["zip"] is False, "declining the confirm must abort before CreateZip"


def test_backup_confirm_shows_final_filename_then_proceeds(wiz, monkeypatch, tmp_path):
    """Confirming proceeds to the zip build, and the confirm message shows the
    FINAL filename (spaces->_, auto timestamp, .zip) so the user reviews it."""
    _stub_backup_env(wiz, monkeypatch, tmp_path, keyboard_name="Living Room")
    seen = {}

    def _capture_confirm(message, **k):
        seen["msg"] = message
        return True

    monkeypatch.setattr(wiz.ui, "confirm", _capture_confirm)
    captured = {}
    monkeypatch.setattr(
        wiz, "CreateZip", lambda src, dst, *a, **k: captured.__setitem__("dst", dst)
    )
    wiz.backup(mode="full")
    assert "Living_Room" in seen.get("msg", ""), "confirm must show the final name"
    assert seen["msg"].rstrip().endswith(".zip"), "confirm must show the .zip filename"
    assert "dst" in captured, "confirming must proceed to CreateZip"


# NOTE: the Dropbox name-confirm gate is now asserted BEHAVIORALLY (declining the
# confirm must reach neither CreateZip nor upload) in
# test_backup_dropbox_confirm_declined_builds_nothing further down, together with
# the rest of the wiz-side Dropbox flow. The old source-string assertion that lived
# here could not tell a live call from a commented-out one.


def _record_settings_jumps(wiz, monkeypatch):
    """Record both settings entry points separately.

    A bail that opens the settings window on its FIRST category (Maintenance) is the
    defect: she is told to set a path and then dropped on the wrong tab. Recording the
    plain openSettings AND the tab-aware openSettingsTab is what lets a test say which
    one was used, rather than "settings opened somehow"."""
    plain, tabs = [], []
    monkeypatch.setattr(wiz.control, "openSettings", lambda *a, **k: plain.append(True))
    monkeypatch.setattr(
        wiz.control, "openSettingsTab", lambda index, **k: tabs.append(index)
    )
    return plain, tabs


def _backup_env_with_setting(wiz, monkeypatch, tmp_path, value):
    backupdata = tmp_path / "home"
    backupdata.mkdir(exist_ok=True)
    monkeypatch.setattr(wiz.control, "HOME", str(backupdata))
    monkeypatch.setattr(wiz.control, "setting", lambda key: value)


def test_backup_jumps_to_the_backup_restore_tab_when_path_unset(
    wiz, monkeypatch, tmp_path
):
    """backup() with an empty download.path must open the settings window ON the
    Backup/Restore tab, where download.path actually lives.

    A plain openSettings() lands on Maintenance, the first category: the message says
    "Please Setup a Path for Downloads first" and then nothing on screen says where."""
    _backup_env_with_setting(wiz, monkeypatch, tmp_path, "")
    plain, tabs = _record_settings_jumps(wiz, monkeypatch)

    wiz.backup(mode="full")

    assert tabs == [wiz.control.SETTINGS_TAB_BACKUP_RESTORE], (
        "the bail must target the Backup/Restore tab, not just open settings: "
        "tab jumps %r, plain opens %r" % (tabs, plain)
    )
    assert plain == [], "the settings window must not also be opened untargeted"


def test_restore_jumps_to_the_backup_restore_tab_when_path_unset(wiz, monkeypatch):
    """Same rule for restoreFolder() and restore.path."""
    monkeypatch.setattr(wiz.control, "setting", lambda key: "")
    plain, tabs = _record_settings_jumps(wiz, monkeypatch)

    wiz.restoreFolder()

    assert tabs == [wiz.control.SETTINGS_TAB_BACKUP_RESTORE], (
        "the bail must target the Backup/Restore tab: tab jumps %r, plain opens %r"
        % (tabs, plain)
    )
    assert plain == []


def test_backup_treats_a_whitespace_only_path_as_unset(wiz, monkeypatch, tmp_path):
    """ "   " is not a folder, and the menu row already says so.

    backup() used to compare the RAW setting to "", so a whitespace-only download.path
    walked past this guard and the backup ran on toward a path that cannot exist -
    while the Backup row on the menu right before it read "Not set". The row told the
    truth and the action did not."""
    _backup_env_with_setting(wiz, monkeypatch, tmp_path, "   ")
    plain, tabs = _record_settings_jumps(wiz, monkeypatch)
    built = []
    monkeypatch.setattr(wiz, "CreateZip", lambda *a, **k: built.append(a))

    wiz.backup(mode="full")

    assert tabs == [wiz.control.SETTINGS_TAB_BACKUP_RESTORE], (
        "a whitespace-only download.path must bail to the settings tab like any "
        "other unset path: tab jumps %r, plain opens %r" % (tabs, plain)
    )
    assert built == [], "nothing may be zipped against a whitespace-only path"


def test_restore_treats_a_whitespace_only_path_as_unset(wiz, monkeypatch):
    """The mirror for restore.path: listing "   " lists nothing and reports an empty
    folder, which reads as "your backups are gone" rather than "you have not set a
    folder yet"."""
    monkeypatch.setattr(wiz.control, "setting", lambda key: " \t ")
    plain, tabs = _record_settings_jumps(wiz, monkeypatch)
    listed = []

    def _listdir(path):
        listed.append(path)
        return ([], [])

    monkeypatch.setattr(wiz.xbmcvfs, "listdir", _listdir)

    wiz.restoreFolder()

    assert tabs == [wiz.control.SETTINGS_TAB_BACKUP_RESTORE], (
        "a whitespace-only restore.path must bail to the settings tab: tab jumps %r, "
        "plain opens %r" % (tabs, plain)
    )
    assert listed == [], "nothing may be listed against a whitespace-only path: %r" % (
        listed,
    )


def test_configured_path_strips_the_nfs_port_the_actions_discard(wiz, monkeypatch):
    """One definition of "configured", shared by the actions and the menu row.

    Kodi's browse dialog bakes :2049 into nfs:// paths and that form breaks Kodi's own
    NFS client, so every reader strips it. The row used to print the raw setting and
    therefore named a folder with a port the add-on discards - visible on the live
    boxes, whose setting really does carry :2049."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "nfs://192.168.7.2:2049/volume1/Kodi"
    )
    assert wiz.configured_path("download.path") == "nfs://192.168.7.2/volume1/Kodi"

    def _boom(key):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(wiz.control, "setting", _boom)
    assert wiz.configured_path("download.path") == "", (
        "an unreadable setting is 'not configured', never a crash: the callers are a "
        "menu row and a bail guard"
    )


def test_restore_does_not_rewrite_settings_verbatim_restore(wiz, monkeypatch, tmp_path):
    """A restore now restores the backup EXACTLY as taken - it does NOT re-stamp
    download.path/restore.path/destination afterward. The user sets the backup path
    themselves (the native settings dialog works), so restore stays a plain, predictable
    extract with no magic touching the restored settings."""
    import zipfile as _zip

    writes = []
    monkeypatch.setattr(wiz.control, "setting", lambda key: "")
    monkeypatch.setattr(wiz.control, "setSetting", lambda k, v: writes.append((k, v)))
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: None)

    src = tmp_path / "some_backup.zip"
    with _zip.ZipFile(src, "w") as z:
        z.writestr(
            "userdata/addon_data/script.ezmaintenanceplusplus/settings.xml",
            "<settings><setting id='download.path'>"
            "nfs://192.168.7.2/Kodi/Backup/office/</setting></settings>",
        )
        z.writestr("userdata/guisettings.xml", "<settings />")

    wiz.restore(str(src), confirm=False)

    # restore() must NOT setSetting any box-local key - the extracted settings.xml stands.
    assert not any(
        k in ("download.path", "restore.path", "destination") for k, _ in writes
    ), f"restore should not re-stamp box-local settings, but wrote: {writes}"


# --------------------------------------------------------------------------- #
# "Wipe clean before restore" (clean-clone) path + the extract crash fix.
# --------------------------------------------------------------------------- #
class _RecordingProgress:
    """A fake ui.Progress that records every items() note and never cancels, so the
    extract's dialog-update throttle can be asserted off-device."""

    def __init__(self):
        self.notes = []

    def cancelled(self):
        return False

    def items(self, done, total, note=""):
        self.notes.append(note)


def _make_valid_zip(path, files):
    import zipfile as _zip

    with _zip.ZipFile(path, "w") as z:
        for name, body in files:
            z.writestr(name, body)
    return path


def _load_onetap():
    return importlib.import_module("resources.lib.modules.onetap")


def test_restore_wipe_does_not_wipe_on_bad_zip(wiz, monkeypatch, tmp_path):
    """(a) restore(wipe=True) with a corrupt/short zip must ABORT with the box UNTOUCHED
    - validation fails, so the wipe is never reached."""
    onetap = _load_onetap()

    wiped = []
    restarted = []
    monkeypatch.setattr(onetap, "_wipe", lambda *a, **k: wiped.append(a))
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: restarted.append(True))
    monkeypatch.setattr(wiz.control, "HOME", str(tmp_path / "home"))

    bad = tmp_path / "corrupt.zip"
    bad.write_bytes(b"this is not a zip file at all")  # size > 0 but not a real zip

    wiz.restore(str(bad), confirm=False, wipe=True)

    assert wiped == [], "the box must NOT be wiped when the zip is invalid"
    assert restarted == [], "a bad zip must not reach the restart prompt"


def test_restore_wipe_validates_then_wipes_then_extracts(wiz, monkeypatch, tmp_path):
    """(b) restore(wipe=True) with a valid zip must wipe ONLY after validation, then run
    the (uninterruptible) extract, then reach the restart prompt - in that order."""
    onetap = _load_onetap()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(wiz.control, "HOME", str(home))

    events = []
    monkeypatch.setattr(onetap, "_wipe", lambda *a, **k: events.append("wipe"))

    captured = {}

    def _fake_extract(_in, _out, progress, **kw):
        events.append("extract")
        captured["cancelable"] = kw.get("cancelable", True)
        return False

    monkeypatch.setattr(wiz, "ExtractWithProgress", _fake_extract)
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: events.append("restart"))

    src = tmp_path / "backup.zip"
    _make_valid_zip(
        src,
        [
            ("userdata/guisettings.xml", "<settings />"),
            ("addons/foo/addon.xml", "<a/>"),
        ],
    )

    wiz.restore(str(src), confirm=False, wipe=True)

    assert events == ["wipe", "extract", "restart"], events
    # A wiped box must be driven by an UNINTERRUPTIBLE extract (post_wipe semantics).
    assert captured["cancelable"] is False


def test_wipe_excludes_preserves_addon_deps_and_temp(wiz):
    """(c) the reused One-Tap wipe excludes must preserve this add-on, its runtime deps,
    and special://temp (where the validated zip is staged)."""
    onetap = _load_onetap()
    ex = onetap._wipe_excludes()
    assert "temp" in ex
    assert "script.module.requests" in ex
    assert "script.ezmaintenanceplusplus" in ex


def test_extract_progress_note_is_throttled(wiz, tmp_path):
    """(d) the extract must NOT redraw the progress dialog with a new filename every file
    (the Fire OS 8 SIGSEGV). The note is refreshed at most every N files and never carries
    a per-file basename."""
    src = tmp_path / "many.zip"
    files = [("data/file%03d.txt" % i, "x") for i in range(200)]
    _make_valid_zip(src, files)

    out = tmp_path / "out"
    out.mkdir()
    p = _RecordingProgress()
    wiz.ExtractWithProgress(str(src), str(out), p)

    # Far fewer dialog updates than files (throttled), but still moving.
    assert 0 < len(p.notes) <= 200 // 10
    # No note carries a source basename - only the short static "Extracting file X of Y".
    assert all(n.startswith("Extracting file ") for n in p.notes)
    assert not any(".txt" in n for n in p.notes)
    # Every file was still actually extracted.
    assert len(list(out.rglob("*.txt"))) == 200


def test_order_userdata_first_puts_settings_before_addons(wiz):
    """(e) userdata/ entries must be ordered before addons/ so an interrupted extract
    keeps the irreplaceable settings."""
    infos = [
        types.SimpleNamespace(filename="addons/a/x.py"),
        types.SimpleNamespace(filename="userdata/guisettings.xml"),
        types.SimpleNamespace(filename="media/logo.png"),
        types.SimpleNamespace(filename="addons/b/y.py"),
        types.SimpleNamespace(filename="userdata/sources.xml"),
    ]
    names = [i.filename for i in wiz._order_userdata_first(infos)]
    last_userdata = max(i for i, n in enumerate(names) if n.startswith("userdata/"))
    first_addon = min(i for i, n in enumerate(names) if n.startswith("addons/"))
    assert last_userdata < first_addon, names


# --------------------------------------------------------------------------- #
# Post-restore, per-device video-cache-buffer retune.
# --------------------------------------------------------------------------- #
def test_restore_no_wipe_still_overlays(wiz, monkeypatch, tmp_path):
    """(f) the normal (wipe=False) path is unchanged: it never wipes, it extracts, and it
    reaches the restart prompt."""
    onetap = _load_onetap()

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(wiz.control, "HOME", str(home))

    wiped = []
    monkeypatch.setattr(onetap, "_wipe", lambda *a, **k: wiped.append(a))

    extracted = []
    monkeypatch.setattr(
        wiz, "ExtractWithProgress", lambda *a, **k: extracted.append(True) or False
    )
    restarted = []
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: restarted.append(True))

    src = tmp_path / "backup.zip"
    _make_valid_zip(src, [("userdata/guisettings.xml", "<settings />")])

    wiz.restore(str(src), confirm=False, wipe=False)

    assert wiped == [], "the no-wipe path must never wipe"
    assert extracted == [True], "the no-wipe path must still extract"
    assert restarted == [True], "the no-wipe path must still offer a restart"


# --------------------------------------------------------------------------- #
# Post-restore, per-device DEVICE-NAME prompt (runs before the buffer prompt in
# the combined post-restore tune-up, gated by the SAME marker).
# --------------------------------------------------------------------------- #
def test_get_devicename_reads_value(wiz, monkeypatch):
    """_get_devicename returns the live core-setting value."""
    tools = wiz.tools
    monkeypatch.setattr(tools, "_jsonrpc", lambda m, p: {"result": {"value": "Box7"}})
    assert tools._get_devicename() == "Box7"


def test_get_devicename_bad_shape_returns_empty(wiz, monkeypatch):
    """A JSON-RPC error / wrong id yields '' (never raises) so callers stay guarded."""
    tools = wiz.tools
    monkeypatch.setattr(tools, "_jsonrpc", lambda m, p: {})
    assert tools._get_devicename() == ""


def test_set_devicename_success_writes_both_live_and_file(wiz, monkeypatch):
    """A successful live set ALSO writes guisettings.xml (both-ways persistence: the live
    set is durable on tvOS, the file write survives a Fire TV / Android unclean shutdown)."""
    tools = wiz.tools
    monkeypatch.setattr(tools, "_jsonrpc", lambda m, p: {"result": True})
    from resources.lib.modules import _kodisettings

    wrote = []
    monkeypatch.setattr(
        _kodisettings,
        "write_guisetting",
        lambda path, sid, val: wrote.append((sid, val)) or True,
    )
    assert tools._set_devicename("NewName") is True
    assert wrote == [("services.devicename", "NewName")], "must persist to the file too"


def test_set_devicename_failure_does_not_touch_file(wiz, monkeypatch):
    """When the live set fails, the file is NOT written (no half-applied name on disk)."""
    tools = wiz.tools
    monkeypatch.setattr(tools, "_jsonrpc", lambda m, p: {"result": False})
    from resources.lib.modules import _kodisettings

    monkeypatch.setattr(
        _kodisettings,
        "write_guisetting",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not write the file when the live set failed")
        ),
    )
    assert tools._set_devicename("NewName") is False


def test_write_guisetting_updates_existing_and_clears_default(wiz, tmp_path):
    """write_guisetting overwrites an existing <setting> and drops its default='true' marker
    so Kodi treats the value as user-set."""
    import xml.etree.ElementTree as ET

    from resources.lib.modules import _kodisettings

    p = tmp_path / "guisettings.xml"
    p.write_text(
        '<settings version="2">'
        '<setting id="services.devicename" default="true">Kodi</setting>'
        "</settings>"
    )
    assert _kodisettings.write_guisetting(str(p), "services.devicename", "Living Room")
    node = [
        n
        for n in ET.parse(str(p)).getroot().iter("setting")
        if n.get("id") == "services.devicename"
    ][0]
    assert node.text == "Living Room"
    assert node.get("default") is None


def test_write_guisetting_creates_missing_element(wiz, tmp_path):
    """If the setting isn't present yet, it is created."""
    import xml.etree.ElementTree as ET

    from resources.lib.modules import _kodisettings

    p = tmp_path / "guisettings.xml"
    p.write_text(
        '<settings version="2"><setting id="other.thing">x</setting></settings>'
    )
    assert _kodisettings.write_guisetting(str(p), "services.devicename", "Box9")
    node = [
        n
        for n in ET.parse(str(p)).getroot().iter("setting")
        if n.get("id") == "services.devicename"
    ]
    assert node and node[0].text == "Box9"


def test_write_guisetting_missing_file_returns_false(wiz, tmp_path):
    """A missing guisettings.xml is a guarded no-op (returns False, never raises)."""
    from resources.lib.modules import _kodisettings

    assert (
        _kodisettings.write_guisetting(
            str(tmp_path / "nope.xml"), "services.devicename", "X"
        )
        is False
    )


# --------------------------------------------------------------------------- #
# Extract-root contract (bugs #1/#2/#3/#7): a restore must extract to the root the zip is
# anchored at, and drop stray HOME-root pollution.
# --------------------------------------------------------------------------- #
def test_archive_anchor_home_vs_userdata(wiz):
    assert wiz._archive_anchor(["userdata/guisettings.xml", "addons/x/y"]) == "home"
    assert (
        wiz._archive_anchor(
            ["guisettings.xml", "addon_data/pvr.iptvsimple/instance-settings-1.xml"]
        )
        == "userdata"
    )
    assert wiz._archive_anchor([], hint="home") == "home"
    assert wiz._archive_anchor([]) == "userdata"  # degenerate default


def test_extract_skip_predicate(wiz):
    skip_home = wiz._extract_skip("home", "temp/")
    assert skip_home("temp/x.zip") is True  # temp self-ref
    assert skip_home("userdata/guisettings.xml") is False  # allowed
    assert (
        skip_home("addon_data/pvr.iptvsimple/instance-settings-1.xml") is True
    )  # stray
    assert skip_home("guisettings.xml") is True  # stray root file
    skip_ud = wiz._extract_skip("userdata", None)
    # on a userdata anchor addon_data/ and guisettings.xml ARE the real content -> keep
    assert skip_ud("addon_data/pvr.iptvsimple/instance-settings-1.xml") is False
    assert skip_ud("guisettings.xml") is False


def _prep_restore(wiz, monkeypatch, tmp_path):
    """control.HOME + control.USERDATA as real tmp dirs; ask_restart stubbed."""
    home = tmp_path / "home"
    (home / "userdata").mkdir(parents=True)
    monkeypatch.setattr(wiz.control, "HOME", str(home))
    monkeypatch.setattr(wiz.control, "USERDATA", str(home / "userdata"))
    monkeypatch.setattr(wiz.ui, "ask_restart", lambda *a, **k: None)
    return home


def test_restore_userdata_zip_lands_under_userdata_not_home(wiz, monkeypatch, tmp_path):
    """THE regression guard: a userdata-anchored 'kodi_settings' zip must extract UNDER
    userdata/, never scattered at the HOME root (the bug that bricked the box)."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    src = tmp_path / "kodi_settings_202607081313.zip"
    _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<settings/>"),
            ("addon_data/pvr.iptvsimple/instance-settings-1.xml", "<i/>"),
            ("sources.xml", "<sources/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert (home / "userdata" / "guisettings.xml").exists()
    assert (
        home / "userdata" / "addon_data" / "pvr.iptvsimple" / "instance-settings-1.xml"
    ).exists()
    assert not (home / "guisettings.xml").exists(), "must NOT scatter into HOME root"
    assert not (home / "addon_data").exists(), "must NOT scatter into HOME root"


def test_restore_full_zip_still_lands_at_home(wiz, monkeypatch, tmp_path):
    """A home-anchored full backup extracts to HOME unchanged (regression guard)."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    src = tmp_path / "kodi_backup_202607081313.zip"
    _make_valid_zip(
        src,
        [
            ("userdata/guisettings.xml", "<settings/>"),
            ("addons/plugin.x/addon.xml", "<a/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert (home / "userdata" / "guisettings.xml").exists()
    assert (home / "addons" / "plugin.x" / "addon.xml").exists()


def test_extract_filter_drops_stray_root_pollution(wiz, monkeypatch, tmp_path):
    """A polluted FULL backup carrying BOTH the real userdata/ copy AND stray root copies:
    only the userdata/ copies land; the strays are dropped (breaks the crash feedback loop)."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    src = tmp_path / "kodi_backup_polluted.zip"
    _make_valid_zip(
        src,
        [
            ("userdata/addon_data/pvr.iptvsimple/instance-settings-1.xml", "<real/>"),
            ("userdata/guisettings.xml", "<settings/>"),
            ("addons/plugin.x/addon.xml", "<a/>"),
            # stray HOME-root pollution (must be dropped):
            ("addon_data/pvr.iptvsimple/instance-settings-1.xml", "<stray/>"),
            ("guisettings.xml", "<stray/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert (
        home / "userdata" / "addon_data" / "pvr.iptvsimple" / "instance-settings-1.xml"
    ).exists()
    assert not (home / "addon_data").exists(), "stray root addon_data must be dropped"
    assert not (home / "guisettings.xml").exists(), (
        "stray root guisettings must be dropped"
    )


def test_createzip_prunes_home_root(wiz, tmp_path):
    """A FULL backup (prune_home_root=True) captures only allowed home-level dirs; stray
    root pollution is NOT re-captured. prune_home_root=False keeps everything (userdata mode)."""
    import zipfile as _zip

    home = tmp_path / "home"
    (home / "userdata" / "addon_data").mkdir(parents=True)
    (home / "userdata" / "guisettings.xml").write_text("<s/>")
    (home / "addons" / "plugin.x").mkdir(parents=True)
    (home / "addons" / "plugin.x" / "addon.xml").write_text("<a/>")
    # stray pollution at home root:
    (home / "addon_data" / "pvr.iptvsimple").mkdir(parents=True)
    (home / "addon_data" / "pvr.iptvsimple" / "instance-settings-1.xml").write_text(
        "<x/>"
    )
    (home / "guisettings.xml").write_text("<stray/>")

    class _P:
        def cancelled(self):
            return False

        def items(self, *a, **k):
            pass

    import contextlib

    @contextlib.contextmanager
    def _prog(*a, **k):
        yield _P()

    import unittest.mock as mock

    with mock.patch.object(wiz.ui, "Progress", _prog):
        out = tmp_path / "full.zip"
        wiz.CreateZip(
            str(home), str(out), "h", "m", ["temp"], [".log"], prune_home_root=True
        )
        names = set(_zip.ZipFile(out).namelist())
        out2 = tmp_path / "nopr.zip"
        wiz.CreateZip(str(home), str(out2), "h", "m", ["temp"], [".log"])
        names2 = set(_zip.ZipFile(out2).namelist())

    assert "userdata/guisettings.xml" in names and "addons/plugin.x/addon.xml" in names
    assert not any(n.startswith("addon_data/") for n in names), "stray root pruned"
    assert "guisettings.xml" not in names, "loose root file pruned"
    # without pruning the strays ARE captured (proves userdata-mode is unaffected):
    assert any(n.startswith("addon_data/") for n in names2)
    assert "guisettings.xml" in names2


def test_createzip_never_embeds_ezm_own_settings(wiz, tmp_path):
    """The backup must NEVER carry EZM's own settings.xml (Dropbox token + the
    source box's paths). Regression for the secret leak backup_lint caught on a
    real Fire TV 2026-07-16: the exclusion existed only on the tvOS NSUD path, not
    on the POSIX walk. Covers both a full and a userdata-mode backup, and a
    per-profile copy."""
    import contextlib
    import unittest.mock as mock
    import zipfile as _zip

    home = tmp_path / "home"
    ez = home / "userdata" / "addon_data" / "script.ezmaintenanceplusplus"
    ez.mkdir(parents=True)
    (ez / "settings.xml").write_text("<settings><token/></settings>")
    (ez / "data.json").write_text("{}")  # non-secret sibling: MUST be captured
    prof = (
        home
        / "userdata"
        / "profiles"
        / "kid"
        / "addon_data"
        / "script.ezmaintenanceplusplus"
    )
    prof.mkdir(parents=True)
    (prof / "settings.xml").write_text("<settings><token/></settings>")
    (home / "userdata" / "guisettings.xml").write_text("<s/>")

    class _P:
        def cancelled(self):
            return False

        def items(self, *a, **k):
            pass

    @contextlib.contextmanager
    def _prog(*a, **k):
        yield _P()

    with mock.patch.object(wiz.ui, "Progress", _prog):
        full = tmp_path / "full.zip"
        wiz.CreateZip(str(home), str(full), "h", "m", ["temp"], [".log"])
        names = set(_zip.ZipFile(full).namelist())

    secret_tail = "addon_data/script.ezmaintenanceplusplus/settings.xml"
    assert not any(n.endswith(secret_tail) for n in names), (
        "EZM's own settings.xml (top-level OR per-profile) must never be backed up"
    )
    # the non-secret sibling and other userdata are still captured
    assert any(n.endswith("script.ezmaintenanceplusplus/data.json") for n in names)
    assert any(n.endswith("userdata/guisettings.xml") for n in names)


def test_sweep_and_iptv_removed_from_wiz(wiz):
    """SAFETY BY CONSTRUCTION: the boot-time home-root delete sweep and all IPTV
    enable/disable/stage automation are gone from wiz. Nothing here deletes files at
    boot, and a restore never toggles the IPTV client (or any add-on). The ONLY
    IPTV-adjacent behavior left is the restore-side duplicate-instance sweep
    (_sweep_iptv_instances), which removes stale instance-settings-*.xml so the
    restored state equals the archive - covered by its own tests below."""
    # The sweep function no longer exists as an attribute (nothing can call it).
    assert not hasattr(wiz, "sweep_home_root_pollution")
    assert not hasattr(wiz, "_USERDATA_STRAY_NAMES")

    src = (ADDON_ROOT / "resources" / "lib" / "modules" / "wiz.py").read_text(
        encoding="utf-8"
    )
    for gone in (
        "stage_iptv_disabled",
        "mark_iptv_autoenable_pending",
        "set_pvr_enabled",
        "pvr_is_enabled",
        "def sweep_home_root_pollution",
    ):
        assert gone not in src, "wiz.py must no longer contain %r" % gone


def test_restore_does_not_toggle_any_addon(wiz, monkeypatch, tmp_path):
    """A restore must not enable or disable ANY add-on. The restored files are placed and the
    settings are made durable, but no client state is flipped (that is what crashed the box)."""
    _prep_restore(wiz, monkeypatch, tmp_path)

    calls = []
    monkeypatch.setattr(
        wiz.xbmc,
        "executeJSONRPC",
        lambda payload: calls.append(payload) or '{"result":"OK"}',
    )
    monkeypatch.setattr(wiz, "ExtractWithProgress", lambda *a, **k: False)  # completes

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])

    wiz.restore(str(src), confirm=False, wipe=False)

    assert not any("SetAddonEnabled" in c for c in calls), (
        "a restore must never enable/disable an add-on"
    )


# --------------------------------------------------------------------------- #
# Honest backup: per-file failure accounting (A), manifest (C), root-only temp
# exclusion (D), and the loud tvOS capture contract (B).
# --------------------------------------------------------------------------- #
class _NoopProgress:
    def cancelled(self):
        return False

    def items(self, *a, **k):
        pass


def _run_create_zip(wiz, src, dst, exclude_dirs=("temp",), prune=False):
    """Drive the REAL CreateZip with ui.Progress mocked out (no dialog)."""
    import contextlib
    import unittest.mock as mock

    @contextlib.contextmanager
    def _prog(*a, **k):
        yield _NoopProgress()

    with mock.patch.object(wiz.ui, "Progress", _prog):
        return wiz.CreateZip(
            str(src),
            str(dst),
            "h",
            "m",
            list(exclude_dirs),
            [".log"],
            prune_home_root=prune,
        )


def _load_nsub():
    return importlib.import_module("resources.lib.modules.nsub")


def _home_with(tmp_path, files, name="srchome"):
    """A home tree with the given (relpath, body) files; returns its Path."""
    home = tmp_path / name
    for rel, body in files:
        p = home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return home


def test_createzip_per_file_failure_keeps_rest_of_directory(wiz, tmp_path):
    """(A) One unreadable file is COUNTED and NAMED; it never silently drops the
    rest of its directory (the old per-directory except swallowed every file after
    the first failure)."""
    import os as _os
    import zipfile as _zip

    if _os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 cannot make a file unreadable")
    home = _home_with(
        tmp_path,
        [
            ("userdata/a.xml", "<a/>"),
            ("userdata/b.xml", "<b/>"),
            ("userdata/c.xml", "<c/>"),
        ],
    )
    (home / "userdata" / "b.xml").chmod(0)
    out = tmp_path / "out.zip"
    try:
        result = _run_create_zip(wiz, home, out)
    finally:
        (home / "userdata" / "b.xml").chmod(0o644)

    names = set(_zip.ZipFile(out).namelist())
    assert "userdata/a.xml" in names and "userdata/c.xml" in names, (
        "the files AFTER the unreadable one must still be captured"
    )
    assert "userdata/b.xml" not in names
    assert result.canceled is False
    assert result.failed == ["userdata/b.xml"], "the failure must be named"


def test_createzip_manifest_records_failures(wiz, tmp_path):
    """(C) The embedded manifest carries created/source_os/entries/failed, with
    failed naming exactly what the backup could not capture."""
    import json as _json
    import os as _os
    import zipfile as _zip

    if _os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 cannot make a file unreadable")
    home = _home_with(
        tmp_path, [("userdata/good.xml", "<g/>"), ("userdata/bad.xml", "<b/>")]
    )
    (home / "userdata" / "bad.xml").chmod(0)
    out = tmp_path / "out.zip"
    try:
        result = _run_create_zip(wiz, home, out)
    finally:
        (home / "userdata" / "bad.xml").chmod(0o644)

    with _zip.ZipFile(out) as z:
        names = z.namelist()
        assert wiz.MANIFEST_NAME in names
        manifest = _json.loads(z.read(wiz.MANIFEST_NAME).decode("utf-8"))
    assert manifest["source_os"] == "other"
    assert manifest["failed"] == ["userdata/bad.xml"]
    assert manifest["entries"] == len([n for n in names if n != wiz.MANIFEST_NAME])
    assert manifest["created"], "an ISO created stamp must be present"
    assert result.failed == ["userdata/bad.xml"]


def test_createzip_manifest_clean_backup(wiz, tmp_path):
    """(C) A clean backup's manifest has an accurate entry count and no failures."""
    import json as _json
    import zipfile as _zip

    home = _home_with(
        tmp_path, [("userdata/a.xml", "<a/>"), ("addons/x/addon.xml", "<x/>")]
    )
    out = tmp_path / "out.zip"
    result = _run_create_zip(wiz, home, out)

    with _zip.ZipFile(out) as z:
        names = z.namelist()
        manifest = _json.loads(z.read(wiz.MANIFEST_NAME).decode("utf-8"))
    assert manifest["failed"] == []
    assert manifest["entries"] == 2
    assert len([n for n in names if n != wiz.MANIFEST_NAME]) == 2
    assert result.failed == [] and result.entries == 2


def test_createzip_prunes_temp_only_at_walk_root(wiz, tmp_path):
    """(D) exclude_dirs=["temp"] means special://home/temp - the WALK ROOT only. A
    nested dir that merely happens to be named temp is real content."""
    import zipfile as _zip

    home = _home_with(
        tmp_path,
        [
            ("temp/junk.txt", "x"),
            ("userdata/addon_data/plugin.x/temp/keep.txt", "y"),
        ],
    )
    out = tmp_path / "out.zip"
    _run_create_zip(wiz, home, out)

    names = set(_zip.ZipFile(out).namelist())
    assert "userdata/addon_data/plugin.x/temp/keep.txt" in names, (
        "a NESTED temp dir must be captured"
    )
    assert not any(n.startswith("temp/") for n in names), (
        "the root temp dir must be excluded"
    )


def test_createzip_tvos_capture_exception_fails_backup(wiz, monkeypatch, tmp_path):
    """(B) On tvOS a raising NSUserDefaults capture FAILS the backup loudly
    (BackupCaptureError) and removes the partial zip."""
    nsub = _load_nsub()
    monkeypatch.setattr(wiz, "_source_os", lambda: "tvos")

    def boom(*a, **k):
        raise RuntimeError("plist unreadable")

    monkeypatch.setattr(nsub, "capture_nsud_userdata", boom)
    home = _home_with(tmp_path, [("userdata/a.xml", "<a/>")])
    out = tmp_path / "out.zip"
    with pytest.raises(wiz.BackupCaptureError):
        _run_create_zip(wiz, home, out)
    assert not out.exists(), "the partial zip must be removed on a failed capture"


def test_createzip_tvos_capture_failed_entries_fail_backup(wiz, monkeypatch, tmp_path):
    """(B) On tvOS a capture reporting failed entries fails the backup - the zip
    would be missing settings the owner cares about."""
    nsub = _load_nsub()
    monkeypatch.setattr(wiz, "_source_os", lambda: "tvos")
    monkeypatch.setattr(nsub, "capture_nsud_userdata", lambda *a, **k: (3, 2, 1))
    home = _home_with(tmp_path, [("userdata/a.xml", "<a/>")])
    with pytest.raises(wiz.BackupCaptureError):
        _run_create_zip(wiz, home, tmp_path / "out.zip")


def test_createzip_tvos_missing_store_fails_backup(wiz, monkeypatch, tmp_path):
    """(B) On tvOS the NSUserDefaults store ALWAYS exists; a capture that finds
    nothing at all means it was never read - fail the backup."""
    nsub = _load_nsub()
    monkeypatch.setattr(wiz, "_source_os", lambda: "tvos")
    monkeypatch.setattr(nsub, "capture_nsud_userdata", lambda *a, **k: (0, 0, 0))
    home = _home_with(tmp_path, [("userdata/a.xml", "<a/>")])
    with pytest.raises(wiz.BackupCaptureError):
        _run_create_zip(wiz, home, tmp_path / "out.zip")


def test_createzip_non_tvos_capture_error_is_noop(wiz, monkeypatch, tmp_path):
    """(B) Off tvOS the capture is a true no-op: a hiccup is logged, the backup
    completes cleanly with no failures recorded."""
    import zipfile as _zip

    nsub = _load_nsub()

    def boom(*a, **k):
        raise RuntimeError("no plist here")

    monkeypatch.setattr(nsub, "capture_nsud_userdata", boom)
    home = _home_with(tmp_path, [("userdata/a.xml", "<a/>")])
    out = tmp_path / "out.zip"
    result = _run_create_zip(wiz, home, out)
    assert result.canceled is False and result.failed == []
    names = set(_zip.ZipFile(out).namelist())
    assert "userdata/a.xml" in names and wiz.MANIFEST_NAME in names


def _stub_local_backup_env(wiz, monkeypatch, tmp_path, home):
    """backup() stubs with a LOCAL download.path (no VFS ship) over a real home."""
    dest = tmp_path / "dest"
    dest.mkdir()
    monkeypatch.setattr(wiz.control, "HOME", str(home))
    monkeypatch.setattr(
        wiz.control,
        "setting",
        lambda key: str(dest) if key == "download.path" else "",
    )
    monkeypatch.setattr(wiz.tools, "_get_keyboard", lambda **k: "mybackup")
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(
        wiz,
        "xbmcaddon",
        types.SimpleNamespace(
            Addon=lambda: types.SimpleNamespace(getSetting=lambda k: "false")
        ),
    )
    return dest


def test_backup_tvos_capture_failure_no_success_no_rotation(wiz, monkeypatch, tmp_path):
    """(B) backup(): a tvOS capture failure surfaces an error dialog, never claims
    success, and never rotates the previous good backup."""
    nsub = _load_nsub()
    home = _home_with(tmp_path, [("userdata/a.xml", "<a/>")], name="home")
    _stub_local_backup_env(wiz, monkeypatch, tmp_path, home)
    monkeypatch.setattr(wiz, "_source_os", lambda: "tvos")

    def boom(*a, **k):
        raise RuntimeError("plist unreadable")

    monkeypatch.setattr(nsub, "capture_nsud_userdata", boom)
    rotations = []
    monkeypatch.setattr(wiz, "_rotate_vfs", lambda *a, **k: rotations.append(a))
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )

    wiz.backup(mode="full")

    assert rotations == [], "a failed backup must never rotate the previous one"
    assert oks, "an error dialog must be shown"
    assert "FAILED" in oks[-1]
    assert not any("Backup complete" in m for m in oks)


def test_backup_reports_uncaptured_files_before_claiming_success(
    wiz, monkeypatch, tmp_path
):
    """(A/C) backup(): a backup with unreadable files says EXACTLY what is missing
    in the completion dialog instead of a bare 'Backup complete'."""
    import os as _os

    if _os.geteuid() == 0:
        pytest.skip("running as root: chmod 000 cannot make a file unreadable")
    home = _home_with(
        tmp_path,
        [("userdata/good.xml", "<g/>"), ("userdata/bad.xml", "<b/>")],
        name="home",
    )
    _stub_local_backup_env(wiz, monkeypatch, tmp_path, home)
    monkeypatch.setattr(wiz, "_rotate_vfs", lambda *a, **k: None)
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )
    (home / "userdata" / "bad.xml").chmod(0)
    try:
        wiz.backup(mode="full")
    finally:
        (home / "userdata" / "bad.xml").chmod(0o644)

    assert oks, "a completion dialog must be shown"
    assert "userdata/bad.xml" in oks[-1], "the missing file must be NAMED"
    assert "could NOT be captured" in oks[-1]


# --------------------------------------------------------------------------- #
# Truthful restore reporting (E) + manifest verification (F).
# --------------------------------------------------------------------------- #
def _record_restore_report(wiz, monkeypatch, retry=False):
    """Capture ask_restart statuses, dialog.ok messages, and dialog.yesno prompts
    from restore(). `retry` is the canned answer to the locked Try Again prompt."""
    statuses = []
    monkeypatch.setattr(
        wiz.ui, "ask_restart", lambda status="", **k: statuses.append(status)
    )
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )
    yesnos = []

    def _yesno(*a, **k):
        yesnos.append(" ".join(str(x) for x in a))
        return retry

    monkeypatch.setattr(wiz.dialog, "yesno", _yesno)
    return statuses, oks, yesnos


def test_restore_member_failure_asks_with_locked_problem_copy(
    wiz, monkeypatch, tmp_path
):
    """(E) A member that fails to extract is a HARD problem: the user sees the
    LOCKED Problem prompt (Try Again / Close) and nothing else - no counts, no
    paths, no 'INCOMPLETE' (those live in the log). Declining still drives the
    restart prompt and never claims Complete."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch, retry=False)
    # Extraction target for blocked.xml is an existing DIRECTORY -> extract fails.
    (home / "userdata" / "blocked.xml").mkdir(parents=True)

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("blocked.xml", "<b/>")])

    wiz.restore(str(src), confirm=False)

    assert yesnos and wiz.MSG_PROBLEM in yesnos[0], yesnos
    assert oks == [], "declining Try Again must not stack another dialog"
    assert statuses == [""], "the restart prompt still runs, with no status jargon"
    for shown in yesnos + statuses:
        assert "INCOMPLETE" not in shown and "blocked.xml" not in shown


def test_restore_member_failure_retry_twice_then_problem(wiz, monkeypatch, tmp_path):
    """(E) Accepting Try Again re-runs the whole restore once; a second HARD failure
    (backup content still did not restore) shows the locked PROBLEM wording - never a
    third attempt, never the softer needs-attention (audit Finding C: a hard content
    loss must say 'couldn't be restored', not 'needs attention')."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch, retry=True)
    (home / "userdata" / "blocked.xml").mkdir(parents=True)

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("blocked.xml", "<b/>")])

    wiz.restore(str(src), confirm=False)

    assert len(yesnos) == 1, "the Problem prompt asks exactly once (2-attempt cap)"
    assert oks == [wiz.AddonTitle + " " + wiz.MSG_PROBLEM]
    assert statuses == [""]


def test_restore_success_shows_only_locked_complete(wiz, monkeypatch, tmp_path):
    """(E) A clean restore shows EXACTLY the locked Complete status - no counts,
    no settings tally, no extra dialogs. The numbers live in the log."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<s/>"),
            ("sources.xml", "<s/>"),
            ("RssFeeds.xml", "<r/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert oks == [] and yesnos == []


def test_restore_manifest_mismatch_reports_partial(wiz, monkeypatch, tmp_path):
    """(F) A manifest whose entry count does not match the archive is surfaced as a
    problem: the report is INCOMPLETE even though every member extracted."""
    import json as _json

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch, retry=False)

    src = tmp_path / "kodi_settings_x.zip"
    manifest = {"created": "t", "source_os": "tvos", "entries": 5, "failed": []}
    _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<s/>"),
            (wiz.MANIFEST_NAME, _json.dumps(manifest)),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert yesnos and wiz.MSG_PROBLEM in yesnos[0], yesnos
    assert statuses == [""]
    for shown in yesnos + oks + statuses:
        assert "manifest" not in shown, "manifest detail belongs in the log"


def test_restore_manifest_backup_gaps_surface(wiz, monkeypatch, tmp_path):
    """(F) A manifest recording backup-time failures tells the user the RESTORE
    cannot contain those items - surfaced, never silently dropped."""
    import json as _json

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch, retry=False)

    src = tmp_path / "kodi_settings_x.zip"
    manifest = {
        "created": "t",
        "source_os": "tvos",
        "entries": 1,
        "failed": ["userdata/secret.xml"],
    }
    _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<s/>"),
            (wiz.MANIFEST_NAME, _json.dumps(manifest)),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert yesnos and wiz.MSG_PROBLEM in yesnos[0], yesnos
    assert statuses == [""]
    for shown in yesnos + oks + statuses:
        assert "secret.xml" not in shown, "the failed path belongs in the log"


def test_restore_matching_manifest_reports_complete_and_skips_manifest(
    wiz, monkeypatch, tmp_path
):
    """(F) A consistent manifest verifies cleanly; the manifest member itself is
    metadata and is never extracted to disk. Archives WITHOUT a manifest are
    tolerated (covered by the other restore tests)."""
    import json as _json

    home = _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, _oks, _yesnos = _record_restore_report(wiz, monkeypatch)

    src = tmp_path / "kodi_settings_x.zip"
    manifest = {"created": "t", "source_os": "other", "entries": 2, "failed": []}
    _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<s/>"),
            ("sources.xml", "<s/>"),
            (wiz.MANIFEST_NAME, _json.dumps(manifest)),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert not (home / "userdata" / wiz.MANIFEST_NAME).exists()
    assert not (home / wiz.MANIFEST_NAME).exists()


def test_restore_complete_despite_stale_key_purge_failures(wiz, monkeypatch, tmp_path):
    """A stale-key purge that cannot clear PRE-EXISTING vector-everything-era keys
    (undecodable, or a tvOS async-flush confirm miss) must NOT downgrade the
    restore to INCOMPLETE - the purge is hygiene of old cruft this restore did not
    create. Regression for the false 'Restore INCOMPLETE' seen on atv2 2026-07-16,
    where extract/sweep/rewrite were all 0-failed but the purge reported failures."""
    from resources.lib.modules import nsud

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    # The purge reports 3 UNRESOLVED pre-existing keys; the rewrite is clean.
    monkeypatch.setattr(nsud, "purge_stale_keys", lambda root, log=None: (0, 5, 2, 3))
    monkeypatch.setattr(
        nsud, "rewrite_userdata_xml", lambda root, log=None: (0, 0, 0, 0)
    )

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("sources.xml", "<s/>")])

    wiz.restore(str(src), confirm=False)

    assert statuses and statuses[0].startswith("Restore Complete"), statuses
    assert not any(s.startswith("Restore INCOMPLETE") for s in statuses)
    assert oks == []  # no INCOMPLETE dialog


def test_backup_restore_roundtrip_reports_complete(wiz, monkeypatch, tmp_path):
    """End-to-end: a real CreateZip backup (manifest included) restores with a
    clean manifest verification and a truthful Complete report."""
    srchome = _home_with(
        tmp_path,
        [("userdata/guisettings.xml", "<s/>"), ("addons/plugin.x/addon.xml", "<a/>")],
    )
    out = tmp_path / "kodi_backup_202607161200.zip"
    _run_create_zip(wiz, srchome, out, prune=True)

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)

    wiz.restore(str(out), confirm=False)

    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert oks == [] and yesnos == []


# --------------------------------------------------------------------------- #
# Restore-side IPTV duplicate-instance sweep (G).
# --------------------------------------------------------------------------- #
def test_iptv_profile_prefixes(wiz):
    """The sweep scope comes from the ARCHIVE: top-level and/or per-profile, and
    only when pvr.iptvsimple addon_data is actually present."""
    assert wiz._iptv_profile_prefixes(
        ["userdata/addon_data/pvr.iptvsimple/instance-settings-1.xml"], "home"
    ) == {""}
    assert wiz._iptv_profile_prefixes(
        [
            "userdata/addon_data/pvr.iptvsimple/instance-settings-1.xml",
            "userdata/profiles/Kids/addon_data/pvr.iptvsimple/instance-settings-2.xml",
        ],
        "home",
    ) == {"", "profiles/Kids/"}
    assert wiz._iptv_profile_prefixes(
        ["addon_data/pvr.iptvsimple/customTVGroups.xml"], "userdata"
    ) == {""}
    # home anchor: a bare addon_data/ member is stray pollution, not userdata content
    assert (
        wiz._iptv_profile_prefixes(
            ["addon_data/pvr.iptvsimple/instance-settings-1.xml"], "home"
        )
        == set()
    )
    assert wiz._iptv_profile_prefixes(["userdata/guisettings.xml"], "home") == set()


def test_restore_sweeps_stale_iptv_instances(wiz, monkeypatch, tmp_path):
    """(G) When the archive carries pvr.iptvsimple config, the TARGET's existing
    instance-settings-*.xml are removed first, so instance numbering can never
    accumulate (the 2026-07-08 duplicate-instance brick). settings.xml and the
    archive's own instance files land normally."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    iptv = home / "userdata" / "addon_data" / "pvr.iptvsimple"
    iptv.mkdir(parents=True)
    (iptv / "instance-settings-1.xml").write_text("<old/>")
    (iptv / "instance-settings-7.xml").write_text("<stale/>")
    (iptv / "settings.xml").write_text("<keep/>")

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(
        src,
        [
            ("addon_data/pvr.iptvsimple/instance-settings-1.xml", "<new/>"),
            ("guisettings.xml", "<s/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert not (iptv / "instance-settings-7.xml").exists(), (
        "a stale instance NOT in the archive must be swept"
    )
    assert (iptv / "instance-settings-1.xml").read_text() == "<new/>"
    assert (iptv / "settings.xml").read_text() == "<keep/>", (
        "the sweep only touches instance-settings-*.xml"
    )


def test_restore_without_iptv_leaves_target_instances_alone(wiz, monkeypatch, tmp_path):
    """(G) No pvr.iptvsimple entries in the archive -> no sweep: the target's IPTV
    config is not EZM's to touch."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    iptv = home / "userdata" / "addon_data" / "pvr.iptvsimple"
    iptv.mkdir(parents=True)
    (iptv / "instance-settings-7.xml").write_text("<keep/>")

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])

    wiz.restore(str(src), confirm=False)

    assert (iptv / "instance-settings-7.xml").read_text() == "<keep/>"


def test_restore_sweep_scopes_per_profile_to_archive(wiz, monkeypatch, tmp_path):
    """(G) Per-profile sweep only for profiles the archive carries; a top-level
    instance file survives when the archive has no top-level IPTV entries."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    top = home / "userdata" / "addon_data" / "pvr.iptvsimple"
    top.mkdir(parents=True)
    (top / "instance-settings-8.xml").write_text("<top/>")
    kids = home / "userdata" / "profiles" / "Kids" / "addon_data" / "pvr.iptvsimple"
    kids.mkdir(parents=True)
    (kids / "instance-settings-9.xml").write_text("<stale/>")

    src = tmp_path / "kodi_backup_x.zip"
    _make_valid_zip(
        src,
        [
            (
                "userdata/profiles/Kids/addon_data/pvr.iptvsimple/"
                "instance-settings-1.xml",
                "<k/>",
            ),
            ("addons/plugin.x/addon.xml", "<a/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert not (kids / "instance-settings-9.xml").exists(), (
        "the archive's profile must be swept"
    )
    assert (kids / "instance-settings-1.xml").read_text() == "<k/>"
    assert (top / "instance-settings-8.xml").read_text() == "<top/>", (
        "a profile the archive does NOT carry is left alone"
    )


def test_restore_sweep_drops_nsud_key_without_posix_file(wiz, monkeypatch, tmp_path):
    """(G, tvOS) An instance-settings key that exists ONLY in NSUserDefaults (no
    disk file) is still swept: the key is dropped via the special:// path (the
    sanctioned two-layer delete). Non-IPTV keys are never touched."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    # The sweep machinery lives in nsud (wiz delegates); patch ITS plist reader.
    from resources.lib.modules import nsud

    store = {
        "/userdata/addon_data/pvr.iptvsimple/instance-settings-3.xml": b"x",
        "/userdata/guisettings.xml": b"g",
    }
    monkeypatch.setattr(nsud, "_find_nsud_plist", lambda: ("plist", store))
    deleted = []
    monkeypatch.setattr(nsud.xbmcvfs, "delete", lambda p: deleted.append(p))
    # Verification now reads the LIVE layer (listdir dup-count), not a plist
    # re-read: after the drop the key is gone, so the name is no longer listed.
    monkeypatch.setattr(nsud, "_vfs_dir_names", lambda _reldir: [])

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(
        src,
        [
            ("addon_data/pvr.iptvsimple/instance-settings-1.xml", "<new/>"),
            ("guisettings.xml", "<s/>"),
        ],
    )

    wiz.restore(str(src), confirm=False)

    assert (
        "special://home/userdata/addon_data/pvr.iptvsimple/instance-settings-3.xml"
        in deleted
    ), "the key-only stale instance must be dropped"
    assert not any("guisettings" in d for d in deleted), (
        "the sweep must never delete non-IPTV userdata"
    )


# --------------------------------------------------------------------------- #
# The restore UX contract (owner-locked 2026-07-17): four messages total, no
# jargon, verification before reporting, silent auto-fix, skin as boot state.
# Born from the atv2 round-trip where the honest-but-raw reporting read as
# breakage and its modal ate Kodi's keep-skin confirmation.
# --------------------------------------------------------------------------- #
def test_locked_vocabulary_is_pinned(wiz):
    """The owner-edited strings, byte for byte. Implementation may not reword."""
    assert wiz.MSG_COMPLETE == "Restore Complete"
    assert wiz.MSG_PROBLEM == (
        "Restore Problem\n"
        "Some of your backup couldn't be restored, so this box may not work "
        "the way it did before."
    )
    assert wiz.MSG_NEEDS_ATTENTION == (
        "Restore Problem\n"
        "Your restore finished, but one setting may not have applied. "
        "Restore again, or restart the box."
    )


def test_attention_wording_matches_the_boot_check_toast():
    """wiz's restore dialog and service.py's boot toast fire on the SAME condition,
    so they must say the same thing.

    They drifted once: 2026.07.19.5 rewrote the toast to name a real action but left
    MSG_NEEDS_ATTENTION pointing at "open EZ Maintenance++" - a menu whose only
    relevant entry that release had just deleted. One owner, one condition, two
    different instructions, one of them dead. This pins them together so the next
    reword cannot land in only one place.
    """
    import pathlib

    from resources.lib.modules import wiz as wiz_mod

    service_src = (pathlib.Path(wiz_mod.__file__).parents[3] / "service.py").read_text(
        encoding="utf-8"
    )

    action = "Restore again, or restart the box."
    assert action in wiz_mod.MSG_NEEDS_ATTENTION
    assert action in service_src, (
        "service.py's boot toast no longer carries the shared action sentence - "
        "the two restore-attention messages have drifted apart again"
    )
    # Neither may route the owner back into a menu that has no repair on it.
    assert "open EZ Maintenance++" not in wiz_mod.MSG_NEEDS_ATTENTION
    assert "open EZ Maintenance++" not in service_src


def test_attention_only_findings_auto_fix_silently(wiz, monkeypatch, tmp_path):
    """A fixable finding (e.g. a surviving stale key) triggers ONE silent fresh
    pass - no dialog, no question ("when this occurs, we should just fix it").
    When the second pass verifies clean, the user only ever sees Complete."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)

    from resources.lib.modules import restorecheck

    calls = []

    def _fake_verify(leftovers, names, anchor):
        calls.append(1)
        if len(calls) == 1:
            return (["1 stale NSUserDefaults key(s) still shadow restored"], [])
        return ([], [])

    monkeypatch.setattr(restorecheck, "verify_restored_state", _fake_verify)

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("sources.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)

    assert len(calls) == 2, "exactly one silent auto-fix pass"
    assert yesnos == [] and oks == [], "the auto-fix never surfaces a dialog"
    assert statuses == [wiz.MSG_COMPLETE]


def test_attention_surviving_auto_fix_needs_attention(wiz, monkeypatch, tmp_path):
    """If the silent fresh pass cannot clear the finding, the user sees exactly
    the locked needs-attention line - no counts, no key paths."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)

    from resources.lib.modules import restorecheck

    monkeypatch.setattr(
        restorecheck,
        "verify_restored_state",
        lambda *a, **k: (["1 stale key still shadows userdata/x.xml"], []),
    )

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)

    assert yesnos == [], "attention findings never ask - they auto-fix"
    assert oks == [wiz.AddonTitle + " " + wiz.MSG_NEEDS_ATTENTION]
    assert statuses == [""]
    assert "x.xml" not in oks[0], "key paths belong in the log"


# --------------------------------------------------------------------------- #
# _preserve_device_settings, tested DIRECTLY.
#
# These exist because the end-to-end preserve tests cannot see this function's own
# tvOS vector ordering: _apply_boot_skin runs immediately afterwards and calls
# nsud.persist_one("guisettings.xml") a SECOND time, which masks a preserve that
# vectored before its own writes. That masking disappears exactly when the archive
# records no skin - _apply_boot_skin returns early with "none" and never vectors - so
# the end-to-end suite is green while an Apple TV silently reopens carrying the SOURCE
# box's device name. Proven by mutation on 2026-07-19: moving persist_one above the
# writes was NOT caught by any end-to-end test.
# --------------------------------------------------------------------------- #
def _preserve_env(wiz, monkeypatch, tmp_path, archive_name="Golden Image Box"):
    """A userdata dir holding an ARCHIVE-valued guisettings.xml, plus a persist_one
    fake that snapshots what the file said AT THE MOMENT it was called.

    The archive's buffer is 200, distinct from Kodi's default 20 and from the 96 the
    tests hand in as a captured value, so "reset" cannot be confused with either "left
    alone" or "preserved"."""
    userdata = tmp_path / "userdata"
    userdata.mkdir(parents=True, exist_ok=True)
    gs = userdata / "guisettings.xml"
    gs.write_text(
        '<settings version="2">'
        '<setting id="services.devicename">%s</setting>'
        '<setting id="filecache.memorysize">200</setting>'
        "</settings>" % archive_name
    )
    monkeypatch.setattr(wiz.control, "USERDATA", str(userdata))

    snapshots = []
    from resources.lib.modules import nsud

    def _fake_persist_one(rel, log=None, **k):
        try:
            snapshots.append((rel, _setting_value(gs, "services.devicename")))
        except Exception:
            snapshots.append((rel, None))
        return True

    monkeypatch.setattr(nsud, "persist_one", _fake_persist_one)
    return gs, snapshots


def _setting_value(path, sid):
    import xml.etree.ElementTree as ET

    for n in ET.parse(str(path)).getroot().iter("setting"):
        if n.get("id") == sid:
            return (n.text or "").strip()
    return None


def test_preserve_vectors_only_after_its_own_writes(wiz, monkeypatch, tmp_path):
    """The tvOS vector must carry THIS box's values, not the archive's.

    On tvOS the NSUserDefaults key SHADOWS guisettings.xml and Kodi never copies a key
    back to disk, so whatever persist_one captured is what the box boots with. A vector
    taken before the write-back publishes the archive's device name permanently. This
    asserts against _preserve_device_settings alone, so no later persist_one can
    launder the ordering."""
    gs, snapshots = _preserve_env(wiz, monkeypatch, tmp_path)

    prop = wiz._preserve_device_settings(
        lambda m: None,
        {"services.devicename": "Living Room", "filecache.memorysize": 96},
    )

    # The buffer is reported ONCE, as the reset value. A capture that still carries the
    # buffer must be SKIPPED by the write-back loop, not written and then overwritten:
    # the ezm_preserved property is read off a real box over JSON-RPC to find out what
    # a restore did, and a property listing a number the box does not have makes it a
    # worse diagnostic than none. This is also the only place the skip guard is
    # observable, because the reset that follows the loop hides the double write.
    assert prop == "services.devicename:Living Room,filecache.memorysize:20", (
        "ezm_preserved is %r. A ':96' in there means an inherited buffer was written "
        "back before the reset overwrote it, and the property is now reporting a value "
        "no layer holds." % prop
    )
    assert snapshots, "the preserved file must be vectored into NSUserDefaults"
    assert [s for _rel, s in snapshots] == ["Living Room"], (
        "every vector must see this box's preserved name; a snapshot of %r means "
        "persist_one ran before the write-back and published the ARCHIVE's value"
        % [s for _rel, s in snapshots]
    )
    assert _setting_value(gs, "services.devicename") == "Living Room"
    # The buffer is RESET to Kodi's default, never preserved (owner decision
    # 2026-07-31), so the 96 handed in above must NOT reach the file - and neither may
    # the archive's 200. A capture that still carries the buffer cannot smuggle an
    # inherited number past the reset.
    assert _setting_value(gs, "filecache.memorysize") == "20", (
        "the buffer is %r: 96 means the captured value was written back (the deleted "
        "preserve behaviour), 200 means the archive's value stands and the reset never "
        "ran at all" % _setting_value(gs, "filecache.memorysize")
    )


def test_preserve_vectors_after_writing_every_value_not_just_the_first(
    wiz, monkeypatch, tmp_path
):
    """Every value must be in the file before the vector, not just the first.

    The preserved ids are written in a loop and the buffer reset runs after it, so a
    vector taken inside the loop - or inside the reset, which is why the reset is asked
    NOT to vector for itself - would carry one settled value and one archive value.
    Asserted on the BUFFER, which is written last, so it is the one an early vector
    would miss."""
    gs, snapshots = _preserve_env(wiz, monkeypatch, tmp_path)
    seen_buffer = []
    from resources.lib.modules import nsud

    def _snap(rel, log=None, **k):
        seen_buffer.append(_setting_value(gs, "filecache.memorysize"))
        return True

    monkeypatch.setattr(nsud, "persist_one", _snap)

    wiz._preserve_device_settings(
        lambda m: None,
        {"services.devicename": "Living Room", "filecache.memorysize": 96},
    )

    assert seen_buffer == ["20"], (
        "the vector must run exactly once, after every write including the buffer "
        "reset; saw %r" % seen_buffer
    )


def test_preserve_with_nothing_captured_still_resets_the_buffer(
    wiz, monkeypatch, tmp_path
):
    """An empty capture leaves the archive's device NAME alone rather than writing a
    guess - and still resets the buffer.

    A default name here would invent one nobody chose and publish it to the network,
    which is worse than keeping the value the restore brought. The buffer is the
    opposite case: its reset uses no captured value at all, so an empty capture must
    not suppress it. The function used to return early on an empty capture, which would
    now leave the SOURCE box's buffer on any box whose name could not be read."""
    gs, snapshots = _preserve_env(wiz, monkeypatch, tmp_path)

    prop = wiz._preserve_device_settings(lambda m: None, {})

    assert _setting_value(gs, "services.devicename") == "Golden Image Box", (
        "nothing was captured, so the archive's device name must stand"
    )
    assert _setting_value(gs, "filecache.memorysize") == "20", (
        "an empty capture suppressed the buffer reset - the archive's %r survived"
        % _setting_value(gs, "filecache.memorysize")
    )
    assert snapshots, (
        "the reset wrote the file, so it MUST be vectored: on tvOS an unvectored "
        "write is shadowed by the stale key and the Apple TV boots the old buffer"
    )
    assert prop == "filecache.memorysize:20", (
        "the ezm_preserved property must report what actually happened, so a hardware "
        "check can read the outcome over JSON-RPC instead of inferring it; got %r"
        % prop
    )


def test_preserve_never_raises_into_the_restore(wiz, monkeypatch, tmp_path):
    """A preserve failure must be reported, never thrown: it runs inside the restore
    pass, and a raise there would abort a restore over a cosmetic setting."""
    _prep_gs, _snap = _preserve_env(wiz, monkeypatch, tmp_path)
    from resources.lib.modules import _kodisettings

    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(_kodisettings, "write_guisetting", _boom)

    status = wiz._preserve_device_settings(
        lambda m: None, {"services.devicename": "Living Room"}
    )
    assert status.startswith("failed:"), status


def test_restore_arms_the_check_marker_and_arms_no_prompt(wiz, monkeypatch, tmp_path):
    """A finished restore arms the restore self-check marker and NOTHING else.

    The self-check marker stays: final certainty lives after the restart, where the
    restored settings are actually live, and it is SILENT on a clean pass. The
    tune-up marker that used to be armed beside it is gone - it existed only to make
    the boot service open a modal asking the user to repair the device name and cache
    buffer, both of which the restore now preserves."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    _record_restore_report(wiz, monkeypatch)
    armed = []
    monkeypatch.setattr(
        wiz.tools, "mark_restore_check_pending", lambda *a, **k: armed.append("check")
    )

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)

    assert armed == ["check"]
    # And the prompt marker cannot be re-armed from anywhere in wiz: the module must
    # not even reference it. Without this, a re-added call would only be caught by a
    # test that happens to stub the right name.
    src_text = (ADDON_ROOT / "resources" / "lib" / "modules" / "wiz.py").read_text(
        encoding="utf-8"
    )
    assert "buffer_prompt" not in src_text, (
        "wiz.py must not arm any post-restore prompt marker"
    )


def _record_window_props(wiz, monkeypatch):
    """Swap the fake Window for a recorder so a test can read Window(10000) properties
    (notably ezm_boot_skin). Returns the shared prop dict."""
    props = {}

    class _RecWindow:
        def __init__(self, *a, **k):
            pass

        def setProperty(self, key, value):
            props[key] = value

        def getProperty(self, key):
            return props.get(key, "")

        def clearProperty(self, key):
            props.pop(key, None)

    monkeypatch.setattr(wiz.xbmcgui, "Window", _RecWindow, raising=False)
    return props


def _no_skin_live_switch(monkeypatch, wiz):
    """Record every JSON-RPC + builtin so a test can prove _apply_boot_skin does NO live
    skin switch and answers NO keep-skin dialog. Returns (rpc_calls, builtins)."""
    rpc = []
    builtins = []

    def _jsonrpc(payload):
        rpc.append(payload)
        return "{}"

    monkeypatch.setattr(wiz.xbmc, "executeJSONRPC", _jsonrpc, raising=False)
    monkeypatch.setattr(
        wiz.xbmc, "executebuiltin", lambda cmd: builtins.append(cmd), raising=False
    )
    return rpc, builtins


def test_boot_skin_persists_restored_skin_to_disk_no_live_switch(
    wiz, monkeypatch, tmp_path
):
    """The restored skin is PERSISTED, never live-switched (atv2, 2026-07-17).

    _apply_boot_skin writes the captured skin straight into guisettings.xml on disk and
    does NOT live-set lookandfeel.skin or answer any keep-skin dialog - the flaky
    mechanism that reverted the box to stock is gone. That invariant is what this test
    pins, and it must not regress.

    IT DOES NOT MEAN THE REOPEN BOOTS THAT SKIN. This docstring used to claim "so a
    force-quit reopen boots it"; that is FALSE and was disproved on the bench
    2026-07-19 (defect A3). Kodi's clean shutdown serializes guisettings from LIVE
    memory over the file afterwards (Application.cpp:2131, log line "Saving settings"),
    so the disk write LOSES and a restore that CHANGES the skin reopens on the old one.
    A negative control (SIGKILL, no flush) kept the written value, isolating the cause
    to the flush.

    This test stops at the write and is structurally incapable of detecting A3 - do not
    read a pass here as evidence the skin survives. The outcome is only observable
    after the restart, which is why it is checked at boot instead (see
    test_boot_check_reports_a_wrong_skin in test_service_restore_check.py)."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    gp = home / "userdata" / "guisettings.xml"
    # Simulate the post-apply_guisettings state: the on-disk file carries STOCK.
    gp.write_text(
        '<settings><setting id="lookandfeel.skin">skin.estuary</setting></settings>'
    )
    props = _record_window_props(wiz, monkeypatch)
    rpc, builtins = _no_skin_live_switch(monkeypatch, wiz)

    persisted = []
    from resources.lib.modules import nsud

    # True is DELIBERATE here: this test models the fully successful path (it asserts
    # the "written:" verdict below), so a confirmed vector is exactly what is intended.
    monkeypatch.setattr(
        nsud, "persist_one", lambda rel, log=None: persisted.append(rel) or True
    )

    logs = []
    wiz._apply_boot_skin(lambda m: logs.append(m), "skin.estuary7")

    # (1) written straight to disk (write_guisetting), the last-step durable write.
    import xml.etree.ElementTree as ET

    root = ET.parse(str(gp)).getroot()
    got = next(
        n.text for n in root.iter("setting") if n.get("id") == "lookandfeel.skin"
    )
    assert got == "skin.estuary7", "the restored skin must be written to disk"
    # (2) vectored into NSUserDefaults via persist_one (no-op off tvOS).
    assert persisted == ["guisettings.xml"]
    # (3) NO live Settings.SetSettingValue for the skin, NO SendClick / keep-skin nav.
    assert not any("SetSettingValue" in p for p in rpc), "no live skin switch"
    assert not any("Settings." in p for p in rpc), "no live settings RPC at all"
    assert builtins == [], "no SendClick / navigation / keep-skin handling remains"
    # A readable diagnostic is published for JSON-RPC inspection.
    assert props.get("ezm_boot_skin") == "written:skin.estuary7"


def _fake_tvos(wiz, monkeypatch, on=True):
    """Make wiz._source_os() report 'tvos'. The base fixture answers every
    getCondVisibility with False, so without this a test named '...on_tvos' silently
    exercises the DESKTOP path - which is exactly how the unconfirmed-vector gap hid."""
    monkeypatch.setattr(
        wiz.xbmc,
        "getCondVisibility",
        lambda cond: bool(on) and cond == "System.Platform.TVOS",
    )


def _stub_nsud_layers(monkeypatch):
    """Neutralize the tvOS storage machinery so a test can isolate wiz's own verdict
    logic. nsud's real two-layer behavior is covered by the nsud/sandbox-io suites."""
    from resources.lib.modules import nsud

    monkeypatch.setattr(nsud, "purge_stale_keys", lambda *a, **k: (0, 0, 0, 0))
    monkeypatch.setattr(nsud, "rewrite_userdata_xml", lambda *a, **k: (0, 0, 0))
    return nsud


def test_boot_skin_vectors_via_persist_one_on_tvos(wiz, monkeypatch, tmp_path):
    """The tvOS durability path: the restored skin is vectored into NSUserDefaults via
    nsud.persist_one('guisettings.xml') - the same tvOS-safe primitive boxsetup uses.

    An UNCONFIRMED vector (persist_one -> False: the bytes are on disk but the read-back
    did not prove NSUserDefaults holds them) must be REPORTED, not discarded: the status
    becomes 'unconfirmed:<skin>' both as the return value and on the diagnostic window
    property. On tvOS a stale key shadows the disk file, so this is a partial restore."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    (home / "userdata" / "guisettings.xml").write_text(
        '<settings><setting id="lookandfeel.skin">skin.estuary</setting></settings>'
    )
    props = _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)
    _fake_tvos(wiz, monkeypatch)

    from resources.lib.modules import nsud

    calls = []
    monkeypatch.setattr(
        nsud, "persist_one", lambda rel, log=None: calls.append(rel) or False
    )

    status = wiz._apply_boot_skin(lambda m: None, "skin.estuary7")

    assert calls == ["guisettings.xml"], (
        "guisettings.xml must be vectored into NSUserDefaults (persist_one) on tvOS"
    )
    assert status == "unconfirmed:skin.estuary7", status
    # Item 4: the diagnostic property carries it too, for off-box JSON-RPC inspection.
    assert props.get("ezm_boot_skin") == "unconfirmed:skin.estuary7"
    # The skin is still on disk - False means "not durably vectored", never "data gone".
    import xml.etree.ElementTree as ET

    r = ET.parse(str(home / "userdata" / "guisettings.xml")).getroot()
    got = next(n.text for n in r.iter("setting") if n.get("id") == "lookandfeel.skin")
    assert got == "skin.estuary7"


def test_boot_skin_confirmed_vector_on_tvos_reports_written(wiz, monkeypatch, tmp_path):
    """The other side of the same branch: a CONFIRMED vector on tvOS still reports
    'written:', so the new check cannot cry wolf on a healthy Apple TV restore."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    (home / "userdata" / "guisettings.xml").write_text(
        '<settings><setting id="lookandfeel.skin">skin.estuary</setting></settings>'
    )
    props = _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)
    _fake_tvos(wiz, monkeypatch)

    from resources.lib.modules import nsud

    monkeypatch.setattr(nsud, "persist_one", lambda rel, log=None: True)

    assert wiz._apply_boot_skin(lambda m: None, "skin.estuary7") == (
        "written:skin.estuary7"
    )
    assert props.get("ezm_boot_skin") == "written:skin.estuary7"


def test_boot_skin_missing_or_empty_is_a_clean_noop(wiz, monkeypatch, tmp_path):
    """A missing / absent / empty lookandfeel.skin means there is no skin to assert:
    _read_target_skin returns None and _apply_boot_skin touches nothing, reporting 'none'."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    props = _record_window_props(wiz, monkeypatch)
    rpc, builtins = _no_skin_live_switch(monkeypatch, wiz)

    from resources.lib.modules import _kodisettings, nsud

    monkeypatch.setattr(
        _kodisettings,
        "write_guisetting",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not write on no-op")
        ),
    )
    monkeypatch.setattr(
        nsud,
        "persist_one",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not vector on no-op")
        ),
    )

    for empty in (None, "", "   "):
        wiz._apply_boot_skin(lambda m: None, empty)
    assert rpc == [] and builtins == [], "a no-op never touches Kodi"
    assert props.get("ezm_boot_skin") == "none"


def _skin_zip(tmp_path, skin="skin.estuary7"):
    src = tmp_path / "kodi_settings_skin.zip"
    return _make_valid_zip(
        src,
        [
            (
                "guisettings.xml",
                '<settings><setting id="lookandfeel.skin">'
                "%s</setting></settings>" % skin,
            )
        ],
    )


def test_boot_skin_unconfirmed_vector_reports_needs_attention(
    wiz, monkeypatch, tmp_path
):
    """THE fix: an unconfirmed tvOS vector is a PARTIAL restore and must never be
    reported as Complete.

    persist_one keeps the POSIX copy, so nothing is lost - but on tvOS a stale
    NSUserDefaults key SHADOWS the disk file, so the reopen this restore asks the user
    to perform comes back on the PREVIOUS skin. Saying "Restore Complete" there is a
    false success, and the locked contract forbids it: a partial restore is reported as
    partial. The finding must name the consequence in the user's terms."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    props = _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)
    _fake_tvos(wiz, monkeypatch)
    nsud = _stub_nsud_layers(monkeypatch)
    monkeypatch.setattr(nsud, "persist_one", lambda rel, log=None: False)

    res = wiz.restore(str(_skin_zip(tmp_path)), confirm=False)

    assert res["attention"], "an unconfirmed vector must produce a finding"
    assert any("previous skin" in a for a in res["attention"]), res["attention"]
    assert res["hard"] == [], (
        "the bytes are on disk; this is attention, not lost content"
    )
    assert oks == [wiz.AddonTitle + " " + wiz.MSG_NEEDS_ATTENTION], oks
    assert wiz.MSG_COMPLETE not in statuses, (
        "a partial restore may never claim Complete"
    )
    # Item 4: the diagnostic property is published IN ADDITION to the finding.
    assert props.get("ezm_boot_skin") == "unconfirmed:skin.estuary7"


def test_boot_skin_flaky_vector_self_heals_on_the_retry_without_warning(
    wiz, monkeypatch, tmp_path
):
    """THE ANTI-CRY-WOLF GUARANTEE. Ordering is load-bearing: attempt -> retry ->
    read-back -> only THEN decide the verdict.

    _apply_boot_skin runs INSIDE _restore_pass, so an unconfirmed vector is an
    attention-only finding, which triggers the existing SILENT auto-fix pass. Because
    _apply_boot_skin is idempotent, a one-off flaky read-back is re-attempted and
    confirms on pass 2 - and the user sees a clean Complete with no warning at all.
    A transient failure must never reach the user."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    props = _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)
    _fake_tvos(wiz, monkeypatch)
    nsud = _stub_nsud_layers(monkeypatch)

    attempts = {"n": 0}

    def _persist(rel, log=None):
        attempts["n"] += 1
        return attempts["n"] > 1  # fails once, then confirms

    monkeypatch.setattr(nsud, "persist_one", _persist)

    res = wiz.restore(str(_skin_zip(tmp_path)), confirm=False)

    assert attempts["n"] == 2, "the retry must give the vector a SECOND attempt"
    assert res["attention"] == [], "a self-healed vector must produce NO finding"
    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert oks == [] and yesnos == [], "a transient failure must never reach the user"
    assert props.get("ezm_boot_skin") == "written:skin.estuary7"


def test_boot_skin_unconfirmed_vector_off_tvos_is_never_a_finding(
    wiz, monkeypatch, tmp_path
):
    """The non-tvOS path must be completely unaffected. On Fire TV / Android / desktop
    there is no NSUserDefaults layer to shadow the file, so an unconfirmed vector is
    not this failure mode and must raise no warning and no new dialog."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    props = _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)
    _fake_tvos(wiz, monkeypatch, on=False)  # Fire TV / Android / desktop
    nsud = _stub_nsud_layers(monkeypatch)
    monkeypatch.setattr(nsud, "persist_one", lambda rel, log=None: False)

    res = wiz.restore(str(_skin_zip(tmp_path)), confirm=False)

    assert res["attention"] == [], "no tvOS shadowing layer -> no finding"
    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert oks == [] and yesnos == []
    assert props.get("ezm_boot_skin") == "written:skin.estuary7"


def test_read_target_skin_captures_absent_and_present(wiz, tmp_path):
    """_read_target_skin: the archive's skin when present, None when the file is missing,
    unparseable, or the setting is absent / empty."""
    present = tmp_path / "present.xml"
    present.write_text(
        '<settings><setting id="lookandfeel.skin">skin.estuary7</setting></settings>'
    )
    assert wiz._read_target_skin(str(present)) == "skin.estuary7"

    absent_setting = tmp_path / "absent.xml"
    absent_setting.write_text('<settings><setting id="other">x</setting></settings>')
    assert wiz._read_target_skin(str(absent_setting)) is None

    empty_setting = tmp_path / "empty.xml"
    empty_setting.write_text(
        '<settings><setting id="lookandfeel.skin">   </setting></settings>'
    )
    assert wiz._read_target_skin(str(empty_setting)) is None

    assert wiz._read_target_skin(str(tmp_path / "does-not-exist.xml")) is None


def test_boot_skin_failure_never_breaks_the_restore(wiz, monkeypatch, tmp_path):
    """Fully guarded: a raising write_guisetting only logs and records failed:<Error>."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    (home / "userdata" / "guisettings.xml").write_text(
        '<settings><setting id="lookandfeel.skin">skin.estuary</setting></settings>'
    )
    props = _record_window_props(wiz, monkeypatch)

    from resources.lib.modules import _kodisettings

    def _boom(*a, **k):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(_kodisettings, "write_guisetting", _boom)

    logs = []
    wiz._apply_boot_skin(lambda m: logs.append(m), "skin.estuary7")  # must not raise
    assert any("boot-skin" in m for m in logs)
    assert props.get("ezm_boot_skin", "").startswith("failed:")


def test_restore_captures_skin_before_apply_and_writes_it_back_last(
    wiz, monkeypatch, tmp_path
):
    """End-to-end: restore() captures the archive's skin BEFORE apply_guisettings can
    rewrite the file, then persists it as the LAST userdata write (after apply, purge,
    and the tvOS re-vector) - never a live switch."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    _record_window_props(wiz, monkeypatch)
    _no_skin_live_switch(monkeypatch, wiz)

    from resources.lib.modules import _kodisettings, nsud

    order = []
    # apply_guisettings simulates real Kodi stamping STOCK over the archive's skin on disk.
    gp = home / "userdata" / "guisettings.xml"

    def _apply(path):
        order.append("apply")
        try:
            import xml.etree.ElementTree as ET

            tree = ET.parse(str(gp))
            r = tree.getroot()
            for n in r.iter("setting"):
                if n.get("id") == "lookandfeel.skin":
                    n.text = "skin.estuary"  # the clobber the fix must survive
            tree.write(str(gp))
        except Exception:
            pass
        return 0

    monkeypatch.setattr(_kodisettings, "apply_guisettings", _apply)
    monkeypatch.setattr(
        nsud,
        "purge_stale_keys",
        lambda *a, **k: order.append("purge") or (0, 0, 0, 0),
    )
    monkeypatch.setattr(
        nsud,
        "rewrite_userdata_xml",
        lambda *a, **k: order.append("rewrite") or (0, 0, 0),
    )
    wg = []
    real_wg = _kodisettings.write_guisetting

    def _wg(path, sid, val):
        order.append("write_guisetting")
        wg.append((sid, val))
        return real_wg(path, sid, val)

    monkeypatch.setattr(_kodisettings, "write_guisetting", _wg)
    # True is DELIBERATE here: this is the clean end-to-end restore (it ends on
    # MSG_COMPLETE with the restored skin on disk), so the vector really did confirm.
    monkeypatch.setattr(
        nsud,
        "persist_one",
        lambda rel, log=None: order.append("persist_one") or True,
    )

    src = tmp_path / "kodi_settings_skin.zip"
    _make_valid_zip(
        src,
        [
            (
                "guisettings.xml",
                '<settings><setting id="lookandfeel.skin">'
                "skin.estuary7</setting></settings>",
            )
        ],
    )
    wiz.restore(str(src), confirm=False)

    # The restored skin was captured (skin.estuary7) despite apply stamping stock, and
    # written back via write_guisetting.
    assert ("lookandfeel.skin", "skin.estuary7") in wg
    # It is the LAST userdata write: write_guisetting + persist_one come AFTER apply,
    # purge, and the re-vector.
    assert order.index("write_guisetting") > order.index("apply")
    assert order.index("write_guisetting") > order.index("rewrite")
    assert order.index("persist_one") > order.index("rewrite")
    # And the file on disk ends on the restored skin, not the stock clobber.
    import xml.etree.ElementTree as ET

    r = ET.parse(str(gp)).getroot()
    got = next(n.text for n in r.iter("setting") if n.get("id") == "lookandfeel.skin")
    assert got == "skin.estuary7"


def test_no_live_skin_switch_mechanism_remains_in_sources(wiz):
    """The flaky live-switch-and-confirm is fully removed from wiz.py: no SendClick, no
    keep-skin dialog handling, and no live SetSettingValue for lookandfeel.skin."""
    src = (ADDON_ROOT / "resources" / "lib" / "modules" / "wiz.py").read_text()
    # The live-switch mechanism's actual code constructs must all be gone (the docstring
    # may still name SendClick to explain WHY - so match the call form, not the bare word).
    assert "SendClick(11)" not in src, "the keep-skin SendClick confirm must be gone"
    assert "IsActive(yesnodialog)" not in src, "the keep-skin dialog probe must be gone"
    assert "Action(Select)" not in src, "the keep-skin nav confirm must be gone"
    # No live skin switch: lookandfeel.skin is never handed to Settings.SetSettingValue.
    assert '"setting": "lookandfeel.skin"' not in src


def test_no_mid_flight_wipe_warning_dialog_remains(wiz):
    """The raw wipe warning is gone from the sources: leftovers are triaged by
    the verification, never surfaced as a fear."""
    ADDON = ADDON_ROOT / "resources" / "lib" / "modules"
    combined = (ADDON / "onetap.py").read_text() + (ADDON / "wiz.py").read_text()
    # The two retired user-facing strings, by their distinctive tails (comments
    # may still DESCRIBE the old behavior; the dialogs may not SHOW it).
    assert "The restore will proceed." not in combined
    assert "shadow or pollute the restored state" not in combined


def test_merge_cancel_after_completed_pass_still_arms(wiz, monkeypatch, tmp_path):
    """Finding 4: merge restore, pass 1 completes with a fixable finding, the silent
    auto-fix retry's extract is canceled. The box WAS restored by pass 1, so the
    tune-up + restore-check markers and the boot skin must still arm - and the pass's
    own 'Restore Canceled' dialog is the only message (never a stray Complete)."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)

    # Extract: pass 1 lands clean, pass 2 (the auto-fix retry) is canceled.
    calls = {"n": 0}

    def _extract(_in, _out, progress, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return wiz.ExtractResult(canceled=False, extracted=1, total=1)
        return wiz.ExtractResult(canceled=True, extracted=-1)

    monkeypatch.setattr(wiz, "ExtractWithProgress", _extract)

    # Force a fixable (attention-only) finding on pass 1 so a silent retry runs.
    from resources.lib.modules import restorecheck

    vcalls = {"n": 0}

    def _verify(*a, **k):
        vcalls["n"] += 1
        return (
            (["1 stale key still shadows something"], [])
            if vcalls["n"] == 1
            else ([], [])
        )

    monkeypatch.setattr(restorecheck, "verify_restored_state", _verify)

    armed = []
    monkeypatch.setattr(
        wiz.tools, "mark_restore_check_pending", lambda *a, **k: armed.append("check")
    )
    skinned = []
    monkeypatch.setattr(
        wiz, "_apply_boot_skin", lambda rlog, target=None: skinned.append(True)
    )

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])
    result = wiz.restore(str(src), confirm=False)  # merge (no wipe/post_wipe)

    assert calls["n"] == 2, "the auto-fix retry ran"
    assert armed == ["check"], (
        "a restored box arms its restore-check marker even on a canceled retry"
    )
    assert skinned == [True], "the boot skin still applies"
    assert not any(s == wiz.MSG_COMPLETE for s in statuses), (
        "a canceled retry never claims Complete"
    )
    assert result.get("canceled") is True
    # The pass's own 'Restore Canceled' dialog fired inside the pass; no extra Problem dialog.
    assert not any(wiz.MSG_NEEDS_ATTENTION in o for o in oks)


def test_failed_member_is_named_in_the_log(wiz, monkeypatch, tmp_path):
    """The 'named, in the log' half of the honesty contract: a member that fails to
    extract must be logged by name even though the UI shows only the locked Problem."""
    home = _prep_restore(wiz, monkeypatch, tmp_path)
    _record_restore_report(wiz, monkeypatch, retry=False)
    logs = []
    monkeypatch.setattr(wiz.xbmc, "log", lambda msg, level=0: logs.append(msg))
    (home / "userdata" / "blocked.xml").mkdir(parents=True)  # extract target is a dir

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("blocked.xml", "<b/>")])
    wiz.restore(str(src), confirm=False)

    assert any("failed member" in m and "blocked.xml" in m for m in logs), (
        "the failed member must be named in the log"
    )


def test_revector_miss_on_merge_path_is_attention_not_silent(
    wiz, monkeypatch, tmp_path
):
    """audit Finding A/B: on the MERGE path (add-on-top / Dropbox) a tvOS re-vector
    miss can leave a SURVIVING stale key shadowing the restored file - a silent loss.
    It MUST surface as needs-attention, never a silent 'Restore Complete'."""
    from resources.lib.modules import nsud

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    monkeypatch.setattr(nsud, "purge_stale_keys", lambda root, log=None: (0, 0, 0, 0))
    monkeypatch.setattr(
        nsud, "rewrite_userdata_xml", lambda root, log=None: (0, 0, 2, 0)
    )  # 2 files did not re-vector

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("sources.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)  # MERGE: wipe=False

    assert oks == [wiz.AddonTitle + " " + wiz.MSG_NEEDS_ATTENTION], oks
    assert not any(s == wiz.MSG_COMPLETE for s in statuses)


def test_revector_miss_on_wipe_path_is_harmless_complete(wiz, monkeypatch, tmp_path):
    """On the WIPE path the wipe already cleared every NSUD key, so a re-vector miss
    leaves the restored POSIX file served with NO shadow - harmless. It must NOT
    alarm; the restore says Complete (this is exactly the atv2 clean-restore case)."""
    from resources.lib.modules import nsud

    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    monkeypatch.setattr(nsud, "purge_stale_keys", lambda root, log=None: (0, 0, 0, 0))
    monkeypatch.setattr(
        nsud, "rewrite_userdata_xml", lambda root, log=None: (0, 0, 2, 0)
    )

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("sources.xml", "<s/>")])
    wiz.restore(str(src), confirm=False, post_wipe=True)  # WIPE path

    assert statuses == [wiz.MSG_COMPLETE], statuses
    assert oks == [] and yesnos == []


# --------------------------------------------------------------------------- #
# T-H3: the restore-scoped PVR/IPTV pause.
#
# The backup/restore contract names this the ONLY sanctioned add-on toggle in the
# whole add-on, and its failure mode is severe: a restore that disables the user's
# IPTV client and never re-enables it leaves live TV dead with no visible cause.
# The contract says the client is disabled ONLY when the archive carries IPTV
# config AND the client is live, and is ALWAYS re-enabled afterward - cancel path
# included - with a loud report when the re-enable does not take.
#
# These tests patch _pvr_enabled/_pvr_set_enabled directly rather than steering the
# JSON-RPC fake: the fixture's executeJSONRPC returns "{}", so _pvr_enabled() is
# False for every other test in this file and the entire pause block was dead code
# under test until now.
# --------------------------------------------------------------------------- #

_IPTV_MEMBER = "addon_data/pvr.iptvsimple/instance-settings-1.xml"


def _iptv_zip(tmp_path, name="kodi_settings_202607180101.zip"):
    """A userdata-anchored archive that CARRIES pvr.iptvsimple config (so the
    restore-scoped pause is in scope) plus an ordinary settings file."""
    src = tmp_path / name
    return _make_valid_zip(
        src,
        [
            ("guisettings.xml", "<settings/>"),
            (_IPTV_MEMBER, "<instance/>"),
        ],
    )


def _pvr_recorder(wiz, monkeypatch, enabled=True, reenable_ok=True):
    """Patch the PVR probe/toggle seam. Returns the recorded (flag, ...) calls."""
    calls = []

    def _set(flag):
        calls.append(bool(flag))
        return reenable_ok if flag else True

    monkeypatch.setattr(wiz, "_pvr_enabled", lambda: enabled)
    monkeypatch.setattr(wiz, "_pvr_set_enabled", _set)
    return calls


def _pvr_marker_recorder(wiz, monkeypatch):
    """Patch the crash-recovery marker seam (tools.mark/clear_pvr_pause_marker).
    wiz imports tools at module top AND lazily inside the pause block; both resolve
    to the same module object, so one patch covers both call sites."""
    marks = []
    monkeypatch.setattr(
        wiz.tools, "mark_pvr_paused", lambda *a, **k: marks.append("mark")
    )
    monkeypatch.setattr(
        wiz.tools, "clear_pvr_pause_marker", lambda *a, **k: marks.append("clear")
    )
    return marks


def test_pvr_pause_disables_before_extract_and_reenables_after_rewrite(
    wiz, monkeypatch, tmp_path
):
    """The contract's ordering, asserted as ORDER and not just as call counts: the
    client is disabled BEFORE the extract (so it cannot flush stale in-memory
    instance settings over the restored files) and re-enabled only AFTER the tvOS
    durability rewrite has vectored those files."""
    from resources.lib.modules import nsud

    _prep_restore(wiz, monkeypatch, tmp_path)
    events = []

    def _set(flag):
        events.append("disable" if not flag else "enable")
        return True

    monkeypatch.setattr(wiz, "_pvr_enabled", lambda: True)
    monkeypatch.setattr(wiz, "_pvr_set_enabled", _set)
    _pvr_marker_recorder(wiz, monkeypatch)

    real_extract = wiz.ExtractWithProgress

    def _extract(*a, **k):
        events.append("extract")
        return real_extract(*a, **k)

    monkeypatch.setattr(wiz, "ExtractWithProgress", _extract)

    real_rewrite = nsud.rewrite_userdata_xml

    def _rewrite(*a, **k):
        events.append("rewrite")
        return real_rewrite(*a, **k)

    monkeypatch.setattr(nsud, "rewrite_userdata_xml", _rewrite)

    wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert events == ["disable", "extract", "rewrite", "enable"], events


def test_pvr_pause_marks_recovery_marker_before_extract_and_clears_on_resume(
    wiz, monkeypatch, tmp_path
):
    """The pause is recorded BEFORE the extract so a crash/power loss mid-restore
    leaves a marker the boot service can act on, and the marker is cleared only once
    the client is confirmed re-enabled."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    toggles = _pvr_recorder(wiz, monkeypatch)

    order = []
    monkeypatch.setattr(
        wiz.tools, "mark_pvr_paused", lambda *a, **k: order.append("mark")
    )
    monkeypatch.setattr(
        wiz.tools, "clear_pvr_pause_marker", lambda *a, **k: order.append("clear")
    )
    real_extract = wiz.ExtractWithProgress

    def _extract(*a, **k):
        order.append("extract")
        return real_extract(*a, **k)

    monkeypatch.setattr(wiz, "ExtractWithProgress", _extract)

    res = wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert toggles == [False, True], toggles
    assert order == ["mark", "extract", "clear"], order
    assert res["attention"] == [], res


def test_pvr_pause_not_attempted_when_archive_has_no_iptv(wiz, monkeypatch, tmp_path):
    """The toggle is bounded by the archive: no pvr.iptvsimple config in the zip means
    the client is never touched, even when it is live (the contract's 'restore never
    toggles add-ons' default)."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    toggles = _pvr_recorder(wiz, monkeypatch)
    marks = _pvr_marker_recorder(wiz, monkeypatch)

    src = tmp_path / "kodi_settings_noiptv.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>"), ("sources.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)

    assert toggles == [], "no IPTV in the archive -> no toggle at all"
    assert marks == []


def test_pvr_pause_not_attempted_when_client_already_disabled(
    wiz, monkeypatch, tmp_path
):
    """The pause protects a LIVE client's teardown flush. An already-disabled (or
    not-installed) client must never be touched - a restore may not ENABLE anything."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    toggles = _pvr_recorder(wiz, monkeypatch, enabled=False)
    marks = _pvr_marker_recorder(wiz, monkeypatch)

    wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert toggles == [], "a disabled client must be left disabled"
    assert marks == []


def test_pvr_pause_failure_to_disable_is_log_only_not_a_finding(
    wiz, monkeypatch, tmp_path
):
    """A pause MISS is a risk, not a realized failure: the restore must not be
    downgraded on it (that is what cried wolf on clean restores), and nothing is
    re-enabled afterward because nothing was paused."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    calls = []

    monkeypatch.setattr(wiz, "_pvr_enabled", lambda: True)
    monkeypatch.setattr(
        wiz, "_pvr_set_enabled", lambda flag: calls.append(bool(flag)) or False
    )
    marks = _pvr_marker_recorder(wiz, monkeypatch)

    res = wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert calls == [False], "one failed disable attempt, and no bogus re-enable"
    assert marks == [], "nothing was paused, so no crash-recovery marker is armed"
    assert res["attention"] == [] and res["hard"] == [], res


def test_pvr_reenable_failure_is_loud_and_keeps_the_recovery_marker(
    wiz, monkeypatch, tmp_path
):
    """The severe case: the client WAS disabled and cannot be turned back on. It must
    surface as a needs-attention finding (never a silent Complete) and the crash
    recovery marker must NOT be cleared, so the boot service still re-enables it."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    toggles = _pvr_recorder(wiz, monkeypatch, reenable_ok=False)
    marks = _pvr_marker_recorder(wiz, monkeypatch)

    res = wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert any("could not be re-enabled" in a for a in res["attention"]), res
    assert res["hard"] == [], "a failed re-enable is attention, not lost content"
    # Attention-only findings trigger one silent auto-fix pass, so the disable/enable
    # pair runs twice; what matters is that EVERY disable is followed by a re-enable
    # attempt and that the marker is never cleared on a failed one.
    assert toggles and toggles == [False, True] * (len(toggles) // 2), toggles
    assert "clear" not in marks, "the marker must survive a failed re-enable"
    assert marks.count("mark") == len(toggles) // 2
    assert oks == [wiz.AddonTitle + " " + wiz.MSG_NEEDS_ATTENTION], oks


def test_pvr_pause_cancel_path_still_reenables(wiz, monkeypatch, tmp_path):
    """ALWAYS re-enable means the cancel path too. A user who aborts a merge restore
    mid-extract must not be left with a dead IPTV client."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    toggles = _pvr_recorder(wiz, monkeypatch)
    marks = _pvr_marker_recorder(wiz, monkeypatch)
    monkeypatch.setattr(
        wiz,
        "ExtractWithProgress",
        lambda *a, **k: wiz.ExtractResult(canceled=True, extracted=1, total=2),
    )

    res = wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert res.get("canceled") is True, res
    assert toggles == [False, True], "a cancel must still resume the client"
    assert marks == ["mark", "clear"]


def test_pvr_cancel_path_reenable_failure_tells_the_user(wiz, monkeypatch, tmp_path):
    """On the cancel path there is no findings report to ride on, so a failed
    re-enable has to speak for itself in a dialog."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    statuses, oks, yesnos = _record_restore_report(wiz, monkeypatch)
    toggles = _pvr_recorder(wiz, monkeypatch, reenable_ok=False)
    marks = _pvr_marker_recorder(wiz, monkeypatch)
    monkeypatch.setattr(
        wiz,
        "ExtractWithProgress",
        lambda *a, **k: wiz.ExtractResult(canceled=True, extracted=1, total=2),
    )

    wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert toggles == [False, True]
    assert "clear" not in marks
    assert any("IPTV client could not be re-enabled" in m for m in oks), oks


def test_pvr_pause_survives_a_broken_marker_module(wiz, monkeypatch, tmp_path):
    """The crash-recovery marker is best-effort: a raising mark/clear must never
    abort the restore or strand the client disabled."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    toggles = _pvr_recorder(wiz, monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("marker store unavailable")

    monkeypatch.setattr(wiz.tools, "mark_pvr_paused", _boom)
    monkeypatch.setattr(wiz.tools, "clear_pvr_pause_marker", _boom)

    res = wiz.restore(str(_iptv_zip(tmp_path)), confirm=False)

    assert toggles == [False, True]
    assert res["hard"] == [] and res["attention"] == [], res


def test_pvr_probe_helpers_read_the_real_jsonrpc_shapes(wiz, monkeypatch):
    """The probe/toggle helpers themselves, against Kodi's real response shapes.
    _pvr_enabled must fail toward 'no pause needed' on any malformed answer, and
    _pvr_set_enabled must treat anything but Kodi's "OK" as a failure."""
    answers = {}
    monkeypatch.setattr(
        wiz.xbmc, "executeJSONRPC", lambda payload: answers.get("body", "{}")
    )

    answers["body"] = '{"result":{"addon":{"enabled":true}}}'
    assert wiz._pvr_enabled() is True
    answers["body"] = '{"result":{"addon":{"enabled":false}}}'
    assert wiz._pvr_enabled() is False
    # Not installed: Kodi answers with an error object and no result.
    answers["body"] = '{"error":{"code":-32602,"message":"Invalid params."}}'
    assert wiz._pvr_enabled() is False
    answers["body"] = "not json at all"
    assert wiz._pvr_enabled() is False

    answers["body"] = '{"result":"OK"}'
    assert wiz._pvr_set_enabled(True) is True
    answers["body"] = '{"result":"Something else"}'
    assert wiz._pvr_set_enabled(False) is False
    answers["body"] = '{"error":{"code":-32100}}'
    assert wiz._pvr_set_enabled(True) is False


# --------------------------------------------------------------------------- #
# T-H1: keep-N rotation, run for REAL.
#
# Every pre-existing backup test monkeypatches _rotate_vfs/_rotate_dropbox to a
# no-op, so the assertions about rotation were negative space: they proved a stub
# was not called. Rotation DELETES the user's backups, and _is_rolling is the only
# thing standing between keep-N and a user's renamed golden backup, so the real
# functions are driven here against fake VFS/Dropbox listings.
# --------------------------------------------------------------------------- #


def _rotation_env(wiz, monkeypatch, keep, files):
    """Point _keep_n at `keep`, make xbmcvfs.listdir return `files`, and record every
    xbmcvfs.delete. Returns the recorded delete targets (basenames)."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: str(keep) if key == "backup.keep" else ""
    )
    monkeypatch.setattr(wiz.xbmcvfs, "listdir", lambda p: ([], list(files)))
    deleted = []
    monkeypatch.setattr(
        wiz.xbmcvfs, "delete", lambda p: deleted.append(Path(p).name) or True
    )
    return deleted


_ROLLING = [
    "kodi_backup_202601010101.zip",
    "kodi_backup_202602010101.zip",
    "kodi_backup_202603010101.zip",
    "kodi_backup_202604010101.zip",
]
_PROTECTED = [
    "golden_keep_202601010101.zip",  # stamped, but carries a keep token
    "my_golden_backup.zip",  # user-renamed: no stamp at all
    "KEEP_this_one_202512310000.zip",  # keep token, any case
    "notes.txt",  # not a zip
]


def test_is_rolling_only_matches_the_tools_own_naming_contract(wiz):
    """The single predicate that decides whether a file may be deleted. Every
    unrecognized shape must be PROTECTED - that failure direction is the contract."""
    for rolling in _ROLLING:
        assert wiz._is_rolling(rolling) is True, rolling
    for protected in _PROTECTED + ["", None, "kodi_backup_2026.zip"]:
        assert wiz._is_rolling(protected) is False, protected
    # A stamp that is not TRAILING is not the tool's contract either.
    assert wiz._is_rolling("kodi_backup_202601010101.zip.bak") is False
    assert wiz._is_rolling("kodi_backup_2026010101011.zip") is False  # 13 digits


def test_name_stamp_is_lexically_sortable_or_empty(wiz):
    assert wiz._name_stamp("kodi_backup_202601020304.zip") == "202601020304"
    assert wiz._name_stamp("golden.zip") == ""
    assert wiz._name_stamp(None) == ""
    stamps = [wiz._name_stamp(n) for n in _ROLLING]
    assert stamps == sorted(stamps), "lexical order must equal chronological order"


def test_keep_n_defaults_to_zero_on_anything_unparseable(wiz, monkeypatch):
    """keep-N is the OFF switch: anything the setting cannot parse must mean 0
    (never rotate), not an accidental prune."""
    for raw, want in (("3", 3), ("", 0), (None, 0), ("abc", 0), ("2.5", 0)):
        monkeypatch.setattr(wiz.control, "setting", lambda key, r=raw: r)
        assert wiz._keep_n() == want, raw


def test_rotate_vfs_deletes_only_the_oldest_rolling_past_keep_n(
    wiz, monkeypatch, tmp_path
):
    """The REAL _rotate_vfs against a fake listing: four rolling backups plus
    protected/renamed files, keep=2. Only the two oldest ROLLING files may go."""
    deleted = _rotation_env(wiz, monkeypatch, 2, _ROLLING + _PROTECTED)

    wiz._rotate_vfs("/backups")

    assert sorted(deleted) == sorted(_ROLLING[:2]), deleted
    for survivor in _PROTECTED + _ROLLING[2:]:
        assert survivor not in deleted, survivor


def test_rotate_vfs_never_counts_protected_files_toward_keep_n(wiz, monkeypatch):
    """Protected files must not consume keep-N slots. With keep=4 and four rolling
    backups, the four protected files must not push any rolling backup out."""
    deleted = _rotation_env(wiz, monkeypatch, 4, _ROLLING + _PROTECTED)
    wiz._rotate_vfs("/backups")
    assert deleted == [], deleted


def test_rotate_vfs_protects_the_backup_just_written(wiz, monkeypatch):
    """`protect` is how the caller keeps the run's own fresh backup: with keep=1 the
    protected name is neither deleted nor counted, so one older rolling copy stays."""
    fresh = "kodi_backup_202605010101.zip"
    deleted = _rotation_env(wiz, monkeypatch, 1, _ROLLING + [fresh])
    wiz._rotate_vfs("/backups", protect={fresh})
    assert fresh not in deleted
    assert sorted(deleted) == sorted(_ROLLING[:3]), deleted


def test_rotate_vfs_keep_zero_is_a_hard_off_switch(wiz, monkeypatch):
    deleted = _rotation_env(wiz, monkeypatch, 0, _ROLLING)
    wiz._rotate_vfs("/backups")
    assert deleted == []
    deleted_neg = _rotation_env(wiz, monkeypatch, -1, _ROLLING)
    wiz._rotate_vfs("/backups")
    assert deleted_neg == []


def test_rotate_vfs_swallows_an_unreadable_backup_dir(wiz, monkeypatch):
    """An offline NFS/SMB share must degrade to 'rotation skipped', never raise into
    the backup that just succeeded."""
    deleted = _rotation_env(wiz, monkeypatch, 1, [])

    def _boom(p):
        raise OSError("share is offline")

    monkeypatch.setattr(wiz.xbmcvfs, "listdir", _boom)
    wiz._rotate_vfs("/backups")  # must not raise
    assert deleted == []


def test_rotate_vfs_one_failed_delete_does_not_stop_the_rest(wiz, monkeypatch):
    """A single locked/absent file must not abort the prune of the others."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "1" if key == "backup.keep" else ""
    )
    monkeypatch.setattr(wiz.xbmcvfs, "listdir", lambda p: ([], list(_ROLLING)))
    deleted = []

    def _delete(p):
        name = Path(p).name
        if name == _ROLLING[0]:
            raise OSError("file is locked")
        deleted.append(name)
        return True

    monkeypatch.setattr(wiz.xbmcvfs, "delete", _delete)
    wiz._rotate_vfs("/backups")
    assert sorted(deleted) == sorted(_ROLLING[1:3]), deleted


class _FakeDropboxRemote:
    """The wiz-side seam of the Dropbox transport: only what wiz.py actually calls.
    The transport itself is covered by tests/test_dropbox_remote.py; this fake exists
    so wiz's own flow can be driven without importing requests or touching a network."""

    DropboxCanceled = type("DropboxCanceled", (Exception,), {})

    def __init__(self, names=None, list_error=None, upload_error=None):
        self.names = list(names or [])
        self.list_error = list_error
        self.upload_error = upload_error
        self.uploaded = []
        self.deleted = []
        self.downloaded = []
        self.delete_error_for = set()
        self.download_result = None
        self.download_error = None

    def list_backups(self):
        if self.list_error:
            raise self.list_error
        return list(self.names)

    def delete(self, name):
        if name in self.delete_error_for:
            raise OSError("dropbox delete failed for %s" % name)
        self.deleted.append(name)

    def upload(self, local, remote_name, progress=None):
        if self.upload_error:
            raise self.upload_error
        self.uploaded.append(remote_name)

    def download(self, name, progress=None):
        if self.download_error:
            raise self.download_error
        self.downloaded.append(name)
        return self.download_result


def test_rotate_dropbox_deletes_only_rolling_past_keep_n(wiz, monkeypatch):
    """The REAL _rotate_dropbox against a fake remote. list_backups is newest-first,
    so keep-N keeps the head and prunes the ROLLING tail only."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "2" if key == "backup.keep" else ""
    )
    newest_first = list(reversed(_ROLLING)) + _PROTECTED
    remote = _FakeDropboxRemote(names=newest_first)

    wiz._rotate_dropbox(remote)

    assert sorted(remote.deleted) == sorted(_ROLLING[:2]), remote.deleted
    for survivor in _PROTECTED:
        assert survivor not in remote.deleted, survivor


def test_rotate_dropbox_protects_the_upload_just_confirmed(wiz, monkeypatch):
    fresh = "kodi_backup_202605010101.zip"
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "1" if key == "backup.keep" else ""
    )
    remote = _FakeDropboxRemote(names=[fresh] + list(reversed(_ROLLING)))

    wiz._rotate_dropbox(remote, protect={fresh})

    assert fresh not in remote.deleted
    assert sorted(remote.deleted) == sorted(_ROLLING[:3]), remote.deleted


def test_rotate_dropbox_keep_zero_is_a_hard_off_switch(wiz, monkeypatch):
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "0" if key == "backup.keep" else ""
    )
    remote = _FakeDropboxRemote(names=list(reversed(_ROLLING)))
    wiz._rotate_dropbox(remote)
    assert remote.deleted == []


def test_rotate_dropbox_swallows_a_raising_list_backups(wiz, monkeypatch):
    """A Dropbox API/network failure while listing must degrade to 'rotation skipped'
    and never raise into the backup that already landed."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "1" if key == "backup.keep" else ""
    )
    remote = _FakeDropboxRemote(
        names=list(reversed(_ROLLING)), list_error=RuntimeError("429 rate limited")
    )
    wiz._rotate_dropbox(remote)  # must not raise
    assert remote.deleted == []


def test_rotate_dropbox_one_failed_delete_does_not_stop_the_rest(wiz, monkeypatch):
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "1" if key == "backup.keep" else ""
    )
    remote = _FakeDropboxRemote(names=list(reversed(_ROLLING)))
    remote.delete_error_for = {_ROLLING[0]}
    wiz._rotate_dropbox(remote)
    assert sorted(remote.deleted) == sorted(_ROLLING[1:3]), remote.deleted


# --------------------------------------------------------------------------- #
# T-H2: wiz's own Dropbox backup/restore flow (NOT the transport - that lives in
# tests/test_dropbox_remote.py). The rule under test is the same one the local
# path already enforces: rotation runs ONLY after a confirmed-landed, COMPLETE
# backup, so a failed or canceled run can never prune the last good backup.
# --------------------------------------------------------------------------- #


def _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote, name="mybackup"):
    """Wire _backup_dropbox's seams: a signed-in token, a keyboard name, an accepted
    confirm, a CreateZip that really stages a file in special://temp, and a recorded
    _rotate_dropbox. Returns (calls, staged_paths)."""
    monkeypatch.setattr(resources_modules(wiz), "dropbox_remote", remote, raising=False)
    monkeypatch.setattr(
        wiz.control,
        "setting",
        lambda key: "tok" if key == "dropbox_refresh_token" else "",
    )
    monkeypatch.setattr(wiz.tools, "_get_keyboard", lambda **k: name)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True)
    for fn in ("clearCache", "deleteThumbnails", "purgePackages"):
        monkeypatch.setattr(wiz.maintenance, fn, lambda **k: None)
    calls = []
    monkeypatch.setattr(wiz, "_rotate_dropbox", lambda *a, **k: calls.append("rotate"))
    staged = []

    def _create_zip(src, dst, *a, **k):
        staged.append(dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"PK\x03\x04staged")
        return wiz.ZipResult(entries=2)

    monkeypatch.setattr(wiz, "CreateZip", _create_zip)
    return calls, staged


def resources_modules(wiz):
    """The `resources.lib.modules` package object - the binding site the lazy
    `from resources.lib.modules import dropbox_remote` inside wiz resolves against."""
    return importlib.import_module("resources.lib.modules")


def test_backup_dropbox_happy_path_uploads_rotates_and_cleans_the_stage(
    wiz, monkeypatch, tmp_path
):
    """A complete backup that lands: the staged local zip is uploaded, rotation runs
    with the new name PROTECTED, and the temp stage is always removed."""
    remote = _FakeDropboxRemote()
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)
    protects = []
    monkeypatch.setattr(
        wiz,
        "_rotate_dropbox",
        lambda dr, protect=None: (
            calls.append("rotate"),
            protects.append(set(protect or ())),
        ),
    )

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert len(remote.uploaded) == 1, remote.uploaded
    uploaded = remote.uploaded[0]
    assert uploaded.startswith("mybackup_") and uploaded.endswith(".zip")
    assert calls == ["rotate"], "a landed, complete backup rotates"
    assert protects == [{uploaded}], "the fresh backup must be protected from itself"
    assert staged and not Path(staged[0]).exists(), "the temp stage must be removed"


def test_backup_dropbox_confirm_declined_builds_nothing(wiz, monkeypatch, tmp_path):
    """Declining the final-name confirm must abort BEFORE the zip build - no zip, no
    upload, no rotation (the behavioral replacement for the old source-string test)."""
    remote = _FakeDropboxRemote()
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: False)

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert staged == [], "the confirm must gate the zip build"
    assert remote.uploaded == [] and calls == []


def test_backup_dropbox_without_a_token_does_nothing(wiz, monkeypatch, tmp_path):
    remote = _FakeDropboxRemote()
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)
    monkeypatch.setattr(wiz.control, "setting", lambda key: "  ")

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert staged == [] and remote.uploaded == [] and calls == []


def test_backup_dropbox_canceled_zip_never_uploads_or_rotates(
    wiz, monkeypatch, tmp_path
):
    remote = _FakeDropboxRemote()
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)

    def _canceled(src, dst, *a, **k):
        staged.append(dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"partial")
        return wiz.ZipResult(canceled=True)

    monkeypatch.setattr(wiz, "CreateZip", _canceled)

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert remote.uploaded == [] and calls == []
    assert not Path(staged[0]).exists(), "a canceled run still cleans its stage"


def test_backup_dropbox_tvos_capture_failure_never_uploads_or_rotates(
    wiz, monkeypatch, tmp_path
):
    """A BackupCaptureError means the zip would silently miss the owner's settings:
    nothing is uploaded and the previous remote backup must not be pruned."""
    remote = _FakeDropboxRemote()
    calls, _staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)

    def _raise(src, dst, *a, **k):
        raise wiz.BackupCaptureError("NSUserDefaults capture failed")

    monkeypatch.setattr(wiz, "CreateZip", _raise)

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert remote.uploaded == [] and calls == []


@pytest.mark.parametrize(
    "error_name",
    ["canceled", "network"],
)
def test_backup_dropbox_failed_upload_never_rotates(
    wiz, monkeypatch, tmp_path, error_name
):
    """The load-bearing rule: an upload that did NOT land (user cancel or transport
    error) must leave the remote folder untouched. Rotating here with backup.keep=1
    would delete the last good backup in favor of one that never arrived."""
    remote = _FakeDropboxRemote()
    remote.upload_error = (
        remote.DropboxCanceled("user canceled")
        if error_name == "canceled"
        else RuntimeError("connection reset")
    )
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert calls == [], "a failed upload must never rotate"
    assert not Path(staged[0]).exists(), "the stage is cleaned on every exit path"


def test_backup_dropbox_incomplete_backup_uploads_but_does_not_rotate(
    wiz, monkeypatch, tmp_path
):
    """A swiss-cheese backup (some files could not be captured) is KEPT and reported
    honestly, but it must not prune the last COMPLETE backup."""
    remote = _FakeDropboxRemote()
    calls, staged = _dropbox_backup_env(wiz, monkeypatch, tmp_path, remote)

    def _partial(src, dst, *a, **k):
        staged.append(dst)
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        Path(dst).write_bytes(b"PK\x03\x04staged")
        return wiz.ZipResult(entries=2, failed=["userdata/locked.xml (IOError)"])

    monkeypatch.setattr(wiz, "CreateZip", _partial)
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )

    home = tmp_path / "home"
    home.mkdir()
    wiz._backup_dropbox("full", "kodi_backup", str(home))

    assert len(remote.uploaded) == 1, "the incomplete backup is still kept"
    assert calls == [], "an incomplete backup must never rotate"
    assert any("could NOT be captured" in m for m in oks), oks


def _dropbox_restore_env(wiz, monkeypatch, remote, select=0):
    monkeypatch.setattr(resources_modules(wiz), "dropbox_remote", remote, raising=False)
    monkeypatch.setattr(
        wiz.control,
        "setting",
        lambda key: "tok" if key == "dropbox_refresh_token" else "",
    )
    monkeypatch.setattr(wiz.control, "selectDialog", lambda *a, **k: select)
    restored = []
    monkeypatch.setattr(wiz, "restore", lambda local, *a, **k: restored.append(local))
    return restored


def test_restore_dropbox_downloads_then_restores_then_removes_the_temp(
    wiz, monkeypatch, tmp_path
):
    """The happy path: the chosen backup is downloaded to a LOCAL staged path, handed
    to restore() as a plain path (no '://', so restore extracts it directly), and the
    temp copy is removed afterward."""
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    staged = tmp_path / "temp" / "kodi_backup_202607010101.zip"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"PK\x03\x04")
    remote.download_result = "special://temp/kodi_backup_202607010101.zip"
    restored = _dropbox_restore_env(wiz, monkeypatch, remote)

    wiz._restore_dropbox()

    assert remote.downloaded == ["kodi_backup_202607010101.zip"]
    assert restored == [str(staged)], restored
    assert "://" not in restored[0], "restore() must get a local path"
    assert not staged.exists(), "the downloaded temp must be removed"


def test_restore_dropbox_removes_the_temp_even_when_restore_raises(
    wiz, monkeypatch, tmp_path
):
    """The `finally: os.remove(local)` cleanup: a restore that blows up must not leave
    a full backup zip parked in special://temp forever."""
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    staged = tmp_path / "temp" / "kodi_backup_202607010101.zip"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"PK\x03\x04")
    remote.download_result = "special://temp/kodi_backup_202607010101.zip"
    _dropbox_restore_env(wiz, monkeypatch, remote)

    def _boom(*a, **k):
        raise RuntimeError("restore exploded")

    monkeypatch.setattr(wiz, "restore", _boom)

    with pytest.raises(RuntimeError):
        wiz._restore_dropbox()
    assert not staged.exists(), "the temp must be cleaned on the exception path too"


def test_restore_dropbox_canceled_download_restores_nothing(wiz, monkeypatch, tmp_path):
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    remote.download_error = remote.DropboxCanceled("user canceled")
    restored = _dropbox_restore_env(wiz, monkeypatch, remote)

    wiz._restore_dropbox()

    assert restored == [], "a canceled download must not start a restore"


def test_restore_dropbox_failed_download_restores_nothing(wiz, monkeypatch, tmp_path):
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    remote.download_error = RuntimeError("connection reset")
    restored = _dropbox_restore_env(wiz, monkeypatch, remote)
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )

    wiz._restore_dropbox()

    assert restored == []
    assert any("Could not download" in m for m in oks), oks


def test_restore_dropbox_list_failure_and_empty_folder_restore_nothing(
    wiz, monkeypatch, tmp_path
):
    oks = []
    monkeypatch.setattr(
        wiz.dialog, "ok", lambda *a, **k: oks.append(" ".join(map(str, a)))
    )
    remote = _FakeDropboxRemote(list_error=RuntimeError("401"))
    restored = _dropbox_restore_env(wiz, monkeypatch, remote)
    wiz._restore_dropbox()
    assert restored == [] and remote.downloaded == []
    assert any("Could not list" in m for m in oks), oks

    empty = _FakeDropboxRemote(names=[])
    restored2 = _dropbox_restore_env(wiz, monkeypatch, empty)
    wiz._restore_dropbox()
    assert restored2 == [] and empty.downloaded == []
    assert any("No backups found" in m for m in oks), oks


def test_restore_dropbox_backing_out_of_the_picker_downloads_nothing(
    wiz, monkeypatch, tmp_path
):
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    restored = _dropbox_restore_env(wiz, monkeypatch, remote, select=-1)

    wiz._restore_dropbox()

    assert remote.downloaded == [] and restored == []


def test_restore_dropbox_without_a_token_does_nothing(wiz, monkeypatch, tmp_path):
    remote = _FakeDropboxRemote(names=["kodi_backup_202607010101.zip"])
    restored = _dropbox_restore_env(wiz, monkeypatch, remote)
    monkeypatch.setattr(wiz.control, "setting", lambda key: "")

    wiz._restore_dropbox()

    assert remote.downloaded == [] and restored == []


# --------------------------------------------------------------------------- #
# D5: dead extractor removal. ExtractWithProgress is the ONE extractor; the old
# ExtractZip dispatcher and its silent ExtractNOProgress branch had no callers
# (dead code calling dead code) and are gone. This guard keeps them gone: a silent,
# progress-less, whole-archive extractall() is exactly the "restore that lies about
# what landed" shape the ExtractResult contract exists to prevent.
# --------------------------------------------------------------------------- #


def test_dead_extractors_are_gone_and_stay_gone(wiz):
    assert not hasattr(wiz, "ExtractZip")
    assert not hasattr(wiz, "ExtractNOProgress")
    src = (ADDON_ROOT / "resources" / "lib" / "modules" / "wiz.py").read_text(
        encoding="utf-8"
    )
    # The CALL form only - the removal note in wiz.py names the function in prose.
    assert ".extractall(" not in src, (
        "a whole-archive extractall() cannot report per-member failures"
    )
    for gone in ("def ExtractZip", "def ExtractNOProgress"):
        assert gone not in src, "wiz.py must no longer contain %r" % gone


# --------------------------------------------------------------------------- #
# Restore defect B: a destroyed dialog must never be treated as an answer.
# Reproduced on the bench 2026-07-18: skin.estuary7 arms a 15s AlarmClock on first
# Home load whose skinshortcuts rebuild ends in ReloadSkin(), which destroyed this
# very prompt 15.27s in while the marker was then consumed as if answered.
# See docs/restore-defect-b-reproduced-2026-07-18.md.
# --------------------------------------------------------------------------- #
def test_keyboard_result_reports_confirmation_honestly(wiz, monkeypatch):
    """_keyboard_result must return the text even when unconfirmed, so callers can
    re-present prefilled. _get_keyboard keeps its legacy cancel-sentinel contract."""
    tools = wiz.tools

    class FakeKB:
        def __init__(self, default="", heading="", hidden=False):
            self.text = default

        def doModal(self):
            self.text = "half typed"

        def isConfirmed(self):
            return False

        def getText(self):
            return self.text

    monkeypatch.setattr(tools.xbmc, "Keyboard", FakeKB)
    confirmed, text = tools._keyboard_result(default="OfficeBox")
    assert confirmed is False
    assert text == "half typed", (
        "unconfirmed text must still be returned, not discarded"
    )
    # legacy wrapper is unchanged for the sentinel callers (wiz backup naming, cache size)
    assert tools._get_keyboard(default="OfficeBox", cancel="-") == "-"


# --------------------------------------------------------------------------- #
# Restore defect B, the half that lost the tune-up permanently.
#
# prompt_buffer_after_restore OWNS the marker for the whole post-restore flow.
# It used to clear it in a blanket finally, so a dialog destroyed by the skin's
# 15s deferred menu rebuild (reproduced 2026-07-18: ReloadSkin tears down the
# whole window stack) was read as an answer and the tune-up was gone forever,
# silently. Only an explicit answer may consume the marker now.
# See docs/restore-defect-b-reproduced-2026-07-18.md.
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Restore defect A: restored skin settings are clobbered on the way out.
#
# Restore writes addon_data/<skin>/settings.xml correctly, then ui.restart() ->
# Quit -> CApplication::Stop() -> g_SkinInfo->SaveSettings() serializes the
# PRE-RESTORE in-memory skin settings back over it (Application.cpp:2141). The
# documented fix for this class is BOTH mechanisms reconciled: the file write is
# the extract, the in-memory half is _apply_skin_settings.
# --------------------------------------------------------------------------- #


def _skin_settings_xml(tmp_path, skin, pairs):
    d = tmp_path / "addon_data" / skin
    d.mkdir(parents=True, exist_ok=True)
    body = "".join(
        '<setting id="%s" type="%s">%s</setting>' % (i, t, v) for i, t, v in pairs
    )
    p = d / "settings.xml"
    p.write_text("<settings version='2'>%s</settings>" % body, encoding="utf-8")
    return str(p)


def test_read_skin_settings_parses_and_ignores_unknown_types(wiz, tmp_path):
    """Mirror CSkinInfo::SettingsFromXML: keep bool/string, drop anything else."""
    p = _skin_settings_xml(
        tmp_path,
        "skin.estuary7",
        [
            ("use_pov_search", "bool", "true"),
            ("home_label", "string", "Movies"),
            ("weird", "int", "5"),
        ],
    )
    got = wiz._read_skin_settings(p)
    assert ("use_pov_search", "bool", "true") in got
    assert ("home_label", "string", "Movies") in got
    assert not [g for g in got if g[0] == "weird"], "unknown types must be dropped"


def test_read_skin_settings_never_raises(wiz, tmp_path):
    assert wiz._read_skin_settings(str(tmp_path / "nope.xml")) == []
    bad = tmp_path / "bad.xml"
    bad.write_text("<not-xml", encoding="utf-8")
    assert wiz._read_skin_settings(str(bad)) == []


def test_apply_skin_settings_uses_two_argument_builtins(wiz, monkeypatch):
    """The two-argument forms are mandatory.

    Skin.SetBool(id) with ONE argument can only ever set true, and
    Skin.SetString(id) with one argument opens a KEYBOARD, which would hang the
    restore. A test that only asserted 'a Skin.SetString was issued' would pass
    on the hanging form, so assert the exact commands."""
    _record_window_props(wiz, monkeypatch)
    builtins = []
    monkeypatch.setattr(
        wiz.xbmc, "executebuiltin", lambda cmd, *a: builtins.append(cmd), raising=False
    )
    monkeypatch.setattr(wiz.xbmc, "getSkinDir", lambda: "skin.estuary7", raising=False)

    status = wiz._apply_skin_settings(
        lambda *a, **k: None,
        "skin.estuary7",
        [
            ("use_pov_search", "bool", "true"),
            ("hide_x", "bool", "false"),
            ("home_label", "string", "Movies"),
        ],
    )

    assert "Skin.SetBool(use_pov_search,true)" in builtins
    assert "Skin.SetBool(hide_x,false)" in builtins, "false must be set explicitly"
    # The value is QUOTED: unquoted, any value containing a comma is truncated by
    # CUtil::SplitParams and the truncated form is what the shutdown flush persists.
    assert 'Skin.SetString(home_label,"Movies")' in builtins
    assert status.startswith("applied:3/3")


def _split_params(cmd):
    """Faithful port of Kodi's CUtil::SplitParams (Util.cpp:1211).

    The defect this guards is only visible through Kodi's own parser: the bug is
    that a value or id changes the ARITY of the builtin. Asserting on the command
    string cannot see that; parsing it the way Kodi does can."""
    inner = cmd[cmd.index("(") + 1 : cmd.rindex(")")]
    params, parameter, in_quotes, in_function, escaped = [], "", False, 0, False
    for ch in inner:
        if escaped:
            parameter += ch
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if not in_quotes:
            if ch == "(":
                in_function += 1
            elif ch == ")" and in_function:
                in_function -= 1
            elif ch == "," and not in_function:
                params.append(parameter)
                parameter = ""
                continue
        parameter += ch
    if parameter or params:
        params.append(parameter)
    return params


def test_skin_string_values_survive_kodi_param_splitting(wiz, monkeypatch):
    """A value containing a comma, quote or paren must round-trip EXACTLY.

    Unquoted, `Skin.SetString(label,Movies, HD)` parses to ["label", "Movies"]:
    the value is silently truncated, set in live memory, and then serialized over
    the correctly restored file by the shutdown flush - so the fix meant to prevent
    the clobber becomes the clobber. Skin string settings hold user-typed labels,
    so commas are ordinary, not exotic."""
    _record_window_props(wiz, monkeypatch)
    builtins = []
    monkeypatch.setattr(
        wiz.xbmc, "executebuiltin", lambda cmd, *a: builtins.append(cmd), raising=False
    )
    monkeypatch.setattr(wiz.xbmc, "getSkinDir", lambda: "skin.estuary7", raising=False)

    hostile = [
        "Movies, HD",
        'He said "hi"',
        "path\\to\\thing",
        "Kids (ages 4-8)",
        "  padded  ",
        "",
    ]
    wiz._apply_skin_settings(
        lambda *a, **k: None,
        "skin.estuary7",
        [("v%d" % i, "string", v) for i, v in enumerate(hostile)],
    )

    assert len(builtins) == len(hostile)
    for cmd, original in zip(builtins, hostile):
        params = _split_params(cmd)
        assert len(params) == 2, "arity must stay 2, else a KEYBOARD opens: %r" % cmd
        assert params[1] == original, "value corrupted: %r -> %r" % (
            original,
            params[1],
        )


def test_read_skin_settings_drops_ids_that_would_open_a_keyboard(wiz, tmp_path):
    """An id carrying "(" or '"' collapses the builtin to ONE parameter.

    CUtil::SplitParams suppresses the separating comma inside a paren or quote, so
    `Skin.SetString(Bad(Id,hello)` parses to a single param - the one-argument form,
    which opens a keyboard. executebuiltin(..., True) BLOCKS, so the restore hangs
    unrecoverably on a TV with no restore UI expecting input. The id comes from a
    restored archive, possibly fetched from Dropbox, so it is untrusted."""
    p = tmp_path / "settings.xml"
    p.write_text(
        "<settings>"
        '<setting id="good.id_1" type="string">ok</setting>'
        '<setting id="Bad(Id" type="string">hello</setting>'
        '<setting id="quo&quot;te" type="string">hello</setting>'
        '<setting id="semi;colon" type="bool">true</setting>'
        "</settings>",
        encoding="utf-8",
    )
    ids = [sid for sid, _t, _v in wiz._read_skin_settings(str(p))]
    assert ids == ["good.id_1"], "unsafe ids must be dropped, not emitted"


def test_apply_skin_settings_skipped_when_restored_skin_is_not_live(wiz, monkeypatch):
    """g_SkinInfo is the LIVE skin and flushes to its OWN addon_data dir. If the
    restored skin is not live, the flush cannot touch the restored file and the
    builtins would write into the WRONG skin, so emit nothing."""
    props = _record_window_props(wiz, monkeypatch)
    builtins = []
    monkeypatch.setattr(
        wiz.xbmc, "executebuiltin", lambda cmd, *a: builtins.append(cmd), raising=False
    )
    monkeypatch.setattr(wiz.xbmc, "getSkinDir", lambda: "skin.estuary", raising=False)

    status = wiz._apply_skin_settings(
        lambda *a, **k: None, "skin.estuary7", [("x", "bool", "true")]
    )

    assert builtins == [], "must emit NOTHING when the restored skin is not live"
    assert status == "skipped:not-live"
    assert props.get("ezm_skin_reapply") == "skipped:not-live"


def test_apply_skin_settings_never_breaks_a_restore(wiz, monkeypatch):
    _record_window_props(wiz, monkeypatch)
    monkeypatch.setattr(
        wiz.xbmc,
        "getSkinDir",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        raising=False,
    )
    assert (
        wiz._apply_skin_settings(
            lambda *a, **k: None, "skin.estuary7", [("x", "bool", "true")]
        )
        is not None
    )


def test_no_test_probes_skin_settings_via_the_mutating_api(wiz):
    """The JSON-RPC skin-setting probes are NOT read-only:
    CSkinInfo::TranslateBool inserts a default-false bool AND schedules a save, so a
    test built on them passes for the wrong reason and mutates the thing it measures."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    hits = []
    for p in list((root / "tests").rglob("*.py")) + list(
        (root / "tools").rglob("*.py")
    ):
        try:
            t = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        # Build the needles from parts so this guard cannot match its OWN source,
        # the same trick the fleet's banned-character hooks use.
        needles = ("Skin." + "HasSetting", "GetInfo" + "Booleans")
        if any(n in t for n in needles):
            hits.append(p.name)
    assert not hits, "mutating skin-setting probe used in: %s" % sorted(set(hits))


# --------------------------------------------------------------------------- #
# Video cache buffer: the recommendation must be a size Kodi actually OFFERS.
#
# A value outside Kodi's list is honored at runtime (CSettingInt::CheckValidity
# skips validation when a dynamic filler is present), but merely opening that
# screen in Kodi's own GUI can snap it to a listed value. The fleet ran 200 on
# Apple TV and 165/166 on Fire TV, none of them listed, so every box carried
# that hazard silently.
# --------------------------------------------------------------------------- #


def test_recommendation_is_always_a_size_kodi_offers(wiz, monkeypatch):
    tools = wiz.tools
    for total in (0, 512, 1024, 1655, 2048, 3072, 4096, 8192, 16384):
        monkeypatch.setattr(tools, "_total_ram_mb", lambda t=total: t)
        rec = tools._recommended_mb()
        assert rec in tools._KODI_CACHE_SIZES, (
            "total=%s produced %s, which Kodi's GUI does not offer and may snap"
            % (total, rec)
        )


def test_snap_rounds_down_never_up(wiz):
    """Down, not nearest. Too large costs resident memory the OS cannot reclaim,
    and on tvOS an allocation failure is an uncatchable kill; too small only
    shortens a buffer on a workload that already has more depth than it needs."""
    tools = wiz.tools
    for raw, expect in ((200, 192), (165, 128), (100, 96), (128, 128), (16, 16)):
        assert tools._snap_to_kodi_size(raw) == expect, raw


def test_snap_never_returns_below_the_smallest_offered(wiz):
    tools = wiz.tools
    assert tools._snap_to_kodi_size(0) == min(tools._KODI_CACHE_SIZES)
    assert tools._snap_to_kodi_size(-5) == min(tools._KODI_CACHE_SIZES)


def test_recommendation_still_respects_the_ceiling(wiz, monkeypatch):
    """A huge-RAM box must not be recommended an unbounded buffer."""
    tools = wiz.tools
    monkeypatch.setattr(tools, "_total_ram_mb", lambda: 65536)
    assert tools._recommended_mb() <= 200


def test_purgeable_destination_flags_the_tvos_caches_tree(wiz, monkeypatch):
    """A backup inside Library/Caches is destroyed by the purge it insures against.

    On tvOS Kodi's whole tree lives under <container>/Library/Caches/Kodi, tvOS may
    evict that under storage pressure, and Apple excludes it from iCloud and Finder
    backups. Storing a backup there means losing it in the same event that makes it
    necessary - and the browse dialog offers exactly that location as its home."""
    monkeypatch.setattr(
        wiz,
        "translatePath",
        lambda p: "/var/mobile/Containers/Data/Application/ABC/Library/Caches/Kodi/",
        raising=False,
    )
    caches = (
        "/var/mobile/Containers/Data/Application/ABC/Library/Caches/Kodi/"
        "backups/my_backup.zip"
    )
    assert wiz._purgeable_destination(caches) is True


def test_purgeable_destination_allows_safe_locations(wiz, monkeypatch):
    """USB, network and Dropbox destinations must NOT be warned about.

    A guard that cries wolf on a correct choice trains the user to dismiss it, which
    costs more than it saves."""
    monkeypatch.setattr(
        wiz,
        "translatePath",
        lambda p: "/var/mobile/Containers/Data/Application/ABC/Library/Caches/Kodi/",
        raising=False,
    )
    for safe in (
        "/private/var/mobile/Media/backups/my_backup.zip",
        "nfs://192.168.7.10/vol/backups/my_backup.zip",
        "smb://nas/backups/my_backup.zip",
        "/Users/someone/Backups/my_backup.zip",
        "",
    ):
        assert wiz._purgeable_destination(safe) is False, safe


def test_backup_warns_but_still_lets_the_user_proceed(wiz, monkeypatch):
    """Warn, do not refuse - and never block a backup because the guard itself failed."""
    monkeypatch.setattr(wiz, "_purgeable_destination", lambda p: True)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True, raising=False)
    assert (
        wiz._confirm_destination_survives_a_purge("/x/Library/Caches/Kodi/b.zip")
        is True
    )
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: False, raising=False)
    assert (
        wiz._confirm_destination_survives_a_purge("/x/Library/Caches/Kodi/b.zip")
        is False
    )

    def boom(*a, **k):
        raise RuntimeError("guard broke")

    monkeypatch.setattr(wiz, "_purgeable_destination", boom)
    assert wiz._confirm_destination_survives_a_purge("/anything.zip") is True


def test_backup_consults_the_purge_guard_before_building_the_zip(
    wiz, monkeypatch, tmp_path
):
    """backup() must CALL the guard and honour 'Pick another'.

    Testing _confirm_destination_survives_a_purge directly does not cover the call
    site: deleting the call from backup() leaves those unit tests green, and the only
    thing that then fails is the storage-fingerprint gate - which fires on ANY edit to
    wiz.py, including correct ones, so it is not evidence about this behaviour. A
    guard nobody calls is worth nothing, and this pins the wiring."""
    _stub_backup_env(wiz, monkeypatch, tmp_path)
    seen = {"asked": False, "zip": False}

    def _guard(dest):
        seen["asked"] = True
        return False  # user chose "Pick another"

    def _no_zip(*a, **k):
        seen["zip"] = True
        return False

    monkeypatch.setattr(wiz, "_confirm_destination_survives_a_purge", _guard)
    monkeypatch.setattr(wiz, "CreateZip", _no_zip)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True)
    wiz.backup(mode="full")

    assert seen["asked"] is True, "backup() must consult the purge guard"
    assert seen["zip"] is False, (
        "declining the destination must abort BEFORE any zip is built - otherwise the "
        "warning is cosmetic and the backup still lands somewhere it cannot survive"
    )


def test_caches_is_detected_even_when_special_home_is_unresolvable(wiz, monkeypatch):
    """The literal Library/Caches check must stand on its own.

    The other purge test lets translatePath resolve to the Caches home, so the
    special://home fallback catches the path and the explicit check is never
    exercised - deleting it leaves that test green (verified by mutation with the
    fingerprint gate ignored). On a real box translatePath can fail or resolve
    elsewhere, and then the literal check is the ONLY protection."""

    def _boom(_p):
        raise RuntimeError("special:// unavailable")

    monkeypatch.setattr(wiz, "translatePath", _boom, raising=False)
    caches = (
        "/var/mobile/Containers/Data/Application/ABC/Library/Caches/Kodi/"
        "backups/my_backup.zip"
    )
    assert wiz._purgeable_destination(caches) is True, (
        "a Caches destination must be flagged without depending on special://home"
    )
    assert wiz._purgeable_destination("/Volumes/USB/backups/my_backup.zip") is False


def test_restore_records_the_archives_skin_in_the_check_marker(
    wiz, monkeypatch, tmp_path
):
    """The restore must PASS the archive's skin to the marker, not just arm it.

    Arming with no expectation leaves defect A3 undetectable: the shutdown flush can
    overwrite the boot-skin write, the box reopens on the old skin, and the boot check
    has nothing to compare against so it stays silent. Asserting only that the marker
    was armed (as the sibling test does) passes against that exact regression."""
    _prep_restore(wiz, monkeypatch, tmp_path)
    _record_restore_report(wiz, monkeypatch)
    got = {}
    monkeypatch.setattr(
        wiz.tools,
        "mark_restore_check_pending",
        lambda expected_skin=None: got.update(skin=expected_skin),
    )
    monkeypatch.setattr(wiz, "_read_target_skin", lambda *a, **k: "skin.estuary7")

    src = tmp_path / "kodi_settings_x.zip"
    _make_valid_zip(src, [("guisettings.xml", "<s/>")])
    wiz.restore(str(src), confirm=False)

    assert "skin" in got, "the restore must call the marker with the expected skin"
    assert got["skin"] == "skin.estuary7", (
        "the ARCHIVE's skin must be recorded, got %r - without it the boot check "
        "cannot detect a wrong-skin reopen" % (got["skin"],)
    )


def test_kodi_home_is_flagged_on_a_platform_whose_home_is_not_in_caches(
    wiz, monkeypatch
):
    """The special://home fallback must stand on its own, on Android.

    Every other purge test lets translatePath resolve INSIDE Library/Caches, so the
    literal Caches check absorbs the case and this fallback is never the sole
    detector - replacing it with `return False` passed the whole suite. That left the
    entire Android arm of the documented claim ("any destination under the Kodi home
    is self-defeating on every platform") untested. Fire OS home is /sdcard/..., which
    is NOT under Caches, so only this branch can catch it there."""
    home = "/sdcard/Android/data/org.xbmc.kodi/files/.kodi/"
    monkeypatch.setattr(wiz, "translatePath", lambda p: home, raising=False)

    inside = home + "backups/my_backup.zip"
    assert wiz._purgeable_destination(inside) is True, (
        "a backup under the Kodi home is taken by a wipe or reinstall - the fallback "
        "must flag it even where the home is not inside Library/Caches"
    )
    # Safe destinations on the same platform must NOT be flagged.
    for safe in (
        "/storage/usb/backups/my_backup.zip",
        "/sdcard/Backups/my_backup.zip",
        "/sdcard/Android/data/org.xbmc.kodi/files/.kodi-evil/my_backup.zip",
    ):
        assert wiz._purgeable_destination(safe) is False, (
            "must not flag %s - a sibling that merely shares a name prefix is not "
            "inside the home" % safe
        )


def test_texture_cache_db_is_never_captured_in_a_backup(wiz):
    """Kodi's texture cache must not ride along in a backup archive.

    It used to be absent by ACCIDENT: maintenance.deleteThumbnails() ran first in
    every backup and unlinked the file, so the walk never saw it. That unlink is what
    crashed the office Fire TV on 2026-07-21 (SIGABRT after SQLITE_READONLY_DBMOVED on
    an open handle) and has been removed, so without this exclusion the file is now
    present at walk time and every archive grows by its lifetime high-water mark -
    SQLite never returns freed pages, so an emptied 1.2 MB cache stays 1.2 MB.
    """
    for captured in (
        "userdata/Database/Textures13.db",
        "Database/Textures13.db",
        "userdata/Database/Textures99.db",
    ):
        assert wiz._is_regenerable_cache_arc(captured) is True, captured
    # Precision: only the texture cache. Every other database is real user state and
    # a backup that quietly dropped one would be lying about being full.
    for kept in (
        "userdata/Database/MyVideos131.db",
        "userdata/Database/Addons33.db",
        "userdata/Database/Epg16.db",
        "userdata/Textures13.db",
        "addons/plugin.video.x/Database/Textures13.db.bak",
    ):
        assert wiz._is_regenerable_cache_arc(kept) is False, kept


def test_texture_exclusion_does_not_eat_third_party_addon_data(wiz):
    """QA finding 2026-07-21: the exclusion matched "/Database/" at ANY depth.

    "Full means full" is this project's backup contract. An add-on that keeps its own
    Database/ folder with a file named Textures* was being silently dropped from the
    archive, with nothing in the manifest to say so - a backup quietly lying about
    being complete. Anchor to the real userdata database directory instead.
    """
    assert (
        wiz._is_regenerable_cache_arc(
            "userdata/addon_data/plugin.foo/Database/TexturesOfMyVacation.db"
        )
        is False
    )
    assert wiz._is_regenerable_cache_arc("addons/x/Database/Textures13.db") is False
    # The sidecars go WITH the cache: a Textures13.db-journal restored beside a
    # surviving database is a silent rollback of state the user did not ask to lose.
    assert wiz._is_regenerable_cache_arc("userdata/Database/Textures13.db-journal")
    assert wiz._is_regenerable_cache_arc("userdata/Database/Textures13.db-wal")


# --------------------------------------------------------------------------- #
# Device-name prefix on backup filenames (2026.08.31.2). The suggested name is
# "<devicename>_kodi_backup"; a box with no real name keeps the plain historical
# name, and NOTHING downstream may match on the prefix, so old archives stay
# visible to restore and rotation forever.
# --------------------------------------------------------------------------- #


def test_default_backup_name_carries_the_device_name(wiz, monkeypatch):
    monkeypatch.setattr(wiz.tools, "_get_devicename", lambda: "ts1")
    assert wiz._default_backup_name("kodi_backup") == "ts1_kodi_backup"
    assert wiz._default_backup_name("kodi_settings") == "ts1_kodi_settings"


def test_default_backup_name_plain_when_devicename_empty(wiz, monkeypatch):
    monkeypatch.setattr(wiz.tools, "_get_devicename", lambda: "")
    assert wiz._default_backup_name("kodi_backup") == "kodi_backup"


def test_default_backup_name_plain_on_kodi_stock_default(wiz, monkeypatch):
    # A fresh install is already named "Kodi": that is not a real name, and
    # "kodi_kodi_backup" would be noise. Any casing of the stock default counts.
    for stock in ("Kodi", "kodi", "KODI"):
        monkeypatch.setattr(wiz.tools, "_get_devicename", lambda s=stock: s)
        assert wiz._default_backup_name("kodi_backup") == "kodi_backup"


def test_default_backup_name_plain_when_devicename_unreadable(wiz, monkeypatch):
    def _boom():
        raise RuntimeError("JSON-RPC unavailable")

    monkeypatch.setattr(wiz.tools, "_get_devicename", _boom)
    assert wiz._default_backup_name("kodi_backup") == "kodi_backup"


def test_device_backup_prefix_sanitizes_for_filenames(wiz, monkeypatch):
    # Lowercase, spaces to underscores, everything outside [a-z0-9_-] stripped.
    monkeypatch.setattr(wiz.tools, "_get_devicename", lambda: "Living Room TV!")
    assert wiz._device_backup_prefix() == "living_room_tv"
    # A name that sanitizes to nothing gives NO prefix, not a bare underscore.
    monkeypatch.setattr(wiz.tools, "_get_devicename", lambda: "!!! ***")
    assert wiz._device_backup_prefix() == ""
    assert wiz._default_backup_name("kodi_backup") == "kodi_backup"


def test_backup_suggests_and_builds_the_device_prefixed_name(
    wiz, monkeypatch, tmp_path
):
    """End to end: with a named box, backup() offers <device>_kodi_backup as the
    keyboard default, and accepting it yields <device>_kodi_backup_<stamp>.zip."""
    _stub_backup_env(wiz, monkeypatch, tmp_path)
    monkeypatch.setattr(wiz.tools, "_get_devicename", lambda: "ts1")
    offered = {}

    def _keyboard(default="", **k):
        offered["default"] = default
        return default  # the user accepts the suggestion

    monkeypatch.setattr(wiz.tools, "_get_keyboard", _keyboard)
    monkeypatch.setattr(wiz.ui, "confirm", lambda *a, **k: True)
    captured = {}

    def _fake_create_zip(src, dst, *a, **k):
        captured["dst"] = dst
        return False

    monkeypatch.setattr(wiz, "CreateZip", _fake_create_zip)
    wiz.backup(mode="full")
    assert offered["default"] == "ts1_kodi_backup"
    fname = Path(captured["dst"]).name
    assert fname.startswith("ts1_kodi_backup_")
    assert wiz._name_stamp(fname), "the auto timestamp must still be appended"


def test_old_and_new_backup_names_both_roll_and_both_stamp(wiz):
    """Rotation and ordering key on the trailing stamp, never the prefix: a
    plain pre-2026.08.31.2 archive and a device-prefixed one are BOTH rolling
    candidates, so neither generation is invisible to keep-N or to newest-first."""
    old_name = "kodi_backup_202608310512.zip"
    new_name = "ts1_kodi_backup_202608310512.zip"
    assert wiz._is_rolling(old_name)
    assert wiz._is_rolling(new_name)
    assert wiz._name_stamp(old_name) == wiz._name_stamp(new_name) == "202608310512"


def test_infer_type_reads_through_the_device_prefix(wiz):
    """The restore anchor hint still classifies a prefixed name: 'backup' and
    'kodi_settings' are substring matches, so the prefix cannot flip them."""
    from resources.lib.modules import onetap

    assert onetap.infer_type("ts1_kodi_backup_202608310512.zip") == "full"
    assert onetap.infer_type("ts1_kodi_settings_202608310512.zip") == "userdata"
    # And the old plain names classify exactly as before.
    assert onetap.infer_type("kodi_backup_202608310512.zip") == "full"
    assert onetap.infer_type("kodi_settings_202608310512.zip") == "userdata"


def test_restore_folder_lists_old_and_new_names_side_by_side(
    wiz, monkeypatch, tmp_path
):
    """restoreFolder filters on .zip alone: a folder holding one plain-named and
    one device-prefixed archive offers BOTH. A prefix-anchored filter here is
    the failure that would make new backups invisible to restore."""
    monkeypatch.setattr(
        wiz.control, "setting", lambda key: "/backups/" if key == "restore.path" else ""
    )
    monkeypatch.setattr(
        wiz.xbmcvfs,
        "listdir",
        lambda p: ([], ["kodi_backup_202607010000.zip",
                        "ts1_kodi_backup_202608310512.zip",
                        "notes.txt"]),
    )
    seen = {}

    def _select(names, *a, **k):
        seen["names"] = list(names)
        return -1  # cancel: listing behaviour is the whole assertion

    monkeypatch.setattr(wiz.control, "selectDialog", _select)
    wiz.restoreFolder()
    assert seen["names"] == [
        "kodi_backup_202607010000.zip",
        "ts1_kodi_backup_202608310512.zip",
    ]
