"""Coverage for the owner-facing tool actions in default.py.

The owner-facing tool action is:
  * "Verify backup archive" (action verify_backup_archive): the restore.path picker,
    then a READ-ONLY zip analysis (entry count, backup_manifest.json presence, the
    manifest failed list, IPTV addon_data presence, top-level composition). It never
    extracts and never restores. It is reached from the Backup/Restore select dialog
    (its third entry); the Tools category that used to host it is gone.

The real default.py is imported against fully faked Kodi modules and stubbed sibling
modules (control/ui/maintenance), following the pattern of
test_ezmaintenanceplusplus_tools.py. default.py routes at import time, so the fixture
imports it with a no-op action; each test then drives the factored functions directly.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

ADDON_ROOT = Path(__file__).parent.parent / "script.ezmaintenanceplusplus"
DEFAULT_PY = ADDON_ROOT / "default.py"
MODULES = ADDON_ROOT / "resources" / "lib" / "modules"


def _real_tab_index():
    """control.SETTINGS_TAB_BACKUP_RESTORE, read out of control.py's source.

    Hardcoding 1 here would let the constant and the menu drift apart silently: the
    stub would keep asserting the old tab after the real one moved. control.py is not
    importable in this fixture (it builds Kodi objects at import), so the value is
    lifted from its source and fails loudly if the constant is renamed or deleted.
    Whether that value is the RIGHT tab is settled separately, against settings.xml,
    by test_the_backup_restore_tab_index_matches_what_kodi_would_render."""
    import ast

    src = (MODULES / "control.py").read_text()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "SETTINGS_TAB_BACKUP_RESTORE" for t in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError("control.py no longer defines SETTINGS_TAB_BACKUP_RESTORE")


def _real_configured_path(control):
    """wiz.configured_path's REAL source, bound to this fixture's control stub.

    The Backup/Restore rows and the actions behind them must agree on what
    "configured" means - a whitespace-only setting is not a path, and the nfs:// port
    Kodi's browse dialog bakes in is stripped before use. A hand-written stub here
    would be a SECOND copy of that rule, and two copies of a predicate drifting is the
    exact bug being fixed, so it could not catch a regression.

    wiz.py is far too heavy to import for a menu test (it pulls the whole backup
    stack), so this lifts the actual definitions out of its source and binds them to
    the stub control. If wiz stops defining them, or renames them, this fails loudly
    instead of quietly testing a copy."""
    import ast
    import re as _re

    src = (MODULES / "wiz.py").read_text()
    tree = ast.parse(src)
    wanted = {"_strip_nfs_port", "configured_path"}
    chunks = []
    saw_regex = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            chunks.append(ast.get_source_segment(src, node))
            wanted.discard(node.name)
        elif isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") == "_NFS_PORT_RE" for t in node.targets
        ):
            chunks.append(ast.get_source_segment(src, node))
            saw_regex = True
    assert not wanted, "wiz.py no longer defines %s" % sorted(wanted)
    assert saw_regex, "wiz.py no longer defines _NFS_PORT_RE"
    ns = {"re": _re, "control": control}
    exec("\n".join(chunks), ns)
    return ns["configured_path"]


# --------------------------------------------------------------------------- #
# Fixture: import the real default.py under fakes
# --------------------------------------------------------------------------- #
@pytest.fixture
def dmod(monkeypatch):
    """Import default.py fresh under fake Kodi + stub sibling modules.

    Returns a namespace with the module and every scriptable/recording stub:
      mod, xbmc (tvos flag), xbmcvfs (listdir_result), xbmcplugin (items),
      control (settings/select_result/infoDialog_calls/openSettings_calls),
      ui (done_calls/error_calls/confirm_calls/confirm_result/copy_calls/copy_result),
      set_nsud(module_or_None) to install/replace the lazily imported nsud.
    """
    ns = types.SimpleNamespace()

    # ---- xbmc ---- #
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.log = lambda *a, **k: None
    xbmc.sleep = lambda *a, **k: None
    xbmc.translatePath = lambda p: p
    xbmc._tvos = False
    xbmc._executed = []
    xbmc.executebuiltin = lambda *a, **k: xbmc._executed.append(a[0] if a else "")

    # Monitor.waitForAbort is Kodi's shutdown signal. The looping Backup/Restore menu
    # consults it because `Quit` is ASYNC: the script outlives it, so the menu must
    # not open a modal into a teardown. `_abort` scripts that; the fake NEVER sleeps,
    # so the guard's settle time costs the suite nothing.
    xbmc._abort = False
    xbmc._abort_waits = []

    class _Monitor:
        def waitForAbort(self, timeout=0):
            xbmc._abort_waits.append(timeout)
            return bool(xbmc._abort)

        def abortRequested(self):
            return bool(xbmc._abort)

    xbmc.Monitor = _Monitor
    # Window.IsActive(addonsettings) - the other async builtin the menu must not
    # fight. `_active_window` scripts which window Kodi reports as active.
    xbmc._active_window = ""

    def _cond(cond):
        if "TVOS" in cond:
            return bool(xbmc._tvos)
        if cond.startswith("Window.IsActive("):
            return cond[len("Window.IsActive(") :].rstrip(")") == xbmc._active_window
        return False

    xbmc.getCondVisibility = _cond
    monkeypatch.setitem(sys.modules, "xbmc", xbmc)

    # ---- xbmcgui ---- #
    xbmcgui = types.ModuleType("xbmcgui")

    class _ListItem:
        def __init__(self, name, label2="", **k):
            self.name = name
            self.label2 = label2
            self.props = {}

        def setArt(self, *a, **k):
            pass

        def setInfo(self, *a, **k):
            pass

        def setProperty(self, key, value):
            # Recorded, not swallowed. Nothing in the Backup/Restore menu may set
            # a custom property any more (that was the skin coupling deleted on
            # 2026-07-22), and a fake that dropped the call would let it come back
            # unnoticed - the plain-labels pin below reads these rows.
            self.props[key] = value

        def getProperty(self, key):
            return self.props.get(key, "")

        def setLabel2(self, value):
            self.label2 = value

        def getLabel2(self):
            return self.label2

        def getLabel(self):
            return self.name

    xbmcgui.ListItem = _ListItem

    # Home window (10000) properties. wiz.restore() publishes ezm_restore_verdict
    # there, and the looping Backup/Restore menu reads it back to tell "a restore ran
    # on this box" from "she cancelled before one started".
    xbmcgui._props = {}

    class _Window:
        def __init__(self, wid=0):
            self.wid = wid

        def setProperty(self, key, value):
            xbmcgui._props[key] = value

        def getProperty(self, key):
            return xbmcgui._props.get(key, "")

        def clearProperty(self, key):
            xbmcgui._props.pop(key, None)

    xbmcgui.Window = _Window
    monkeypatch.setitem(sys.modules, "xbmcgui", xbmcgui)

    # ---- xbmcplugin ---- #
    xbmcplugin = types.ModuleType("xbmcplugin")
    xbmcplugin.items = []  # (url, name, isFolder)

    def _add_item(handle=0, url="", listitem=None, isFolder=False):
        xbmcplugin.items.append((url, getattr(listitem, "name", ""), isFolder))
        return True

    xbmcplugin.addDirectoryItem = _add_item
    xbmcplugin.endOfDirectory = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "xbmcplugin", xbmcplugin)

    # ---- xbmcvfs ---- #
    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs._temp_map = {}  # special:// path -> real path
    xbmcvfs.translatePath = lambda p: xbmcvfs._temp_map.get(p, p)
    xbmcvfs.listdir_result = ([], [])

    def _listdir(path):
        return xbmcvfs.listdir_result

    xbmcvfs.listdir = _listdir
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs)

    # ---- stub package tree ---- #
    for pkg in ("resources", "resources.lib", "resources.lib.modules"):
        m = types.ModuleType(pkg)
        m.__path__ = []
        monkeypatch.setitem(sys.modules, pkg, m)
    pkg_mod = sys.modules["resources.lib.modules"]

    def _submodule(name, mod):
        monkeypatch.setitem(sys.modules, "resources.lib.modules.%s" % name, mod)
        setattr(pkg_mod, name, mod)

    # ---- control stub ---- #
    control = types.ModuleType("resources.lib.modules.control")
    control.USERDATA = "/fake/userdata/"
    control._settings = {}
    control.setting = lambda key: control._settings.get(key, "")
    control.addonFanart = lambda: "fanart.jpg"
    control.addonIcon = lambda: "icon.png"
    control.addonInfo = lambda key: {"version": "9.9.9"}.get(key, "")
    control.select_result = -1
    control.select_calls = []
    control.select_items = []
    # Scripted answers, consumed in order; once empty, `select_result` answers every
    # further call. Needed now that the Backup/Restore menu LOOPS: a single fixed
    # answer that is not -1 would re-drive the same branch forever.
    control.select_results = []
    # HARD BOUND. A stub that never returns -1 against a non-terminating menu loop
    # would hang the whole suite instead of failing it, so the stub itself refuses to
    # be called unreasonably often. This is what keeps the mutation check (delete the
    # loop / break, watch it go red) a FAILURE rather than a hang.
    control.select_limit = 25
    control.select_count = [0]

    def _select(options, heading=None):
        # Kodi's select takes strings OR ListItems. Record the LABELS either way,
        # so every assertion about what the menu offered reads the same whichever
        # form the caller used; select_items keeps the rows AS PASSED, which is
        # what the plain-labels pin needs (a stringified copy could not fail).
        control.select_items.append(list(options))
        control.select_calls.append(
            [o if isinstance(o, str) else o.getLabel() for o in options]
        )
        control.select_count[0] += 1
        if control.select_count[0] > control.select_limit:
            raise AssertionError(
                "selectDialog called %d times (limit %d): a menu loop is not "
                "terminating" % (control.select_count[0], control.select_limit)
            )
        if control.select_results:
            return control.select_results.pop(0)
        return control.select_result

    control.selectDialog = _select
    control.infoDialog_calls = []
    control.infoDialog = lambda msg, *a, **k: control.infoDialog_calls.append(msg)
    control.openSettings_calls = []
    control.openSettings = lambda *a, **k: control.openSettings_calls.append(a)
    # The real control.openSettings counts its own calls (control.py), because
    # Addon.OpenSettings is ASYNC and "is the settings window up yet?" is a race the
    # menu guard used to lose. Derived from the recorded calls rather than kept as a
    # separate counter, so a test that swaps openSettings for its own recorder still
    # counts correctly - and so a guard that ignores the count cannot be hidden by a
    # stub that never moves.
    control.open_settings_count = lambda: len(control.openSettings_calls)
    # The tab-aware open. Its own behaviour (wait for the window, THEN SetFocus on
    # -200 + index) is tested against the REAL control.py in
    # test_control_select_dialog.py; here it records the index it was asked for, so a
    # menu test proves default.py reached the right entry point with the right tab.
    # It calls openSettings() because the real one does, which keeps the settings-bail
    # counter the menu guard reads behaving exactly as it does on a box.
    control.openSettingsTab_calls = []

    def _open_settings_tab(index, **k):
        control.openSettingsTab_calls.append(index)
        control.openSettings()
        return True

    control.openSettingsTab = _open_settings_tab
    control.SETTINGS_TAB_BACKUP_RESTORE = _real_tab_index()
    _submodule("control", control)

    # ---- ui stub ---- #
    ui = types.ModuleType("resources.lib.modules.ui")
    ui.HEADING = "EZ Maintenance++"
    ui.COPY_OK = "ok"
    ui.COPY_FAILED = "failed"
    ui.COPY_CANCELLED = "cancelled"
    ui.done_calls = []
    ui.error_calls = []
    ui.confirm_calls = []
    ui.confirm_result = True
    ui.copy_calls = []
    ui.copy_result = "ok"
    ui.copy_payload_src = None  # real file whose bytes the fake copy stages

    def _done(message, heading=None):
        ui.done_calls.append(message)

    def _error(message, heading=None):
        ui.error_calls.append(message)

    def _confirm(message, heading=None, yeslabel="", nolabel=""):
        ui.confirm_calls.append((message, yeslabel, nolabel))
        return ui.confirm_result

    class _Progress:
        def __init__(self, message="", heading=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def items(self, done, total, note=""):
            pass

        def bytes(self, done, total, note=""):
            pass

        def cancelled(self):
            return False

    def _copy_with_progress(src, dst, progress=None):
        ui.copy_calls.append((src, dst))
        if ui.copy_result == ui.COPY_OK and ui.copy_payload_src is not None:
            real_dst = xbmcvfs.translatePath(dst)
            Path(real_dst).write_bytes(Path(ui.copy_payload_src).read_bytes())
        return ui.copy_result

    ui.done = _done
    ui.error = _error
    ui.confirm = _confirm
    ui.confirm_wipe = lambda *a, **k: False
    ui.Progress = _Progress
    ui.copy_with_progress = _copy_with_progress
    ui.restart = lambda: None
    _submodule("ui", ui)

    # ---- wiz stub ---- #
    # Always installed: the menu rows and VERIFY_BACKUP_ARCHIVE both resolve their
    # folder through wiz.configured_path now, so there is one definition of
    # "configured" instead of the row and the action each having their own.
    wiz = types.ModuleType("resources.lib.modules.wiz")
    wiz.backup = lambda *a, **k: None
    wiz.restoreFolder = lambda *a, **k: None
    wiz.configured_path = _real_configured_path(control)
    _submodule("wiz", wiz)

    # ---- tools stub ---- #
    # FRESHSTART imports this lazily to reset the video cache buffer once the wipe has
    # run (owner decision 2026-07-31: a wiped box lands on Kodi's default, never on an
    # inherited number). `event_log` is shared with the per-test onetap fake so the
    # ORDER of wipe-then-reset is assertable, not just the fact of the call.
    tools = types.ModuleType("resources.lib.modules.tools")
    tools.event_log = []
    tools.reset_cache_buffer = lambda *a, **k: tools.event_log.append("reset") or {
        "live": True,
        "file": False,
        "before": 150,
    }
    _submodule("tools", tools)

    # ---- maintenance / backtothefuture stubs ---- #
    maintenance = types.ModuleType("resources.lib.modules.maintenance")
    maintenance.getNextMaintenance = lambda: 0
    _submodule("maintenance", maintenance)

    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.PY2 = False
    b2f.unicode = str
    _submodule("backtothefuture", b2f)

    def set_nsud(module):
        """Install (or replace) the lazily imported nsud module; None removes it."""
        if module is None:
            monkeypatch.delitem(
                sys.modules, "resources.lib.modules.nsud", raising=False
            )
            if hasattr(pkg_mod, "nsud"):
                delattr(pkg_mod, "nsud")
        else:
            _submodule("nsud", module)

    # ---- import default.py with a no-op action ---- #
    monkeypatch.setattr(
        sys, "argv", ["plugin://script.ezmaintenanceplusplus/", "1", "?action=noop"]
    )
    monkeypatch.delitem(sys.modules, "ezm_default_under_test", raising=False)
    spec = importlib.util.spec_from_file_location("ezm_default_under_test", DEFAULT_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_default_under_test"] = mod
    spec.loader.exec_module(mod)

    ns.mod = mod
    ns.xbmc = xbmc
    ns.xbmcvfs = xbmcvfs
    ns.xbmcplugin = xbmcplugin
    ns.control = control
    ns.ui = ui
    ns.wiz = wiz
    ns.tools = tools
    ns.set_nsud = set_nsud
    return ns


def _make_zip(path, members, manifest=None):
    """Write a zip at `path` with `members` (name -> bytes/str). A `manifest` dict is
    added as backup_manifest.json at the archive root."""
    with zipfile.ZipFile(str(path), "w") as zf:
        for name, payload in members.items():
            if isinstance(payload, str):
                payload = payload.encode("utf-8")
            zf.writestr(name, payload)
        if manifest is not None:
            zf.writestr("backup_manifest.json", json.dumps(manifest))
    return str(path)


# --------------------------------------------------------------------------- #
# Menu wiring
# --------------------------------------------------------------------------- #
def test_categories_menu_has_no_tools_folder(dmod):
    """The Tools category is DELETED, not hidden and not emptied.

    Once the manual stale-key purge went in 2026.07.19.5, Tools held exactly one
    item - and that item is a backup operation. A folder a user must open to find
    a single entry is not a category. Nothing in the top-level menu may name it or
    route to it."""
    dmod.mod.CATEGORIES()
    names = [name for _url, name, _folder in dmod.xbmcplugin.items]
    urls = [url for url, _name, _folder in dmod.xbmcplugin.items]
    assert "Tools" not in names, names
    assert not any("action=tools" in u for u in urls), urls
    assert not hasattr(dmod.mod, "TOOLS"), "the TOOLS() builder must be gone entirely"


def test_backup_restore_offers_verify_last(dmod, monkeypatch):
    """ "Verify Backup Archive" now lives in Backup/Restore, after the two actions.

    It is a diagnostic on an archive that already exists, not a primary action, so
    it must sit after Backup and Restore rather than ahead of them. Settings comes
    after it: a jump to the config tab, not a backup operation at all. default.py
    routes at IMPORT time, so drive the real dispatch with the querystring and
    inspect the options the select dialog was actually offered."""
    _stub_wiz(monkeypatch)

    dmod.control.select_calls.clear()
    dmod.control.select_result = -1  # cancel the dialog; we only want the options
    monkeypatch.setattr(
        sys,
        "argv",
        ["plugin://script.ezmaintenanceplusplus/", "1", "?action=backup_restore"],
    )
    monkeypatch.delitem(sys.modules, "ezm_backup_restore_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_backup_restore_under_test", DEFAULT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_backup_restore_under_test"] = mod
    spec.loader.exec_module(mod)

    assert dmod.control.select_calls, "the Backup/Restore dialog never opened"
    options = dmod.control.select_calls[0]
    assert len(options) == 4, options
    # Backup and Restore carry their configured folder after the action name, so
    # match the action rather than the whole row.
    assert options[0].startswith("Backup")
    assert options[1].startswith("Restore")
    assert "VERIFY" in options[2].upper(), (
        "the verify entry must come after backup and restore: %r" % (options,)
    )
    assert options[3] == "Settings"
    assert not any(o == o.upper() for o in options), (
        "the entries are Title Case, not shouted: %r" % (options,)
    )


def _row_paths(dmod, monkeypatch, name, settings):
    """Drive the real menu with `settings` configured; return {action: path}.

    Splits each row into its action label and the greyed path folded in after it,
    so the assertions below read as "what does the Backup row say the folder is"
    rather than as string-formatting checks."""
    _stub_wiz(monkeypatch)
    dmod.control._settings.update(settings)
    dmod.control.select_calls.clear()
    dmod.control.select_result = -1  # cancel; we only want the rows
    _drive_backup_restore(monkeypatch, name)
    out = {}
    for row in dmod.control.select_calls[0]:
        action, _, rest = row.partition("   [COLOR FFA0A0A0]")
        out[action] = rest[: -len("[/COLOR]")] if rest else ""
    return out


def test_the_rows_carry_the_configured_paths_in_their_labels(dmod, monkeypatch):
    """Backup shows where it will WRITE, Restore shows where it will READ.

    The folder is greyed into the row label itself, which every skin draws, so
    the answer to "which box am I about to overwrite from" is on screen without
    backing out to settings and without any skin cooperating. The two settings
    are distinct (download.path is the archive destination, restore.path is the
    folder restores read zips from) and crossing them would point her at the
    wrong share, so both are asserted by id."""
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_paths_uut",
        {
            "destination": "1",
            "download.path": "nfs://192.168.7.10/volume1/Kodi/Backup/fireos",
            "restore.path": "smb://mini/KodiShare/apps",
        },
    )
    assert lines["Backup"] == "nfs://192.168.7.10/volume1/Kodi/Backup/fireos"
    assert lines["Restore"] == "smb://mini/KodiShare/apps"


def test_an_unconfigured_path_says_so_instead_of_reading_blank(dmod, monkeypatch):
    """ "not set" is the state that makes Backup and Restore bail into the
    settings window, so it is exactly the state worth naming. A bare row there
    would look like a rendering bug and tell her nothing."""
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_paths_unset_uut",
        {"destination": "0", "download.path": "", "restore.path": ""},
    )
    assert lines["Backup"] == "Not set"
    assert lines["Restore"] == "Not set"


def test_a_whitespace_only_path_reads_as_unset_to_the_row_AND_the_action(
    dmod, monkeypatch
):
    """The row and the action must not disagree about what "configured" means.

    They used to. The row stripped the setting and said "Not set", while backup() and
    restoreFolder() compared the RAW value to "" - so "   " was a configured folder to
    them, they walked straight past the bail-to-settings guard, and failed later on
    something the owner could do nothing with. The row told the truth and the action
    did not.

    Asserted on BOTH sides in one test, because either side alone can be made green by
    changing only itself."""
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_paths_whitespace_uut",
        {"destination": "0", "download.path": "   ", "restore.path": "\t \n"},
    )
    assert lines["Backup"] == "Not set"
    assert lines["Restore"] == "Not set"
    # The very function the actions gate on (bound to this control stub from wiz.py's
    # own source), so this is the action's answer, not a restatement of the row's.
    assert dmod.wiz.configured_path("download.path") == ""
    assert dmod.wiz.configured_path("restore.path") == ""


def test_the_rows_show_the_path_the_action_will_actually_use(dmod, monkeypatch):
    """Kodi's browse dialog bakes :2049 into nfs:// paths and both actions STRIP it
    before use (the port form breaks Kodi's own NFS client). The row used to print the
    raw setting, so it named a folder with a port the add-on discards - and the live
    boxes really do carry :2049 in that setting, so it was on screen every day.

    What is on the row is what the action will use."""
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_paths_nfsport_uut",
        {
            "destination": "1",
            "download.path": "nfs://192.168.7.2:2049/volume1/Kodi/Backup/fireos",
            "restore.path": "nfs://192.168.7.2:2049/volume1/Kodi/Backup",
        },
    )
    assert lines["Backup"] == "nfs://192.168.7.2/volume1/Kodi/Backup/fireos"
    assert lines["Restore"] == "nfs://192.168.7.2/volume1/Kodi/Backup"
    assert ":2049" not in lines["Backup"] + lines["Restore"]


# --------------------------------------------------------------------------- #
# The rows must FIT. 2026-07-22.
#
# Estuary's select dialog gives a row 840 skin pixels of label (a list 880 wide,
# 20px insets, font13 = NotoSans-Regular 30px; stock Estuary and skin.estuary7
# are identical here). The fleet's own Backup row measured 891px - past the edge
# - and Estuary SCROLLS the focused row instead of clipping it, so the row under
# the cursor was the one that turned ambiguous. A bench capture caught it
# mid-scroll reading "/Users/moquette/Kodi/Backup/fireos/ | Backup   nfs://192.168.7."
# with the row's own name sitting inside the path.
# --------------------------------------------------------------------------- #

# The two paths the boxes really carry, exactly as Kodi's browse dialog writes
# them (the :2049 included; wiz.configured_path strips it).
FLEET_BACKUP_PATH = "nfs://192.168.7.2:2049/Users/moquette/Kodi/Backup/fireos/"
FLEET_RESTORE_PATH = "nfs://192.168.7.2:2049/Users/moquette/Kodi/Backup/tvos/"


def _row_text(row):
    """A rendered row with the colour markup removed - what is actually drawn.

    [COLOR ...] and [/COLOR] are formatting tags Kodi consumes, so counting them
    would overstate every row by 20 characters."""
    import re as _re

    return _re.sub(r"\[/?COLOR[^\]]*\]", "", row)


def test_the_fleet_paths_fit_the_row_instead_of_scrolling_under_the_cursor(
    dmod, monkeypatch
):
    """THE DEFECT. With the fleet's real folders every drawn row must fit.

    Before the fix the Backup row was 61 drawn characters against a budget of 56
    and Estuary scrolled it, which is what made the focused row unreadable. This
    asserts the drawn width of EVERY row, not just the two with paths, because a
    budget applied to one row and not another is the same bug in a new place."""
    _stub_wiz(monkeypatch)
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_rows_fit_uut",
        {
            "destination": "1",
            "download.path": FLEET_BACKUP_PATH,
            "restore.path": FLEET_RESTORE_PATH,
        },
    )
    budget = dmod.mod.ROW_BUDGET
    for row in dmod.control.select_calls[0]:
        drawn = _row_text(row)
        assert len(drawn) <= budget, (
            "row %r draws %d characters against a %d character budget, so Estuary "
            "will scroll it while it is focused" % (drawn, len(drawn), budget)
        )
    # Fitting by showing nothing useful would also pass the length check, so the
    # two questions the row exists to answer are asserted too: which share, and
    # which folder on it.
    assert lines["Backup"].startswith("nfs://192.168.7.2/")
    assert lines["Restore"].startswith("nfs://192.168.7.2/")
    assert lines["Backup"].endswith("/Backup/fireos/")
    assert lines["Restore"].endswith("/Backup/tvos/")
    assert lines["Backup"] != lines["Restore"], (
        "the two rows point at different folders and must not read the same"
    )


def test_the_row_budget_counts_the_action_name_sharing_the_line(dmod, monkeypatch):
    """The path does not get the whole row - the action name is on it too.

    "Restore" is one character longer than "Backup", so the same folder must be
    allowed one character less beside it. Budgeting the path alone puts the
    Restore row over the edge, which is the defect again on one row only. The
    path here is chosen so the two rows land on DIFFERENT elisions: Backup has
    room for one more segment than Restore does."""
    path = "nfs://192.168.7.2/Users/abcdefg/Kodi/Backup/tvos/"
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_budget_per_row_uut",
        {"destination": "1", "download.path": path, "restore.path": path},
    )
    assert lines["Backup"] == "nfs://192.168.7.2/.../abcdefg/Kodi/Backup/tvos/"
    assert lines["Restore"] == "nfs://192.168.7.2/.../Kodi/Backup/tvos/"
    for row in dmod.control.select_calls[0]:
        assert len(_row_text(row)) <= dmod.mod.ROW_BUDGET, row


# (path, budget, expected) - the shapes of path this add-on has to survive.
ELIDE_CASES = [
    (
        "the fleet's Fire TV backup folder",
        "nfs://192.168.7.2/Users/moquette/Kodi/Backup/fireos/",
        47,  # what the "Backup" row leaves
        "nfs://192.168.7.2/.../Kodi/Backup/fireos/",
    ),
    (
        "the fleet's Apple TV backup folder, on the Restore row",
        "nfs://192.168.7.2/Users/moquette/Kodi/Backup/tvos/",
        46,  # what the longer "Restore" label leaves
        "nfs://192.168.7.2/.../Kodi/Backup/tvos/",
    ),
    (
        "an smb:// share, which elides exactly like nfs://",
        "smb://192.168.7.2/KodiShare/Users/moquette/Kodi/Backup/tvos/",
        46,
        "smb://192.168.7.2/.../Kodi/Backup/tvos/",
    ),
    (
        "a short local path, left alone",
        "/storage/emulated/0/backup",
        47,
        "/storage/emulated/0/backup",
    ),
    (
        "a long local path has no host, so the tail is the whole story",
        "/storage/emulated/0/Android/data/org.xbmc.kodi/files/Backup/fireos",
        47,
        ".../data/org.xbmc.kodi/files/Backup/fireos",
    ),
    (
        "a path already inside the budget is returned untouched",
        "nfs://192.168.7.2/volume1/Kodi",
        47,
        "nfs://192.168.7.2/volume1/Kodi",
    ),
    (
        "a path with no separator to cut on keeps its tail",
        "x" * 80,
        47,
        "..." + "x" * 44,
    ),
    (
        "a host so long no whole segment fits beside it still names the folder",
        "smb://averyveryveryverylonghostnamethatgoesonforever/share/fireos",
        47,
        "...rylonghostnamethatgoesonforever/share/fireos",
    ),
    ("the unset case", "", 47, ""),
    ("None, which control.setting can hand back", None, 47, None),
    ("the Not set placeholder is far too short to touch", "Not set", 47, "Not set"),
    ("the Dropbox placeholder likewise", "Dropbox", 47, "Dropbox"),
]


@pytest.mark.parametrize(
    "what,path,budget,expected",
    ELIDE_CASES,
    ids=[c[0] for c in ELIDE_CASES],
)
def test_eliding_across_every_shape_of_path_this_fleet_can_produce(
    dmod, what, path, budget, expected
):
    """Table of the real shapes: both fleet paths, smb://, short and long local,
    a path already inside the budget, a pathological one with no separators, a
    host too long to sit beside anything, and the empty/unset states.

    Two invariants hold for every row of it, asserted separately from the
    expected string so a wrong expectation cannot hide a wrong rule:

      * the result never exceeds the budget, and
      * a result that was shortened says so, with an ellipsis.
    """
    got = dmod.mod._elide_path(path, budget)
    assert got == expected, "%s: %r" % (what, got)
    if got:
        assert len(got) <= budget, "%s: %d over budget %d" % (what, len(got), budget)
        if path and len(path) > budget:
            assert dmod.mod.ELLIPSIS in got, (
                "%s: %r was shortened without saying so" % (what, got)
            )
        else:
            assert got == path, "%s: a path that fit was changed anyway" % what


def test_eliding_never_shows_half_a_folder_name(dmod):
    """The kept tail is made of WHOLE segments.

    ".../ki/Backup/fireos" would be a folder that does not exist, and a folder
    name that does not exist is worse than a shorter true one. This walks the
    budget across the whole interesting range of one real path rather than
    testing the one width that happened to be convenient."""
    path = "nfs://192.168.7.2/Users/moquette/Kodi/Backup/fireos/"
    segments = ["Users", "moquette", "Kodi", "Backup", "fireos"]
    for budget in range(22, len(path) + 1):
        got = dmod.mod._elide_path(path, budget)
        assert len(got) <= budget, (budget, got)
        if got == path:
            continue
        if not got.startswith("nfs://192.168.7.2/..."):
            continue  # the host did not fit; the tail-only fallback is its own case
        kept = [s for s in got[len("nfs://192.168.7.2/...") :].split("/") if s]
        assert kept == segments[len(segments) - len(kept) :], (budget, got)


def test_dropbox_destination_never_reports_a_local_path(dmod, monkeypatch):
    """With Destination on Dropbox, settings.xml HIDES both path settings and
    neither is used. Reporting the stale local path left in them would name a
    folder the backup is not going to - the one thing showing the path at all
    exists to prevent."""
    lines = _row_paths(
        dmod,
        monkeypatch,
        "ezm_br_paths_dropbox_uut",
        {
            "destination": "2",
            "download.path": "/storage/emulated/0/stale",
            "restore.path": "/storage/emulated/0/stale",
        },
    )
    assert lines["Backup"] == "Dropbox"
    assert lines["Restore"] == "Dropbox"


def test_the_diagnostic_rows_have_no_path(dmod, monkeypatch):
    """Verify and Settings show no path at all (owner's call).

    Their labels stand alone, with no separator and no colour markup. The single
    space this once used was an artifact of the deleted skin coupling: it existed
    only to defeat a String.IsEmpty condition in skin.estuary7."""
    lines = _row_paths(dmod, monkeypatch, "ezm_br_lines_blank_uut", {})
    assert lines["Verify Backup Archive"] == ""
    assert lines["Settings"] == ""
    # Exact rows, not just an empty parse: "Verify Backup Archive   [COLOR ...][/COLOR]"
    # would also parse to "" while shipping trailing markup to the screen.
    rows = dmod.control.select_calls[0]
    assert rows[2] == "Verify Backup Archive"
    assert rows[3] == "Settings"


def test_the_rows_are_plain_labels_a_skin_cannot_be_asked_to_decorate(
    dmod, monkeypatch
):
    """THE REGRESSION PIN for 2026-07-22. The path is IN THE LABEL, full stop.

    The original defect shipped the rows as ListItems carrying
    setProperty("ezm.footer", <path>), which only skin.estuary7 rendered: the
    paths were invisible on stock Estuary - the skin EZM++'s own Fresh Start
    REQUIRES - and one feature needed two artifacts, from two repos, to be seen
    at all.

    A plain string cannot carry a property, so this asserts the whole class away
    rather than one property name; the next such coupling would not reuse the old
    name anyway."""
    _stub_wiz(monkeypatch)
    dmod.control._settings.update(
        {"destination": "1", "download.path": "/a", "restore.path": "/b"}
    )
    dmod.control.select_calls.clear()
    dmod.control.select_items.clear()
    dmod.control.select_result = -1
    _drive_backup_restore(monkeypatch, "ezm_br_plain_rows_uut")

    # select_items keeps the rows AS PASSED; select_calls stringifies them, so
    # asserting against select_calls would be unfalsifiable - every element is a
    # str by construction there.
    for row in dmod.control.select_items[0]:
        assert isinstance(row, str), (
            "row %r is a %s, not a plain label - anything richer can carry a "
            "property only a skin we also control would render"
            % (row, type(row).__name__)
        )


def test_a_settings_read_that_throws_cannot_take_the_menu_down(dmod, monkeypatch):
    """The path is decoration; the menu is the feature. If control.setting raises
    (an odd platform, a corrupt settings.xml), the rows must still be offered as
    bare labels - no path, no separator, no dangling colour markup - not
    missing."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_result = -1

    def _boom(key):
        raise RuntimeError("settings unreadable")

    dmod.control.setting = _boom
    _drive_backup_restore(monkeypatch, "ezm_br_paths_boom_uut")

    assert dmod.control.select_calls[0] == [
        "Backup",
        "Restore",
        "Verify Backup Archive",
        "Settings",
    ]


def test_settings_entry_opens_the_backup_restore_tab_and_ends_the_menu(
    dmod, monkeypatch
):
    """The Settings entry must land ON the Backup/Restore tab, then stop the menu.

    It must go through control.openSettingsTab, which waits for the dialog before
    focusing the tab (both builtins are async). A bare control.openSettings() opens on
    the FIRST category, Maintenance, which is not where any path setting lives.

    The menu must NOT re-present afterwards: she asked for the settings window, and a
    modal select dialog on top of it is the defect the loop guard exists to prevent."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_results = [3]  # Settings, and nothing after it

    _drive_backup_restore(monkeypatch, "ezm_br_settings_entry_uut")

    assert dmod.control.openSettingsTab_calls == [
        dmod.control.SETTINGS_TAB_BACKUP_RESTORE
    ], (
        "the Settings entry must open the Backup/Restore TAB, not the settings "
        "window's first category: %r / %r"
        % (dmod.control.openSettingsTab_calls, dmod.control.openSettings_calls)
    )
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "the menu must not re-present on top of the Settings window: %d presentations"
        % len(menus)
    )


# --------------------------------------------------------------------------- #
# The tab index: what KODI renders, not what the file lists
# --------------------------------------------------------------------------- #
class UnmodelledCategory(AssertionError):
    """A settings.xml category whose presence cannot be decided by reading the file."""


def kodi_category_buttons(xml_text):
    """The category ids Kodi would draw as buttons, in button order.

    Kodi does NOT number the category buttons by file order.
    GUIDialogSettingsBase assigns CONTROL_SETTINGS_START_BUTTONS (-200) + offset over
    whatever SettingSection::GetCategories(level) returns, and that skips any category
    that fails IsVisible() or MeetsRequirements(), and any whose groups are empty at
    the current level (SettingSection.cpp). So a category that is present in the file
    but not rendered shifts every tab below it UP by one, and a jump aimed by file
    order then lands on the wrong tab while a file-order test stays green.

    This models the parts that can be decided statically:
      * a category with no unconditionally visible setting is NOT rendered (skipped);
      * a category with at least one setting at level 0, with no <visible> of its own
        and no visibility dependency, IS rendered;
      * anything else - a category carrying its own <visible>/<requirement>, or one
        whose only settings are conditional or above level 0 - is UNDECIDABLE from the
        file, and raises rather than guessing. A constant that cannot be derived is
        not a constant that may be shipped."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text)
    section = root.find("section")
    assert section is not None, "settings.xml has no <section>"
    rendered = []
    for cat in section.findall("category"):
        cid = cat.get("id")
        if cat.find("visible") is not None or cat.find("requirement") is not None:
            raise UnmodelledCategory(
                "category %r carries its own visibility/requirement condition, so the "
                "tab index can no longer be derived from the file. Aim the jump by "
                "something Kodi agrees with, or make the category unconditional." % cid
            )
        unconditional = 0
        conditional = 0
        for setting in cat.iter("setting"):
            level = setting.findtext("level")
            hidden = setting.find("visible") is not None
            dep_visible = any(
                d.get("type") == "visible" for d in setting.iter("dependency")
            )
            if level == "0" and not hidden and not dep_visible:
                unconditional += 1
            elif not hidden:
                # level > 0 (shown only at a higher UI level) or shown only when
                # another setting has some value: its category's presence moves with
                # state this file cannot resolve.
                conditional += 1
        if unconditional:
            rendered.append(cid)
        elif conditional:
            raise UnmodelledCategory(
                "category %r is rendered only under conditions the file cannot "
                "resolve (level, or a visibility dependency), so every tab below it "
                "moves with runtime state: %r" % (cid, cid)
            )
        # else: no visible settings at all -> Kodi draws no button for it
    return rendered


def test_the_backup_restore_tab_index_matches_what_kodi_would_render():
    """The shipped constant must equal Kodi's own button index, not the file's.

    This is the tie between control.SETTINGS_TAB_BACKUP_RESTORE and settings.xml.
    Reordering a category, or adding one that Kodi would skip, fails here instead of
    shipping a jump that opens the wrong tab."""
    buttons = kodi_category_buttons(
        (ADDON_ROOT / "resources" / "settings.xml").read_text()
    )
    assert buttons, "no categories would be rendered at all"
    index = _real_tab_index()
    assert buttons[index] == "backup_restore", (
        "control.SETTINGS_TAB_BACKUP_RESTORE is %d, which is tab %r, not "
        "backup_restore. Kodi would render: %r" % (index, buttons[index], buttons)
    )


def test_file_order_and_kodi_order_disagree_when_a_category_is_not_rendered():
    """Prove the model catches exactly what plain file order misses.

    This XML is the trap the old test could not see: an empty category sits FIRST, so
    counting <category> tags in file order puts backup_restore at 1 (the shipped
    constant, green) while Kodi never draws a button for the empty one and
    backup_restore is really tab 0. SetFocus(-199) would land on maintenance.

    Both halves are asserted: that naive file order says 1 (so the old derivation
    passes this XML), and that the model says 0 (so it does not)."""
    import re as _re

    xml = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<settings version="1">
  <section id="script.ezmaintenanceplusplus">
    <category id="placeholder" label="30099">
      <group id="1"/>
    </category>
    <category id="backup_restore" label="30002">
      <group id="1">
        <setting id="destination" type="integer" label="30051">
          <level>0</level>
          <default>0</default>
        </setting>
      </group>
    </category>
    <category id="maintenance" label="30000">
      <group id="1">
        <setting id="notify_mode" type="boolean" label="30030">
          <level>0</level>
          <default>false</default>
        </setting>
      </group>
    </category>
  </section>
</settings>"""
    file_order = _re.findall(r'<category\s+id="([^"]+)"', xml)
    assert file_order.index("backup_restore") == 1, (
        "the trap XML must LOOK correct to a file-order derivation, or it proves "
        "nothing: %r" % (file_order,)
    )
    buttons = kodi_category_buttons(xml)
    assert buttons == ["backup_restore", "maintenance"], buttons
    assert buttons.index("backup_restore") == 0, (
        "Kodi draws no button for a category with no settings, so backup_restore is "
        "tab 0 here and the file-order answer of 1 would focus maintenance"
    )


def test_a_conditionally_hidden_category_is_refused_rather_than_guessed():
    """The other trap: a category above backup_restore that Kodi may or may not draw.

    File order says 1 and stays green forever, while the real tab index depends on a
    runtime condition. There is no right constant to ship, so the derivation must
    REFUSE, loudly, instead of returning a number that is right only sometimes."""
    xml = """<?xml version="1.0" encoding="utf-8" standalone="yes"?>
<settings version="1">
  <section id="script.ezmaintenanceplusplus">
    <category id="advanced" label="30098">
      <visible>System.HasAddon(pvr.iptvsimple)</visible>
      <group id="1">
        <setting id="whatever" type="boolean" label="30097">
          <level>0</level>
          <default>false</default>
        </setting>
      </group>
    </category>
    <category id="backup_restore" label="30002">
      <group id="1">
        <setting id="destination" type="integer" label="30051">
          <level>0</level>
          <default>0</default>
        </setting>
      </group>
    </category>
  </section>
</settings>"""
    with pytest.raises(UnmodelledCategory, match="advanced"):
        kodi_category_buttons(xml)

    # And the same for a category Kodi only draws at a higher UI level.
    xml_level = xml.replace(
        "<visible>System.HasAddon(pvr.iptvsimple)</visible>", ""
    ).replace(
        "<level>0</level>\n          <default>false</default>", "<level>2</level>"
    )
    with pytest.raises(UnmodelledCategory, match="advanced"):
        kodi_category_buttons(xml_level)


def test_backup_restore_verify_choice_runs_the_verifier(dmod, monkeypatch):
    """Picking the third entry must actually reach VERIFY_BACKUP_ARCHIVE.

    Without this, the label could be present and wired to nothing (or to the
    restore path, which is the dangerous mis-wire) and the options test above
    would still pass."""
    _wiz, backups, restores = _stub_wiz(monkeypatch)

    # The verify entry, then Back out of the (now looping) Backup/Restore menu.
    dmod.control.select_results = [2, -1]
    monkeypatch.setattr(
        sys,
        "argv",
        ["plugin://script.ezmaintenanceplusplus/", "1", "?action=backup_restore"],
    )
    monkeypatch.delitem(
        sys.modules, "ezm_backup_restore_verify_under_test", raising=False
    )
    spec = importlib.util.spec_from_file_location(
        "ezm_backup_restore_verify_under_test", DEFAULT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_backup_restore_verify_under_test"] = mod
    spec.loader.exec_module(mod)

    # No restore.path is configured, so the verifier's own first guard fires. That
    # dialog is the proof the verify branch ran, and it proves nothing destructive
    # ran instead.
    assert dmod.control.infoDialog_calls == ["Please Setup a Zip Files Location first"]
    assert restores == [], "the verify entry must never reach restoreFolder"
    assert backups == [], "the verify entry must never reach backup"


# --------------------------------------------------------------------------- #
# The Backup/Restore menu LOOPS (2026.07.19.8)
#
# Owner report: "the verify backup cancel button should take us back inside the
# backup/restore and not the root". Cancelling any sub-action used to end the
# branch, end the script, and drop her at Kodi's root menu.
# --------------------------------------------------------------------------- #
def _drive_backup_restore(monkeypatch, name):
    """Execute default.py's real backup_restore branch under the fixture's fakes."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["plugin://script.ezmaintenanceplusplus/", "1", "?action=backup_restore"],
    )
    monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(name, DEFAULT_PY)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _stub_wiz(monkeypatch):
    """Make the fixture's wiz stub RECORD; returns (module, backups, restores).

    The module itself comes from the dmod fixture so it keeps the real
    configured_path binding - only the two actions are swapped for recorders."""
    wiz = sys.modules["resources.lib.modules.wiz"]
    backups, restores = [], []
    monkeypatch.setattr(wiz, "backup", lambda *a, **k: backups.append(k))
    monkeypatch.setattr(wiz, "restoreFolder", lambda *a, **k: restores.append(True))
    return wiz, backups, restores


def test_cancelled_verify_re_presents_the_backup_restore_menu(dmod, monkeypatch):
    """THE REPORTED BUG. Cancel out of the verify flow -> back INSIDE Backup/Restore.

    Answers the menu with VERIFY, lets the verifier come back empty-handed (a folder
    with no zips in it: a cancel-shaped exit that restores nothing and takes no window
    away from us), then Backs out. The menu must have been presented TWICE: once to
    choose verify, once after it returned. One presentation means the script exited to
    the root menu, which is the defect.

    The path is CONFIGURED on purpose. Letting the verifier bail on its missing-path
    guard instead would exercise the opposite rule - that guard opens the settings
    window, and a menu that re-presents on top of it is its own defect - so this test
    would then be asserting the wrong outcome."""
    _stub_wiz(monkeypatch)
    dmod.control._settings["restore.path"] = "/fake/backups"
    dmod.xbmcvfs.listdir_result = ([], [])  # a real folder, no archives in it
    dmod.control.select_calls.clear()
    dmod.control.select_results = [2, -1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_verify_uut")

    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, (
        "after a cancelled verify the Backup/Restore menu must be re-presented, not "
        "abandoned to the root menu; dialogs seen: %r" % (dmod.control.select_calls,)
    )


def test_cancelled_restore_picker_re_presents_the_backup_restore_menu(
    dmod, monkeypatch
):
    """Same rule for RESTORE. restoreFolder() returns on every cancel path of its
    own (no zip location, empty folder, Back out of the file picker, "Cancel - don't
    restore anything"); whichever one fired, she lands back on this menu."""
    _wiz, _backups, restores = _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_results = [1, -1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_restore_uut")

    assert restores == [True], "the RESTORE entry must still reach restoreFolder once"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, dmod.control.select_calls


def test_cancelled_backup_mode_falls_back_to_the_menu_not_the_root(dmod, monkeypatch):
    """Backing out of the Full/Addons mode dialog is a sub-dialog cancel too.

    It must land on Backup/Restore, and it must NOT have taken a backup: a fallback
    that runs the default mode anyway would be far worse than the original bug."""
    _wiz, backups, _restores = _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_results = [0, -1, -1]  # BACKUP, cancel the mode dialog, Back

    _drive_backup_restore(monkeypatch, "ezm_br_loop_backupmode_uut")

    assert backups == [], "a cancelled mode dialog must not take a backup"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, (
        "cancelling the backup MODE dialog must fall back to Backup/Restore, not to "
        "the root menu; dialogs seen: %r" % (dmod.control.select_calls,)
    )


def test_cancelling_the_menu_itself_exits_exactly_once(dmod, monkeypatch):
    """The menu's own Back is the ONE exit. It must not re-present itself.

    A loop that re-presented on -1 too would be unescapable - a worse bug than the
    one being fixed, and the mirror-image failure of the original."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_results = [-1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_exit_uut")

    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "cancelling Backup/Restore must exit immediately; it was presented %d times"
        % len(menus)
    )


def test_an_unexpected_select_return_cannot_spin_the_menu(dmod, monkeypatch):
    """Anything outside 0/1/2/-1 exits rather than looping.

    selectDialog is a fake here and a real one could grow a new sentinel; a menu
    whose exit condition is `== -1` would spin forever on it, with no dialog on
    screen to cancel. The exit condition must be "not a known entry", not "-1".
    The fixture's call limit turns a regression here into a failure, not a hang."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_result = 99  # every call answers 99; nothing ever returns -1

    _drive_backup_restore(monkeypatch, "ezm_br_loop_unknown_uut")

    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "an unrecognised selectDialog return must break the loop, not spin it: %d "
        "presentations" % len(menus)
    )


def test_cancelling_the_zip_picker_re_presents_the_menu(dmod, monkeypatch):
    """The owner's LITERAL path: a configured zip location, a real list of archives,
    and Back out of the picker.

    The other verify test bails at the missing-zip-location guard, which is a
    cancel-SHAPED exit but not the one she hit - she had backups to pick from and
    pressed cancel on the list. Configure the location and stock the folder so the
    verifier reaches its real picker (default.py:302), then cancel THAT."""
    _stub_wiz(monkeypatch)
    dmod.control._settings["restore.path"] = "/fake/zips/"
    dmod.xbmcvfs.listdir_result = ([], ["kodi_backup_1.zip", "kodi_backup_2.zip"])
    dmod.control.select_calls.clear()
    # VERIFY, cancel the zip picker, then back out of the menu.
    dmod.control.select_results = [2, -1, -1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_picker_uut")

    picked = [c for c in dmod.control.select_calls if "kodi_backup_1.zip" in c]
    assert picked, (
        "the verifier never reached its zip picker, so this test is not exercising "
        "the reported path: %r" % (dmod.control.select_calls,)
    )
    assert dmod.control.infoDialog_calls == [], "no guard dialog should have fired"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, (
        "cancelling the zip picker must land back inside Backup/Restore: %r"
        % (dmod.control.select_calls,)
    )


def test_a_restore_that_ran_ends_the_menu(dmod, monkeypatch):
    """A restore that REACHED THE BOX must not re-present the menu.

    Every terminal path of wiz.restore() ends in ui.ask_restart(), and accepting it
    fires an ASYNC `Quit` - so Kodi is already tearing down when restoreFolder()
    returns. Re-presenting would open a modal into a shutting-down message pump.
    Declining leaves the box half-applied, which is no state to offer BACKUP in.
    wiz.restore() publishes ezm_restore_verdict on every path that touched the box;
    that property is the signal, since restoreFolder() returns None either way."""
    wiz, _backups, restores = _stub_wiz(monkeypatch)

    def _restore_that_ran(*a, **k):
        restores.append(True)
        # exactly what wiz.restore() does at wiz.py:1769
        sys.modules["xbmcgui"].Window(10000).setProperty(
            "ezm_restore_verdict", "complete"
        )

    wiz.restoreFolder = _restore_that_ran
    dmod.control.select_calls.clear()
    # Answer RESTORE, then keep answering RESTORE. If the menu re-presents, the stub
    # runs another restore and the call bound trips - a failure, not a hang.
    dmod.control.select_result = 1

    _drive_backup_restore(monkeypatch, "ezm_br_loop_restore_ran_uut")

    assert restores == [True], "the restore must run exactly once"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "a restore that ran must END the menu, not re-present it: %d presentations"
        % len(menus)
    )


def test_a_cancelled_restore_still_re_presents_the_menu(dmod, monkeypatch):
    """The mirror of the test above, and the owner's case for RESTORE.

    A cancel at the file picker or the how-dialog never reaches wiz.restore(), so
    nothing is published and the menu must stay open. Without this, the exit above
    could be implemented as "always exit after RESTORE" and still look correct."""
    _wiz, _backups, restores = _stub_wiz(monkeypatch)  # restoreFolder publishes nothing
    dmod.control.select_calls.clear()
    dmod.control.select_results = [1, -1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_restore_cancelled_uut")

    assert restores == [True]
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, (
        "a CANCELLED restore publishes no verdict and must come back to the menu: %r"
        % (dmod.control.select_calls,)
    )


def test_a_stale_verdict_from_an_earlier_restore_does_not_end_the_menu(
    dmod, monkeypatch
):
    """The verdict is cleared before each restore, not just read after it.

    Kodi window properties outlive the script. Without the clear, one restore would
    poison every later RESTORE pick for the rest of the Kodi session: the stale
    property would still be set and the menu would exit on a restore the user
    actually cancelled - the reported bug, reintroduced through the back door."""
    _wiz, _backups, restores = _stub_wiz(monkeypatch)  # a cancel: publishes nothing
    sys.modules["xbmcgui"].Window(10000).setProperty("ezm_restore_verdict", "complete")
    dmod.control.select_calls.clear()
    dmod.control.select_results = [1, -1]

    _drive_backup_restore(monkeypatch, "ezm_br_loop_stale_verdict_uut")

    assert restores == [True]
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 2, (
        "a verdict left over from an EARLIER restore must not end this menu: %r"
        % (dmod.control.select_calls,)
    )


def test_the_menu_stops_when_kodi_is_shutting_down(dmod, monkeypatch):
    """`Quit` is asynchronous, so the script outlives it. The menu must notice.

    ui.restart() calls executebuiltin("Quit") WITHOUT the wait flag - this codebase
    documents the blocking form as executebuiltin(..., True) (wiz.py:867) - so Kodi's
    teardown runs while this Python is still alive. That is not theoretical: defect A
    is a CApplication::Stop settings flush landing after the add-on returned. Opening
    a modal into a shutting-down message pump is the race this guard exists to avoid,
    and Monitor.waitForAbort is Kodi's own signal for it."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_result = 2  # VERIFY forever; only the abort can stop this
    # A CONFIGURED path, so the verifier does not bail to the settings window: that
    # is the other exit, and it would stop the menu for a reason this test is not
    # about, leaving the shutdown check unexercised.
    dmod.control._settings["restore.path"] = "/fake/backups"
    dmod.xbmcvfs.listdir_result = ([], [])
    dmod.xbmc._abort = True

    _drive_backup_restore(monkeypatch, "ezm_br_loop_abort_uut")

    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "the menu must not re-present while Kodi is shutting down: %d presentations"
        % len(menus)
    )
    assert dmod.xbmc._abort_waits, "the shutdown check never ran"


def test_the_menu_stops_when_a_sub_action_opened_settings(dmod, monkeypatch):
    """All three sub-actions bail to Kodi's Settings window when their path setting
    is unconfigured: wiz.backup on download.path (wiz.py:337), wiz.restoreFolder
    (wiz.py:684) and VERIFY_BACKUP_ARCHIVE on restore.path.

    Addon.OpenSettings is ASYNC too, so the sub-action returns immediately and the
    menu would otherwise drop a modal select dialog on top of the settings window the
    user was just sent to - a new defect, created by the loop itself."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_result = 2  # VERIFY forever; only the settings check stops it

    # VERIFY_BACKUP_ARCHIVE's own guard fires (no restore.path) and calls
    # control.openSettings; model Kodi honouring that async builtin.
    def _open_settings(*a, **k):
        dmod.control.openSettings_calls.append(a)
        dmod.xbmc._active_window = "addonsettings"

    dmod.control.openSettings = _open_settings

    _drive_backup_restore(monkeypatch, "ezm_br_loop_settings_uut")

    assert dmod.control.openSettings_calls, "the settings bail never happened"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "the menu must not re-present on top of the Settings window: %d presentations"
        % len(menus)
    )


def test_the_menu_stops_even_when_the_settings_window_paints_LATE(dmod, monkeypatch):
    """THE RACE. The guard must not depend on how fast the settings window paints.

    _safe_to_re_present used to settle 0.25s and then ask Kodi whether the settings
    window was active. Right above it, openSettingsTab polls that SAME window for up to
    FIVE SECONDS, which is this codebase's own measured statement of how long an
    appliance can take. When the window lost that race the guard answered "safe", the
    menu re-presented, and a modal select dialog landed on top of the settings window
    the user had just been sent to - the exact defect the guard exists to prevent.

    So this fake keeps the window inactive for far longer than the guard is willing to
    wait, which is the real Fire TV / Apple TV timing. The signal has to be something
    that cannot be late: control.openSettings COUNTS its calls, and the count is
    already final when the sub-action returns."""
    _stub_wiz(monkeypatch)
    dmod.control.select_calls.clear()
    dmod.control.select_result = 2  # VERIFY forever; only the guard can stop this

    # The builtin fires; the window is still painting. Nothing sets _active_window,
    # so every probe the guard makes comes back "no settings window here" - exactly
    # what a real box reports for the first few hundred milliseconds.
    probes = []
    _real_cond = dmod.xbmc.getCondVisibility

    def _cond(cond):
        if cond == "Window.IsActive(addonsettings)":
            probes.append(cond)
            return False
        return _real_cond(cond)

    dmod.xbmc.getCondVisibility = _cond

    _drive_backup_restore(monkeypatch, "ezm_br_loop_late_window_uut")

    assert dmod.control.openSettings_calls, "the settings bail never happened"
    menus = [c for c in dmod.control.select_calls if c and c[0].startswith("Backup")]
    assert len(menus) == 1, (
        "a settings bail must end the menu even when the window has not painted yet; "
        "the menu re-presented %d times on top of it" % len(menus)
    )


def test_all_three_bails_land_on_the_backup_restore_tab(dmod, monkeypatch):
    """ "Not set" plus a click must land ON the tab that holds the path setting.

    All three bails tell her to set a path: wiz.backup (download.path),
    wiz.restoreFolder (restore.path) and VERIFY_BACKUP_ARCHIVE (restore.path). A plain
    control.openSettings() opens the settings window on its FIRST category, Maintenance
    - told to set a path, then dropped on the wrong tab with nothing on screen saying
    which one. They must go through the tab-aware entry point, with the Backup/Restore
    index.

    wiz's two bails are asserted where they live (test_ezmaintenanceplusplus_wiz.py,
    against the real wiz module); this covers the one in default.py, and that the
    dialog still says WHAT is missing, and that the window opens exactly once."""
    _stub_wiz(monkeypatch)
    dmod.control._settings.update({"destination": "0", "restore.path": "   "})

    dmod.mod.VERIFY_BACKUP_ARCHIVE()

    assert dmod.control.openSettingsTab_calls == [
        dmod.control.SETTINGS_TAB_BACKUP_RESTORE
    ], "the unset-path bail must open the Backup/Restore TAB: %r" % (
        dmod.control.openSettingsTab_calls,
    )
    assert dmod.control.infoDialog_calls == ["Please Setup a Zip Files Location first"]
    assert len(dmod.control.openSettings_calls) == 1, (
        "the settings window must open once, not once per entry point: %r"
        % (dmod.control.openSettings_calls,)
    )


def test_the_guard_probes_are_best_effort_and_never_break_the_menu(dmod):
    """A guard that raises must not take the menu down with it.

    These probes are diagnostics, not the feature. If Monitor or getCondVisibility
    throws (a fake, an odd platform, a Kodi version without the property), staying on
    the menu is the behaviour the owner asked for, so the failure must be swallowed
    and answered "safe"."""

    class _Boom:
        def waitForAbort(self, timeout=0):
            raise RuntimeError("no monitor here")

    assert dmod.mod._safe_to_re_present(monitor=_Boom()) is True
    # And the verdict read must answer False (keep the menu) when it cannot read.
    dmod.mod.xbmcgui.Window = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    assert dmod.mod._restore_verdict() is False
    dmod.mod._clear_restore_verdict()  # must not raise


def test_the_menu_loop_is_bounded_by_the_stub(dmod, monkeypatch):
    """Self-test of the guard above: prove the fixture BOUNDS a runaway loop.

    Without this, "the suite did not hang" is an unverified claim about the stub. A
    branch that ignored the exit condition would hang forever under an unbounded
    stub; under this one it raises. Driven against the real dialog options so it
    fails if the stub's limit is ever removed."""
    dmod.control.select_limit = 5
    dmod.control.select_result = 0  # never -1: a loop keying only on -1 never exits
    with pytest.raises(AssertionError, match="not terminating"):
        while True:
            dmod.control.selectDialog(
                ["Backup", "Restore", "Verify Backup Archive", "Settings"]
            )


def test_verify_entry_keeps_its_description_verbatim(dmod):
    """The plain-language copy survives the move.

    The Backup/Restore select dialog has no plot slot, so the description now lives
    beside the entry in default.py. It is good copy and it is not to be reworded or
    dropped as a casualty of the menu move."""
    src = (ADDON_ROOT / "default.py").read_text()
    # Flatten comment markers and wrapping so a reflow cannot fail this test, while
    # a reworded or truncated sentence still does.
    flat = " ".join(line.strip().lstrip("#").strip() for line in src.splitlines())
    flat = " ".join(flat.split())
    assert (
        "Read-only check of a backup zip: entry count, manifest, failed list, "
        "IPTV data, top-level layout. Restores nothing." in flat
    ), "the verify item's description must be preserved verbatim"


def test_retired_tools_category_is_a_silent_no_op(dmod, monkeypatch):
    """A stale favourite/widget pointing at the deleted category must land on a
    benign no-op, never the unknown-action path and never a traceback.

    Mirrors the retired purge action's treatment (2026.07.19.5). default.py routes
    at IMPORT time off sys.argv[2], so the honest test imports it with the retired
    querystring and asserts a clean load: no exception, no dialog of any kind."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["plugin://script.ezmaintenanceplusplus/", "1", "?action=tools"],
    )
    monkeypatch.delitem(sys.modules, "ezm_retired_tools_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_retired_tools_under_test", DEFAULT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_retired_tools_under_test"] = mod

    spec.loader.exec_module(mod)  # must not raise

    ui = sys.modules["resources.lib.modules.ui"]
    assert getattr(ui, "error_calls", []) == [], "the retired category must not error"
    assert getattr(ui, "done_calls", []) == [], (
        "the retired category must be SILENT - no dialog at all"
    )
    assert dmod.xbmcplugin.items == [], "the retired category must list nothing"


def test_retired_tools_category_is_explicitly_routed(dmod):
    """Pairs with the behavioural test above, which cannot stand alone.

    Mutation-checked 2026-07-19: DELETING the `elif action == "tools"` branch keeps
    the behavioural test GREEN, because the unknown-action fallthrough is silent
    too. Silence is not the property we want - an explicit route is. Without the
    branch a stale bookmark lands in the unknown-action path and renders an empty
    directory (a visible dead end), which the Kodi fakes cannot distinguish from a
    deliberate no-op. This is the same source-level pin the retired purge action
    carries, and for the same reason."""
    src = (ADDON_ROOT / "default.py").read_text()
    assert 'elif action == "tools":' in src, (
        "the retired category must still be routed, so an old bookmark cannot fall "
        "through to the unknown-action path"
    )
    assert "def TOOLS(" not in src, "the TOOLS() builder must be deleted, not kept"


def test_retired_purge_action_is_a_silent_no_op(dmod):
    """A stale favourite/widget still pointing at action=purge_stale_tvos_keys
    must land on a benign no-op, never an unknown-action path or a traceback."""
    assert not hasattr(dmod.mod, "PURGE_STALE_TVOS_KEYS")
    src = (ADDON_ROOT / "default.py").read_text()
    assert 'elif action == "purge_stale_tvos_keys":' in src, (
        "the retired action must still be routed, so an old bookmark cannot fall "
        "through to the unknown-action path"
    )


def test_retired_purge_action_executes_without_raising(dmod, monkeypatch):
    """BEHAVIOURAL proof of the no-op, not a grep of the source.

    The source assertion above pins that a branch exists; it cannot prove that
    ACTUALLY dispatching the retired action is harmless. default.py routes at
    IMPORT time off sys.argv[2], so the only honest test is to import it with
    the retired querystring and assert the module loads clean: no exception, no
    error dialog, no dialog of any kind. A grandmother's stale home-screen
    shortcut must do nothing visible, not raise.
    """
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "plugin://script.ezmaintenanceplusplus/",
            "1",
            "?action=purge_stale_tvos_keys",
        ],
    )
    monkeypatch.delitem(sys.modules, "ezm_retired_action_under_test", raising=False)
    spec = importlib.util.spec_from_file_location(
        "ezm_retired_action_under_test", DEFAULT_PY
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ezm_retired_action_under_test"] = mod

    spec.loader.exec_module(mod)  # must not raise

    ui = sys.modules["resources.lib.modules.ui"]
    assert getattr(ui, "error_calls", []) == [], "the retired action must not error"
    assert getattr(ui, "done_calls", []) == [], (
        "the retired action must be SILENT - no dialog at all"
    )


# --------------------------------------------------------------------------- #
# analyze_backup_zip - the pure, read-only inspection
# --------------------------------------------------------------------------- #
def test_analyze_manifest_present_with_failed_list(dmod, tmp_path):
    zp = _make_zip(
        tmp_path / "b.zip",
        {
            "userdata/guisettings.xml": "<settings/>",
            "userdata/addon_data/pvr.iptvsimple/settings.xml": "<settings/>",
            "addons/plugin.video.x/addon.xml": "<addon/>",
            "media/splash.png": b"\x89PNG",
            "stray.txt": "x",
        },
        manifest={
            "created": "2026-07-16",
            "source_os": "tvOS",
            "entries": 5,
            "failed": ["userdata/keymaps/gen.xml", "userdata/rsstranslator.xml"],
        },
    )
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["total_entries"] == 6  # 5 members + the manifest itself
    assert r["manifest_present"] is True
    assert r["manifest_failed"] == [
        "userdata/keymaps/gen.xml",
        "userdata/rsstranslator.xml",
    ]
    assert r["iptv_present"] is True
    assert r["composition"] == {"userdata": 2, "addons": 1, "media": 1, "other": 2}


def test_analyze_manifest_absent(dmod, tmp_path):
    zp = _make_zip(tmp_path / "b.zip", {"userdata/guisettings.xml": "<s/>"})
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["manifest_present"] is False
    assert r["manifest_failed"] == []


def test_analyze_manifest_present_empty_failed(dmod, tmp_path):
    zp = _make_zip(
        tmp_path / "b.zip",
        {"userdata/guisettings.xml": "<s/>"},
        manifest={"created": "x", "source_os": "android", "entries": 1, "failed": []},
    )
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["manifest_present"] is True
    assert r["manifest_failed"] == []


def test_analyze_corrupt_manifest_json_does_not_crash(dmod, tmp_path):
    zp = _make_zip(
        tmp_path / "b.zip", {"backup_manifest.json": "{not json", "userdata/a": "x"}
    )
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["manifest_present"] is True
    assert r["manifest_failed"] == []


def test_analyze_iptv_absent(dmod, tmp_path):
    zp = _make_zip(
        tmp_path / "b.zip",
        {
            "userdata/addon_data/plugin.video.x/settings.xml": "<s/>",
            "addons/pvr.iptvsimple/addon.xml": "<a/>",  # the ADD-ON, not its data
        },
    )
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["iptv_present"] is False


def test_analyze_iptv_detected_in_userdata_anchored_zip(dmod, tmp_path):
    # A userdata-anchored backup carries addon_data/ at the archive root.
    zp = _make_zip(
        tmp_path / "b.zip",
        {"addon_data/pvr.iptvsimple/instance-settings-1.xml": "<s/>"},
    )
    r = dmod.mod.analyze_backup_zip(zp)
    assert r["iptv_present"] is True
    assert r["composition"]["other"] == 1  # addon_data is not a home-level bucket


def test_analyze_not_a_zip_raises(dmod, tmp_path):
    bad = tmp_path / "not_a_zip.zip"
    bad.write_bytes(b"this is not a zip archive")
    with pytest.raises(Exception):
        dmod.mod.analyze_backup_zip(str(bad))


def test_format_backup_report_lines(dmod):
    report = {
        "total_entries": 12,
        "manifest_present": True,
        "manifest_failed": ["a", "b", "c", "d", "e", "f", "g"],
        "iptv_present": True,
        "composition": {"userdata": 7, "addons": 3, "media": 1, "other": 1},
    }
    text = dmod.mod.format_backup_report(report, "kodi_full.zip")
    assert "Backup archive: kodi_full.zip" in text
    assert "Total entries: 12" in text
    assert "Manifest (backup_manifest.json): present" in text
    assert "Manifest failed items (7): a, b, c, d, e, and 2 more" in text
    assert "IPTV (pvr.iptvsimple) data: yes" in text
    assert "Top level: userdata=7, addons=3, media=1, other=1" in text
    # missing manifest renders loudly
    report["manifest_present"] = False
    report["manifest_failed"] = []
    report["iptv_present"] = False
    text = dmod.mod.format_backup_report(report)
    assert "Manifest (backup_manifest.json): MISSING" in text
    assert "IPTV (pvr.iptvsimple) data: no" in text
    assert "failed items: none" not in text  # no manifest, no failed line


# --------------------------------------------------------------------------- #
# VERIFY_BACKUP_ARCHIVE - the picker flow
# --------------------------------------------------------------------------- #
def test_verify_reports_on_local_zip(dmod, tmp_path):
    zp = _make_zip(
        tmp_path / "kodi_full.zip",
        {"userdata/guisettings.xml": "<s/>"},
        manifest={"created": "x", "source_os": "tvOS", "entries": 1, "failed": []},
    )
    dmod.control._settings["restore.path"] = str(tmp_path)
    dmod.xbmcvfs.listdir_result = ([], ["kodi_full.zip", "notes.txt"])
    dmod.control.select_result = 0
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    # only .zip files were offered
    assert dmod.control.select_calls == [["kodi_full.zip"]]
    assert len(dmod.ui.done_calls) == 1
    msg = dmod.ui.done_calls[0]
    assert "Backup archive: kodi_full.zip" in msg
    assert "Total entries: 2" in msg
    assert "Manifest (backup_manifest.json): present" in msg
    # read-only: the picked zip still exists untouched
    assert Path(zp).exists()


def test_verify_remote_zip_staged_and_cleaned(dmod, tmp_path):
    src_zip = _make_zip(
        tmp_path / "payload.zip",
        {"addon_data/pvr.iptvsimple/settings.xml": "<s/>"},
    )
    staged = tmp_path / "staged_verify.zip"
    dmod.control._settings["restore.path"] = "nfs://mini/KodiShare/backups"
    dmod.xbmcvfs.listdir_result = ([], ["kodi_full.zip"])
    dmod.xbmcvfs._temp_map["special://temp/ezmpp_verify_kodi_full.zip"] = str(staged)
    dmod.control.select_result = 0
    dmod.ui.copy_payload_src = src_zip
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    # fetched over VFS from the share, into the temp sidecar
    assert dmod.ui.copy_calls == [
        (
            "nfs://mini/KodiShare/backups/kodi_full.zip",
            "special://temp/ezmpp_verify_kodi_full.zip",
        )
    ]
    assert len(dmod.ui.done_calls) == 1
    assert "IPTV (pvr.iptvsimple) data: yes" in dmod.ui.done_calls[0]
    # the staged temp copy is removed after analysis
    assert not staged.exists()


def test_verify_remote_fetch_cancel_is_silent(dmod):
    dmod.control._settings["restore.path"] = "nfs://mini/KodiShare/backups"
    dmod.xbmcvfs.listdir_result = ([], ["kodi_full.zip"])
    dmod.control.select_result = 0
    dmod.ui.copy_result = dmod.ui.COPY_CANCELLED
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    assert dmod.ui.done_calls == []
    assert dmod.ui.error_calls == []


def test_verify_without_restore_path_routes_to_settings(dmod):
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    assert dmod.control.infoDialog_calls == ["Please Setup a Zip Files Location first"]
    assert len(dmod.control.openSettings_calls) == 1
    assert dmod.ui.done_calls == []


def test_verify_no_zips_in_folder_reports_error(dmod, tmp_path):
    dmod.control._settings["restore.path"] = str(tmp_path)
    dmod.xbmcvfs.listdir_result = ([], ["notes.txt"])
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    assert len(dmod.ui.error_calls) == 1
    assert "No backup zips found" in dmod.ui.error_calls[0]


def test_verify_picker_cancel_does_nothing(dmod, tmp_path):
    dmod.control._settings["restore.path"] = str(tmp_path)
    dmod.xbmcvfs.listdir_result = ([], ["kodi_full.zip"])
    dmod.control.select_result = -1
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    assert dmod.ui.done_calls == []
    assert dmod.ui.error_calls == []


def test_verify_corrupt_zip_reports_error(dmod, tmp_path):
    (tmp_path / "kodi_full.zip").write_bytes(b"not a zip at all")
    dmod.control._settings["restore.path"] = str(tmp_path)
    dmod.xbmcvfs.listdir_result = ([], ["kodi_full.zip"])
    dmod.control.select_result = 0
    dmod.mod.VERIFY_BACKUP_ARCHIVE()
    assert len(dmod.ui.error_calls) == 1
    assert "Could not read that zip" in dmod.ui.error_calls[0]
    assert dmod.ui.done_calls == []


def test_freshstart_wipe_runs_and_prompts_shutdown_not_false_failed(dmod, monkeypatch):
    """Regression (QA 2026-07-17): onetap._wipe changed from a 3-tuple to a 4-tuple;
    FRESHSTART still unpacked 3, so it WIPED the box then raised on the unpack, was
    swallowed, and falsely told the user 'the wipe did not run' WITHOUT restarting -
    a wiped box left stranded. Let the wipe run and assert honest completion + the exit.

    2026-07-21: Fresh Start requires stock Estuary (so post-wipe dialogs still render)
    and ends with ui.ask_terminate(), which is now an acknowledge-then-exit NOTICE, not
    a Shut down / Later choice - it always hard-exits via ui.terminate() (os._exit), so
    the exit flush cannot re-dirty the slate and Kodi can never outlive the wipe that
    deleted the databases it holds open. The completion copy must say the box will
    close/reopen, never 'restart'."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")
    wiped = {"v": False}

    def _wipe(home, excludes, keep=None, progress=None):
        wiped["v"] = True
        return (5, 2, 0, [])  # (files, keys, failed, leftovers) - the new 4-tuple

    onetap._wipe = _wipe
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    # Guard: the live skin must live OUTSIDE the wipe root to survive (APK-resident).
    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    prompts = []
    dmod.ui.ask_terminate = lambda status="", **k: prompts.append(status) or False

    dmod.mod.FRESHSTART()

    assert wiped["v"] is True, "the wipe must actually run"
    assert not any("FAILED" in m for m in dmod.ui.done_calls), dmod.ui.done_calls
    assert len(prompts) == 1, (
        "a wiped box MUST be driven to the Shut down / Later prompt"
    )
    # Honest appliance copy: the box closes and is reopened, it does NOT self-restart.
    assert "restart" not in prompts[0].lower(), prompts[0]


def test_freshstart_that_dies_mid_wipe_still_exits_and_says_so(dmod, monkeypatch):
    """QA finding 2026-07-21. The one Fresh Start path that still left Kodi ALIVE on a
    wiped tree, and lied about it.

    _wipe deletes the POSIX tree first and sweeps NSUserDefaults keys LAST, so a raise
    from the key pass (tvOS) arrives with every file - including the databases Kodi
    holds open - ALREADY GONE. The old code swallowed it, left wipe_failed at None,
    printed "the wipe did not run. Nothing was removed." and RETURNED without
    terminating. Both halves are wrong: the message is false, and staying up on a tree
    whose open databases were just unlinked is exactly the SIGABRT this release exists
    to prevent (the office Fire TV, 2026-07-21).
    """
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")
    reached = {"v": False}

    def _wipe(home, excludes, keep=None, progress=None):
        reached["v"] = True  # the destructive pass BEGAN
        raise RuntimeError("NSUserDefaults key sweep blew up after the POSIX delete")

    onetap._wipe = _wipe
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    prompts = []
    dmod.ui.ask_terminate = lambda status="", **k: prompts.append(status) or False

    dmod.mod.FRESHSTART()

    assert reached["v"] is True
    assert len(prompts) == 1, (
        "a box whose wipe BEGAN must be driven to terminate - it cannot be left "
        "running on unlinked databases"
    )
    assert not any("did not run" in m.lower() for m in dmod.ui.done_calls), (
        "must not claim nothing was removed when the wipe had already started: %s"
        % dmod.ui.done_calls
    )
    assert not any("nothing was removed" in m.lower() for m in prompts), prompts


def test_freshstart_that_never_started_stays_up_and_says_nothing_was_removed(
    dmod, monkeypatch
):
    """The counterpart. If the wipe genuinely never began (import error, or a raise
    before the first delete) Kodi is untouched, so it is SAFE to stay up - and killing
    the app there would be a gratuitous shutdown over a no-op."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")

    def _excludes():
        raise RuntimeError("failed before any delete")

    onetap._wipe = lambda *a, **k: (0, 0, 0, [])
    onetap._wipe_excludes = _excludes
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    prompts = []
    dmod.ui.ask_terminate = lambda status="", **k: prompts.append(status) or False

    dmod.mod.FRESHSTART()

    assert prompts == [], "nothing was destroyed, so do not kill Kodi"
    assert any("did not run" in m.lower() for m in dmod.ui.done_calls), (
        dmod.ui.done_calls
    )


def test_freshstart_resets_the_cache_buffer_after_the_wipe(dmod, monkeypatch):
    """A wiped box lands on Kodi's default video cache buffer (owner decision
    2026-07-31: no inherited number survives a wipe or a restore, because the fleet
    mixes device classes whose right buffer differs).

    On this path the reset is MOSTLY redundant - the wipe removes guisettings.xml and
    Kodi's own default is already 20 MB - and the owner asked for the guarantee to be
    explicit and TESTABLE rather than incidental. It is not entirely redundant either,
    which is why it runs AFTER the wipe rather than before: Kodi's LIVE store still
    holds this box's old buffer (a wipe removes files, not the running process's
    memory), so any flush writes it straight back into the fresh file, and a wipe that
    left guisettings.xml behind leaves the old number sitting in it.

    No prompt is involved and none may appear: the per-device recommendation stays on
    demand under Video Cache Buffer."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")

    def _wipe(home, excludes, keep=None, progress=None):
        dmod.tools.event_log.append("wipe")
        return (5, 2, 0, [])

    onetap._wipe = _wipe
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    dmod.ui.ask_terminate = lambda status="", **k: False

    dmod.mod.FRESHSTART()

    assert dmod.tools.event_log == ["wipe", "reset"], (
        "Fresh Start must reset the cache buffer exactly once, AFTER the wipe; the "
        "event log was %r. Resetting before the wipe writes a value the wipe then "
        "deletes, and leaves the old number in a guisettings.xml the wipe failed to "
        "remove." % (dmod.tools.event_log,)
    )


