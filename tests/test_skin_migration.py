# -*- coding: utf-8 -*-
"""The skin migration (resources/lib/modules/skinmigrate.py), its service.py
wiring, and the restore-side mapping in wiz.py.

Owner decision 2026-09-26: Estuary POV is renamed Estuary++ with a new add-on
id, and every box moves itself across at boot: install the new skin through
Kodi's own installer (so the origin row is the repository's), copy the old
skin's settings.xml to the new id's folder, switch live and answer the
keep-skin dialog, and on the next start remove the old skin. Restore maps the
old id to the new one; the boot check accepts either.

Rig: the two-layer storage fake (tests/fake_kodi_storage.py) for the box, a
dict of installed add-ons, a set of installable ids standing in for the hub's
index, and Kodi's two confirms (the download prompt and the keep-skin
countdown) modelled as one on-screen yes/no dialog that the real module has
to find by text and answer by walking to Yes. A fake clock drives every
timeout so no test waits on the real one.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from pathlib import Path

import pytest

from fake_kodi_storage import FakeKodiStorage, make_modules
from test_ezmaintenanceplusplus_wiz import wiz  # noqa: F401 - registers the fixture
from test_service_pov_reuse_invoker import _Env, _load_service, _nsud_stub

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parent / "script.ezmaintenanceplusplus"
MODULES = ADDON_ROOT / "resources" / "lib" / "modules"

OLD = "skin.estuary.pov"
NEW = "skin.estuary.plusplus"
STOCK = "skin.estuary"

KEEP_TEXT = "Would you like to keep this change?"
DOWNLOAD_TEXT = "Would you like to download this add-on?"

OLD_SETTINGS = (
    '<settings version="2">\n'
    '    <setting id="pov_menu_defaults" type="bool">true</setting>\n'
    '    <setting id="pov_home_1_label" type="string">Movies, HD</setting>\n'
    '    <setting id="pov_home_2_label" type="string">Live TV</setting>\n'
    "</settings>\n"
).encode()


# --------------------------------------------------------------------------- #
# The rig
# --------------------------------------------------------------------------- #
class _Rig:
    def __init__(self, store, platform):
        self.store = store
        self.platform = platform
        self.addons = {}  # id -> enabled
        self.hub = set()  # ids the repository index offers
        self.live = STOCK
        self.playing = False
        self.dialog = None  # {"kind": "download"|"keep", "text": ...}
        self.focus = "10"
        self.clock = 1000.0
        self.keep_timeout = False  # let the keep-skin countdown expire
        self.install_after_polls = 2  # the download takes a few polls
        self._install_pending = None
        self.logs = []
        self.builtins = []
        self.rpc_calls = []
        self.settings = {}
        self.keep_answered = 0

    # -- paths --
    def addons_home(self):
        return os.path.join(self.store.home, "addons")

    def seed_skin_dir(self, aid):
        d = os.path.join(self.addons_home(), aid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "addon.xml"), "w") as f:
            f.write('<addon id="%s"/>' % aid)
        os.makedirs(os.path.join(self.addons_home(), "packages"), exist_ok=True)
        with open(os.path.join(self.addons_home(), "packages", aid + "-1.4.4.zip"), "wb") as f:
            f.write(b"zip")

    # -- clock --
    def sleep(self, ms):
        self.clock += ms / 1000.0
        if self._install_pending is not None:
            self._install_pending -= 1
            if self._install_pending <= 0:
                self.addons[NEW] = True
                self.seed_skin_dir(NEW)
                self._install_pending = None
        if self.dialog and self.dialog["kind"] == "keep" and self.keep_timeout:
            if self.clock >= self.dialog["opened"] + 10.0:
                # Kodi: anything but Yes within 10 s reverts to the old skin.
                self.dialog = None
                self.live = self.dialog_old

    # -- JSON-RPC --
    def rpc(self, raw):
        req = json.loads(raw)
        m, p = req["method"], req.get("params", {})
        self.rpc_calls.append((m, p))
        if m == "Addons.GetAddonDetails":
            aid = p["addonid"]
            if aid not in self.addons:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps(
                {"result": {"addon": {"addonid": aid, "enabled": self.addons[aid]}}}
            )
        if m == "Addons.SetAddonEnabled":
            aid = p["addonid"]
            if aid in self.addons:
                self.addons[aid] = bool(p["enabled"])
            return json.dumps({"result": "OK"})
        if m == "Addons.GetAddons":
            assert p.get("installed") is False
            assert p.get("type") == "xbmc.gui.skin"
            return json.dumps(
                {"result": {"addons": [{"addonid": a, "type": "xbmc.gui.skin"} for a in sorted(self.hub)]}}
            )
        if m == "Settings.SetSettingValue":
            if p["setting"] == "lookandfeel.skin":
                want = p["value"]
                if not self.addons.get(want):
                    return json.dumps({"error": {"code": -32602}})
                self.dialog_old = self.live
                self.live = want
                self.dialog = {"kind": "keep", "text": KEEP_TEXT, "opened": self.clock}
                self.focus = "10"
                return json.dumps({"result": True})
            return json.dumps({"result": True})
        return json.dumps({"result": None})

    # -- builtins --
    def builtin(self, cmd, wait=False):
        self.builtins.append(cmd)
        if cmd.startswith("InstallAddon("):
            aid = cmd[len("InstallAddon("):-1]
            if aid in self.hub and aid not in self.addons:
                self.dialog = {
                    "kind": "download",
                    "text": "To use this feature you must download an add-on:\nEstuary++\n" + DOWNLOAD_TEXT,
                }
                self.focus = "10"
        elif cmd == "Action(right)":
            if self.dialog:
                self.focus = "11"
        elif cmd == "Action(select)":
            if self.dialog and self.focus == "11":
                if self.dialog["kind"] == "download":
                    self._install_pending = self.install_after_polls
                else:
                    self.keep_answered += 1
                self.dialog = None
        elif cmd == "UpdateLocalAddons":
            for aid in list(self.addons):
                if not os.path.isdir(os.path.join(self.addons_home(), aid)):
                    del self.addons[aid]


def _rig(monkeypatch, tmp_path, platform="android"):
    store = FakeKodiStorage(tmp_path / "kodi", platform=platform)
    store.log = []
    rig = _Rig(store, platform)
    _xbmc_cls, vfs_cls = make_modules(store)

    xbmcvfs_mod = types.ModuleType("xbmcvfs")
    xbmcvfs_mod.File = vfs_cls.File
    xbmcvfs_mod.exists = vfs_cls.exists
    xbmcvfs_mod.delete = vfs_cls.delete
    xbmcvfs_mod.translatePath = vfs_cls.translatePath

    xbmc_mod = types.ModuleType("xbmc")
    xbmc_mod.LOGDEBUG, xbmc_mod.LOGINFO, xbmc_mod.LOGWARNING, xbmc_mod.LOGERROR = 0, 1, 2, 3
    xbmc_mod.log = lambda msg, level=1: rig.logs.append(msg)

    def cond(c):
        if c == "System.Platform.TVOS":
            return platform == "tvos"
        if c == "System.Platform.Android":
            return platform == "android"
        if c == "Window.IsActive(yesnodialog)":
            return rig.dialog is not None
        return False

    def label(name):
        if name == "Control.GetLabel(9)":
            return rig.dialog["text"] if rig.dialog else ""
        if name == "System.CurrentControlId":
            return rig.focus
        return ""

    xbmc_mod.getCondVisibility = cond
    xbmc_mod.getInfoLabel = label
    xbmc_mod.getLocalizedString = lambda sid: {13111: KEEP_TEXT, 24101: DOWNLOAD_TEXT}.get(sid, "")
    xbmc_mod.getSkinDir = lambda: rig.live
    xbmc_mod.executeJSONRPC = rig.rpc
    xbmc_mod.executebuiltin = rig.builtin
    xbmc_mod.sleep = rig.sleep
    xbmc_mod.Player = lambda: types.SimpleNamespace(
        isPlaying=lambda: rig.playing, isPlayingVideo=lambda: rig.playing
    )

    class _Addon:
        def __init__(self, *a, **k):
            pass

        def getSetting(self, key):
            return rig.settings.get(key, "")

        def setSetting(self, key, value):
            rig.settings[key] = value

        def getAddonInfo(self, key):
            return {"path": str(ADDON_ROOT)}.get(key, "")

    xbmcaddon_mod = types.ModuleType("xbmcaddon")
    xbmcaddon_mod.Addon = _Addon

    for name in list(sys.modules):
        if name.startswith("resources"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_mod)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs_mod)
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon_mod)
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    sm = importlib.import_module("resources.lib.modules.skinmigrate")
    monkeypatch.setattr(sm, "_now", lambda: rig.clock)
    rig.sm = sm
    return rig


def _pov_box(rig, with_settings=True):
    """A box on Estuary POV: the old skin installed, enabled and active, its
    settings.xml on disk, the hub offering the new id."""
    rig.addons[OLD] = True
    rig.seed_skin_dir(OLD)
    rig.live = OLD
    rig.hub.add(NEW)
    if with_settings:
        rig.store.seed_disk("addon_data/%s/settings.xml" % OLD, OLD_SETTINGS)


def _new_settings(rig):
    raw = rig.store.vfs_read("special://profile/addon_data/%s/settings.xml" % NEW)
    return bytes(raw) if raw else b""


def _migration_lines(rig):
    """The step's own lines; nsud.persist_one's confirmation rides the same
    logger and is not a step."""
    return [m for m in rig.logs if "skin migration" in m and "nsud." not in m]


# --------------------------------------------------------------------------- #
# 1. No-ops: the step must be harmless on every box it does not apply to
# --------------------------------------------------------------------------- #
def test_stock_estuary_box_is_untouched_and_silent(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.addons[STOCK] = True
    rig.live = STOCK
    rig.hub.add(NEW)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ALREADY and res["steps"] == []
    assert rig.builtins == [] and _migration_lines(rig) == []
    assert NEW not in rig.addons


def test_migrated_and_clean_box_is_silent(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.addons[NEW] = True
    rig.live = NEW
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ALREADY and res["steps"] == []
    assert rig.builtins == [] and _migration_lines(rig) == []


def test_setting_off_skips_with_one_line(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.settings["migrate_skin"] = "false"
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.SKIPPED and "disabled" in res["detail"]
    assert rig.builtins == [] and rig.live == OLD
    assert len(_migration_lines(rig)) == 1
    # unset and "true" both mean on
    rig.settings["migrate_skin"] = ""
    assert rig.sm.enabled_by_setting()
    rig.settings["migrate_skin"] = "true"
    assert rig.sm.enabled_by_setting()


def test_playback_defers_the_whole_step(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.playing = True
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.SKIPPED
    assert res["detail"].startswith("deferred")
    assert rig.builtins == [] and rig.live == OLD and NEW not in rig.addons
    assert _new_settings(rig) == b""
    # the explicit argument wins over the player probe
    rig.playing = False
    res = rig.sm.run(playing=True)
    assert res["detail"].startswith("deferred")


def test_hub_not_listing_the_new_skin_skips_and_installs_nothing(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.hub.clear()
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.SKIPPED
    assert "no repository offers" in res["detail"]
    assert not [b for b in rig.builtins if b.startswith("InstallAddon")]
    assert rig.live == OLD and _new_settings(rig) == b""
    assert len(_migration_lines(rig)) == 1


# --------------------------------------------------------------------------- #
# 2. The migration itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("platform", ["android", "tvos"])
def test_pov_box_installs_copies_and_switches(monkeypatch, tmp_path, platform):
    rig = _rig(monkeypatch, tmp_path, platform)
    _pov_box(rig)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED, res
    names = [s[0] for s in res["steps"]]
    assert names == ["install", "settings", "switch"]
    assert [s[1] for s in res["steps"]] == [rig.sm.APPLIED] * 3
    # (a) installed through Kodi's installer, the download prompt answered
    assert "InstallAddon(%s)" % NEW in rig.builtins
    assert rig.addons[NEW] is True
    assert "prompt answered" in res["steps"][0][2]
    # (b) the settings landed, byte-identical, in the layer Kodi reads
    assert _new_settings(rig) == OLD_SETTINGS
    rel = "addon_data/%s/settings.xml" % NEW
    assert rig.store.state(rel) == ("key-only" if platform == "tvos" else "disk-only")
    assert not os.path.exists(rig.store.translate("special://profile/" + rel) + ".ezm-tmp")
    # (c) switched, the keep-skin dialog answered yes, nothing left open
    assert rig.live == NEW and rig.dialog is None and rig.keep_answered == 1
    assert "answered yes" in res["steps"][2][2]
    # the old skin stays installed until the next start
    assert OLD in rig.addons and os.path.isdir(os.path.join(rig.addons_home(), OLD))
    # one log line per step
    assert len(_migration_lines(rig)) == 3


def test_next_start_removes_the_old_skin_and_its_row(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    assert rig.sm.run()["outcome"] == rig.sm.APPLIED
    rig.logs.clear()
    rig.builtins.clear()
    # the following start: active skin is the new one, old still installed
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert [s[0] for s in res["steps"]] == ["remove old skin"]
    assert not os.path.exists(os.path.join(rig.addons_home(), OLD))
    assert not os.path.exists(os.path.join(rig.addons_home(), "packages", OLD + "-1.4.4.zip"))
    assert "UpdateLocalAddons" in rig.builtins
    assert OLD not in rig.addons
    assert "dropped from the add-on database" in res["detail"]
    # the old settings folder is kept for now
    assert rig.store.state("addon_data/%s/settings.xml" % OLD) != "absent"
    assert len(_migration_lines(rig)) == 1
    # and the start after that is silent
    rig.logs.clear()
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ALREADY and _migration_lines(rig) == []


def test_removal_waits_for_playback_too(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.addons[NEW] = True
    rig.addons[OLD] = True
    rig.seed_skin_dir(OLD)
    rig.live = NEW
    rig.playing = True
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.SKIPPED and res["detail"].startswith("deferred")
    assert os.path.isdir(os.path.join(rig.addons_home(), OLD)) and OLD in rig.addons


def test_removal_reports_when_kodi_keeps_the_row(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.addons[NEW] = True
    rig.addons[OLD] = True
    rig.seed_skin_dir(OLD)
    rig.live = NEW
    # a Kodi whose UpdateLocalAddons does not drop the row
    real = rig.builtin

    def stubborn(cmd, wait=False):
        if cmd == "UpdateLocalAddons":
            rig.builtins.append(cmd)
            return
        real(cmd, wait)

    rig.sm.xbmc.executebuiltin = stubborn
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR and "still lists" in res["detail"]
    assert not os.path.isdir(os.path.join(rig.addons_home(), OLD))


def test_existing_new_settings_are_never_overwritten(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    own = b'<settings version="2"><setting id="pov_menu_defaults" type="bool">true</setting></settings>'
    rig.store.seed_disk("addon_data/%s/settings.xml" % NEW, own)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert res["steps"][1][1] == rig.sm.ALREADY
    assert _new_settings(rig) == own
    assert rig.live == NEW


def test_no_old_settings_is_a_skip_of_that_step_only(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig, with_settings=False)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert res["steps"][1][1] == rig.sm.SKIPPED
    assert _new_settings(rig) == b"" and rig.live == NEW


def test_tvos_unconfirmed_vector_stops_before_the_switch(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path, "tvos")
    _pov_box(rig)
    monkeypatch.setattr(rig.sm.nsud, "persist_one", lambda rel, log=None: False)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR and "persisted" in res["detail"]
    assert [s[0] for s in res["steps"]] == ["install", "settings"]
    assert rig.live == OLD  # not switched without the menu
    # the POSIX copy stands (nothing lost)
    assert rig.store.state("addon_data/%s/settings.xml" % NEW) == "disk-only"


def test_keep_skin_dialog_timing_out_is_reported_and_leaves_the_old_skin(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.keep_timeout = True
    # the watcher cannot see the dialog: Kodi's countdown expires and reverts
    real_cond = rig.sm.xbmc.getCondVisibility
    rig.sm.xbmc.getCondVisibility = lambda c: False if c == "Window.IsActive(yesnodialog)" and rig.dialog and rig.dialog["kind"] == "keep" else real_cond(c)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR
    assert "did not hold" in res["detail"] and "not seen" in res["detail"]
    assert rig.live == OLD and rig.dialog is None
    # the install and the settings copy stand for the retry
    assert rig.addons[NEW] is True and _new_settings(rig) == OLD_SETTINGS
    # the retry (next start) only has the switch left to do
    rig.keep_timeout = False
    rig.sm.xbmc.getCondVisibility = real_cond
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert [(s[0], s[1]) for s in res["steps"]] == [
        ("install", rig.sm.ALREADY),
        ("settings", rig.sm.ALREADY),
        ("switch", rig.sm.APPLIED),
    ]
    assert rig.live == NEW


def test_install_prompt_never_appearing_is_an_error_not_a_hang(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.hub.add(NEW)
    real = rig.builtin

    def no_prompt(cmd, wait=False):
        if cmd.startswith("InstallAddon("):
            rig.builtins.append(cmd)
            return
        real(cmd, wait)

    rig.sm.xbmc.executebuiltin = no_prompt
    start = rig.clock
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR and "never seen" in res["detail"]
    assert rig.clock - start >= rig.sm._INSTALL_TIMEOUT_S
    assert rig.live == OLD and _new_settings(rig) == b""


def test_only_kodis_own_prompt_is_answered(monkeypatch, tmp_path):
    """A different yes/no on screen is left alone, never pressed."""
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    real = rig.builtin

    def other_dialog(cmd, wait=False):
        if cmd.startswith("InstallAddon("):
            rig.builtins.append(cmd)
            rig.dialog = {"kind": "other", "text": "Delete all your files?"}
            return
        real(cmd, wait)

    rig.sm.xbmc.executebuiltin = other_dialog
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR
    assert rig.dialog == {"kind": "other", "text": "Delete all your files?"}
    assert "Action(select)" not in rig.builtins and "Action(right)" not in rig.builtins


def test_old_installed_new_missing_on_stock_estuary_installs_but_does_not_switch(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.addons[STOCK] = True
    rig.live = STOCK
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert [s[0] for s in res["steps"]] == ["install", "settings"]
    assert rig.addons[NEW] is True and _new_settings(rig) == OLD_SETTINGS
    assert rig.live == STOCK and rig.keep_answered == 0


def test_installed_but_disabled_new_skin_is_enabled_not_reinstalled(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    rig.addons[NEW] = False
    rig.seed_skin_dir(NEW)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.APPLIED
    assert "enabled" in res["steps"][0][2]
    assert not [b for b in rig.builtins if b.startswith("InstallAddon")]
    assert rig.addons[NEW] is True and rig.live == NEW


def test_run_never_raises(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _pov_box(rig)
    monkeypatch.setattr(rig.sm, "install_new_skin", lambda log=None: 1 / 0)
    res = rig.sm.run()
    assert res["outcome"] == rig.sm.ERROR and "ZeroDivisionError" in res["detail"]


# --------------------------------------------------------------------------- #
# 3. Restore: the id mapping and the settings carry
# --------------------------------------------------------------------------- #
def test_same_skin_accepts_either_id_across_the_rename(monkeypatch, tmp_path):
    sm = _rig(monkeypatch, tmp_path).sm
    assert sm.same_skin(OLD, NEW) and sm.same_skin(NEW, OLD)
    assert sm.same_skin(OLD, OLD) and sm.same_skin(NEW, NEW)
    assert sm.same_skin(" %s " % NEW, OLD)
    assert not sm.same_skin(STOCK, NEW) and not sm.same_skin(OLD, STOCK)
    assert not sm.same_skin("", NEW) and sm.same_skin(None, "")


def test_map_restored_skin(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    sm = rig.sm
    # new skin absent: old archives restore as they were
    assert sm.map_restored_skin(OLD) == OLD
    assert sm.map_restored_skin(STOCK) == STOCK
    assert sm.map_restored_skin(None) is None and sm.map_restored_skin("  ") is None
    # present on disk only (before Kodi has scanned it)
    rig.seed_skin_dir(NEW)
    assert sm.map_restored_skin(OLD) == NEW
    assert sm.map_restored_skin(NEW) == NEW
    assert sm.map_restored_skin(STOCK) == STOCK
    # present as far as Kodi knows
    rig2 = _rig(monkeypatch, tmp_path / "b")
    rig2.addons[NEW] = True
    assert rig2.sm.map_restored_skin(OLD) == NEW


def test_carry_restored_settings_copies_the_archives_file_over(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    ud = rig.store.userdata
    rig.store.seed_disk("addon_data/%s/settings.xml" % OLD, OLD_SETTINGS)
    rig.store.seed_disk("addon_data/%s/settings.xml" % NEW, b"<settings/>")
    assert rig.sm.carry_restored_settings(ud, log=rig.logs.append) is True
    dst = os.path.join(ud, "addon_data", NEW, "settings.xml")
    assert open(dst, "rb").read() == OLD_SETTINGS  # the archive wins on restore
    assert not os.path.exists(dst + ".ezm-tmp")
    assert rig.store.state("addon_data/%s/settings.xml" % NEW) == "disk-only"
    assert any("carried" in m for m in rig.logs)
    # nothing to carry: False, nothing written
    rig3 = _rig(monkeypatch, tmp_path / "c")
    assert rig3.sm.carry_restored_settings(rig3.store.userdata) is False
    assert not os.path.exists(os.path.join(rig3.store.userdata, "addon_data", NEW))


# --------------------------------------------------------------------------- #
# 4. wiz: the restore pass maps the id
# --------------------------------------------------------------------------- #
def _wiz_with_stub(monkeypatch, mapped, carry_result=True, raise_in_map=False):
    """A stubbed skinmigrate for wiz's _map_restored_skin; returns
    (stub, calls)."""
    calls = []
    stub = types.ModuleType("resources.lib.modules.skinmigrate")

    def _map(target):
        if raise_in_map:
            raise RuntimeError("boom")
        calls.append(("map", target))
        return mapped if target == OLD else target

    def _carry(userdata, log=None):
        calls.append(("carry", userdata))
        return carry_result

    stub.map_restored_skin = _map
    stub.carry_restored_settings = _carry
    return stub, calls


def test_wiz_maps_the_old_id_and_carries_settings(monkeypatch, tmp_path, request):
    wz = request.getfixturevalue("wiz")
    stub, calls = _wiz_with_stub(monkeypatch, NEW)
    monkeypatch.setitem(sys.modules, "resources.lib.modules.skinmigrate", stub)
    setattr(sys.modules["resources.lib.modules"], "skinmigrate", stub)
    lines = []
    assert wz._map_restored_skin(lines.append, OLD) == NEW
    assert calls == [("map", OLD), ("carry", wz.control.USERDATA)]
    assert any("restoring as %s" % NEW in m for m in lines)
    # anything else passes through, no copy
    calls.clear()
    assert wz._map_restored_skin(lines.append, STOCK) == STOCK
    assert wz._map_restored_skin(lines.append, None) is None
    assert calls == [("map", STOCK), ("map", None)]


def test_wiz_keeps_the_archives_id_when_the_mapping_fails(monkeypatch, tmp_path, request):
    wz = request.getfixturevalue("wiz")
    stub, _calls = _wiz_with_stub(monkeypatch, NEW, raise_in_map=True)
    monkeypatch.setitem(sys.modules, "resources.lib.modules.skinmigrate", stub)
    setattr(sys.modules["resources.lib.modules"], "skinmigrate", stub)
    lines = []
    assert wz._map_restored_skin(lines.append, OLD) == OLD
    assert any("mapping failed" in m for m in lines)


def test_restore_pass_maps_after_capturing_the_archives_settings():
    """Source-level: the mapping runs AFTER the settings capture (which reads
    the archive's file under the OLD id) and BEFORE the boot-skin write and
    the restore-check marker consume the target."""
    src = (MODULES / "wiz.py").read_text(encoding="utf-8")
    i_capture = src.index('_boot_skin["settings"] = (')
    i_map = src.index('_boot_skin["target"] = _map_restored_skin(')
    i_apply = src.index("boot_skin = _apply_boot_skin(_rlog, _boot_skin.get(\"target\"))")
    i_marker = src.index("tools.mark_restore_check_pending(_boot_skin.get(\"target\"))")
    assert i_capture < i_map < i_apply
    assert i_map < i_marker


# --------------------------------------------------------------------------- #
# 5. service.py: the wiring
# --------------------------------------------------------------------------- #
def _inject(monkeypatch, name, mod):
    monkeypatch.setitem(sys.modules, "resources.lib.modules." + name, mod)
    setattr(sys.modules["resources.lib.modules"], name, mod)


def _sm_stub(results):
    m = types.ModuleType("resources.lib.modules.skinmigrate")
    m.APPLIED, m.ALREADY, m.SKIPPED, m.ERROR = "applied", "already-correct", "skipped", "error"
    m.calls = []

    def run(log=None, playing=None):
        m.calls.append(playing)
        return results.pop(0)

    m.run = run
    m.same_skin = lambda a, b: a == b or {a, b} == {OLD, NEW}
    return m


def test_service_step_is_owed_again_only_when_deferred(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    stub = _sm_stub([
        {"outcome": "skipped", "detail": "deferred: something is playing", "steps": []},
        {"outcome": "skipped", "detail": "no repository offers %s yet" % NEW, "steps": []},
        {"outcome": "error", "detail": "switch did not hold", "steps": []},
        {"outcome": "applied", "detail": "switched", "steps": []},
    ])
    _inject(monkeypatch, "skinmigrate", stub)
    assert mod._maybe_migrate_skin() is False  # deferred: retry on the next tick
    assert mod._maybe_migrate_skin(playing=False) is True  # hub skip: next start
    assert mod._maybe_migrate_skin(playing=False) is True  # error: warned, next start
    assert mod._maybe_migrate_skin(playing=False) is True
    assert stub.calls == [None, False, False, False]
    assert any("switch did not hold" in m and level == 2 for level, m in env.logs), env.logs


def test_service_step_crash_is_warned_and_not_retried_every_tick(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    stub = _sm_stub([])
    stub.run = lambda log=None, playing=None: 1 / 0
    _inject(monkeypatch, "skinmigrate", stub)
    assert mod._maybe_migrate_skin() is True
    assert any("skin migration crashed" in m for _l, m in env.logs)


def test_service_loop_retries_an_owed_migration_on_the_next_idle_tick(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    stub = _sm_stub([{"outcome": "applied", "detail": "switched", "steps": []}])
    _inject(monkeypatch, "skinmigrate", stub)
    mod._SKIN_MIGRATION_OWED["owed"] = True
    ticks = {"n": 0}

    class _Mon:
        def abortRequested(self):
            return ticks["n"] >= 2

        def waitForAbort(self, t):
            ticks["n"] += 1
            return ticks["n"] >= 3

    monkeypatch.setattr(mod, "_maybe_update_addon_repos", lambda **k: None)
    mod._service_loop(_Mon())
    assert stub.calls == [False]
    assert mod._SKIN_MIGRATION_OWED["owed"] is False


def test_service_runs_the_migration_after_the_gui_wait_and_before_the_pvr_share_step():
    src = (ADDON_ROOT / "service.py").read_text(encoding="utf-8")
    main = src[src.index('if __name__ == "__main__":'):]
    i_ready = main.index("_startup_checks(monitor)")
    i_skin = main.index('_SKIN_MIGRATION_OWED["owed"] = not _maybe_migrate_skin()')
    i_pvr = main.index('_PVR_SHARE_OWED["owed"] = not _maybe_sync_pvr_share()')
    assert i_ready < i_skin < i_pvr


def test_service_restore_check_accepts_either_id(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    _inject(monkeypatch, "skinmigrate", _sm_stub([]))
    assert mod._skin_ids_match(NEW, OLD)
    assert mod._skin_ids_match(OLD, NEW)
    assert mod._skin_ids_match(NEW, NEW)
    assert not mod._skin_ids_match(STOCK, OLD)


def test_service_restore_check_falls_back_to_equality_without_the_module(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    monkeypatch.delitem(sys.modules, "resources.lib.modules.skinmigrate", raising=False)
    assert mod._skin_ids_match(NEW, NEW)
    assert not mod._skin_ids_match(NEW, OLD)


def test_service_names_no_skin_id():
    """The ids live in skinmigrate.py only; service.py stays skin-agnostic."""
    src = (ADDON_ROOT / "service.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )
    assert OLD not in code and NEW not in code


# --------------------------------------------------------------------------- #
# 6. The shipped artifacts
# --------------------------------------------------------------------------- #
def test_setting_exists_default_on_with_its_strings():
    settings = (ADDON_ROOT / "resources" / "settings.xml").read_text(encoding="utf-8")
    i = settings.index('<setting id="migrate_skin" type="boolean" label="30063">')
    assert "<default>true</default>" in settings[i : i + 300]
    po = (
        ADDON_ROOT / "resources" / "language" / "resource.language.en_gb" / "strings.po"
    ).read_text(encoding="utf-8")
    assert 'msgctxt "#30063"' in po and 'msgctxt "#30064"' in po


def test_news_and_changelog_carry_the_release():
    addon = (ADDON_ROOT / "addon.xml").read_text(encoding="utf-8")
    assert 'version="2026.09.27.1"' in addon
    assert "v2026.09.27.1: The skin is now Estuary++." in addon
    changelog = (ADDON_ROOT / "changelog.txt").read_text(encoding="utf-8")
    assert changelog.startswith("v2026.09.27.1\n- The skin is now Estuary++.")