def test_freshstart_that_died_mid_wipe_still_resets_the_cache_buffer(dmod, monkeypatch):
    """The destructive pass BEGAN, so the box is going to a clean state whether or not
    the wipe finished - and the file layer is exactly what a half-finished wipe may
    have left holding the old buffer. The reset must not be gated on the wipe
    SUCCEEDING, only on it having started."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")

    def _wipe(home, excludes, keep=None, progress=None):
        dmod.tools.event_log.append("wipe")
        raise RuntimeError("NSUserDefaults key sweep blew up after the POSIX delete")

    onetap._wipe = _wipe
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    dmod.ui.ask_terminate = lambda status="", **k: False

    dmod.mod.FRESHSTART()

    assert dmod.tools.event_log == ["wipe", "reset"], dmod.tools.event_log


def test_freshstart_that_never_started_leaves_the_cache_buffer_alone(dmod, monkeypatch):
    """The counterpart, and the reason the reset is gated rather than unconditional.

    If the wipe genuinely never began (an import error, a raise before the first
    delete) the box is UNTOUCHED. Changing the user's cache buffer there would be an
    uninstructed mutation on a run that did nothing else - the same reasoning that
    keeps Kodi alive on this path instead of terminating it."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")

    def _excludes():
        raise RuntimeError("failed before any delete")

    onetap._wipe = lambda *a, **k: (0, 0, 0, [])
    onetap._wipe_excludes = _excludes
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/apk/assets/skin.estuary" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    dmod.ui.ask_terminate = lambda status="", **k: False

    dmod.mod.FRESHSTART()

    assert dmod.tools.event_log == [], (
        "nothing was destroyed, so nothing may be changed: %r" % (dmod.tools.event_log,)
    )


def test_freshstart_refused_from_a_custom_skin_changes_nothing(dmod, monkeypatch):
    """The refusal path must not reset the buffer either - it never reaches the wipe."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")
    onetap._wipe = lambda *a, **k: (0, 0, 0, [])
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/home/.kodi/addons/skin.estuary.pov" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True

    dmod.mod.FRESHSTART()

    assert dmod.tools.event_log == [], dmod.tools.event_log


def test_freshstart_requires_stock_estuary_skin(dmod, monkeypatch):
    """Fresh Start deletes the ACTIVE skin's files; only stock Estuary survives the wipe
    (it is APK-resident, outside special://home), so from any other skin the post-wipe
    completion prompt could not render. FRESHSTART MUST abort (error, no wipe, no exit
    prompt) unless the live skin is skin.estuary."""
    import sys
    import types as _t

    onetap = _t.ModuleType("resources.lib.modules.onetap")
    wiped = {"v": False}

    def _wipe(home, excludes, keep=None, progress=None):
        wiped["v"] = True
        return (0, 0, 0, [])

    onetap._wipe = _wipe
    onetap._wipe_excludes = lambda: set()
    onetap.keep_addon_db = lambda: set()
    monkeypatch.setitem(sys.modules, "resources.lib.modules.onetap", onetap)
    setattr(sys.modules["resources.lib.modules"], "onetap", onetap)

    # A custom skin installed UNDER the wipe root (special://home/addons) - it would be
    # deleted mid-wipe, so Fresh Start must refuse.
    dmod.mod.HOME = "/home/.kodi"
    dmod.mod.translatePath = lambda p: (
        "/home/.kodi/addons/skin.estuary.pov" if p == "special://skin/" else p
    )
    dmod.ui.confirm_wipe = lambda *a, **k: True
    prompts = []
    dmod.ui.ask_terminate = lambda status="", **k: prompts.append(status) or False

    dmod.mod.FRESHSTART()

    assert wiped["v"] is False, "the wipe must NOT run from a skin under the wipe root"
    assert prompts == [], "no completion/terminate prompt when the run is refused"
    assert any("estuary" in m.lower() for m in dmod.ui.error_calls), dmod.ui.error_calls


# --------------------------------------------------------------------------- #
# CreateDir art keys. Regression test for the py2 -> py3 port bug found
# 2026-07-18 by looking at an actual screen: every menu row rendered Kodi's
# DefaultVideo.png (the old reel-to-reel movie camera) instead of the add-on's
# own shield icon.
#
# The PY2 branch passes thumbnailImage= as a ListItem CONSTRUCTOR kwarg, which
# really does set the thumbnail. The py3 rewrite turned that kwarg NAME into a
# setArt KEY, and there is no "thumbnailImage" art key, so it was silently
# dropped. With no thumb and setInfo(type="Video"), Kodi fell back to the video
# default. Every setArt fake in this suite is `lambda *a, **k: None`, which is
# precisely why nothing caught it.
# --------------------------------------------------------------------------- #

_VALID_ART_KEYS = {
    "thumb",
    "poster",
    "banner",
    "fanart",
    "clearart",
    "clearlogo",
    "landscape",
    "icon",
}


def _capture_art(dmod):
    """Call CreateDir with a recording ListItem and return the merged art dict."""
    import sys as _sys

    calls = []
    real = _sys.modules["xbmcgui"].ListItem

    class _Recording(real):
        def setArt(self, d):
            calls.append(dict(d))

    _sys.modules["xbmcgui"].ListItem = _Recording
    try:
        dmod.mod.CreateDir("Tools", "url", "tools", "ICON.png", "FAN.jpg", "")
    finally:
        _sys.modules["xbmcgui"].ListItem = real
    merged = {}
    for d in calls:
        merged.update(d)
    return merged


def test_createdir_sets_poster_without_clobbering_folder_glyphs(dmod):
    """The add-on icon must land on 'poster', NOT 'thumb'.

    poster feeds the skin's left info panel (View_50_List.xml:302 binds
    $VAR[IconWallPosterVar], whose chain is poster -> thumb, with a hardcoded
    fallback="DefaultVideo.png"). thumb would ALSO satisfy that panel, but the list
    view prefers thumb over icon, so setting thumb wipes out the per-row folder
    glyphs. Both halves matter: shield in the panel, folders in the list."""
    art = _capture_art(dmod)
    assert "poster" in art, (
        "no 'poster' art key set, so the left info panel falls back to "
        "DefaultVideo.png (the reel-to-reel camera): %r" % (art,)
    )
    assert art["poster"] == "ICON.png"
    assert "thumb" not in art, (
        "do NOT set thumb here: the list view prefers thumb over icon, so setting it "
        "replaces the per-row FOLDER glyphs with the add-on icon. Regression 2026-07-18."
    )
    assert art["icon"] == "DefaultFolder.png", "the per-row folder glyph must survive"


def test_createdir_uses_no_invalid_art_keys(dmod):
    """'thumbnailImage' is a constructor kwarg, never an art key. Kodi ignores it
    silently, which is how the wrong icon shipped unnoticed."""
    art = _capture_art(dmod)
    bogus = set(art) - _VALID_ART_KEYS
    assert not bogus, "invalid setArt keys are silently ignored by Kodi: %s" % (
        sorted(bogus),
    )
