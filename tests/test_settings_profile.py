# -*- coding: utf-8 -*-
"""The Settings Profile engine (resources/lib/modules/profile.py).

Four layers, per the plan's test list (docs/settings-profile-plan.md section 8):

1. Unit tests on load() and plan(): pure functions of the bundle directory and
   the injected device class / catalog. Includes THE AUTHORING GATE: every
   setting id in the shipped House bundle must exist in the captured Kodi 22
   catalog (tests/data/kodi22-setting-ids.txt), which is how a typo'd or
   renamed id is caught in CI instead of on a box.
2. The two ADVERSARIAL cases the house standard demands (a test that does not
   fail on the pre-fix code proves nothing): the per-item persist_one loop must
   FAIL against the two-layer tvOS storage fake, and a whole-document class C
   write must FAIL the pre-existing-sources-survive assertion.
3. The POSITIVE counterpart: apply() executed against fake_kodi_storage at
   platform="tvos", asserting every class A id lands in the final artifact and
   exactly ONE vector was taken. The adversarial cases guard the FAKE; this
   guards the shipped code.
4. The confirm-gated set dance (the three ids Kodi gates behind its own modal,
   measured 2026-08-30): answered means applied, unanswerable means an honest
   timeout with the dialog closed behind us.
"""

from __future__ import annotations

import importlib
import json
import sys
import threading
import types
import zipfile
from pathlib import Path

import pytest

from fake_kodi_storage import FakeKodiStorage, make_modules

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parent / "script.ezmaintenanceplusplus"
HOUSE = ADDON_ROOT / "resources" / "profiles" / "house"
CATALOG_FILE = HERE / "data" / "kodi22-setting-ids.txt"


def _catalog():
    ids = set()
    for line in CATALOG_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line)
    return ids


# --------------------------------------------------------------------------- #
# Importing the real module tree
# --------------------------------------------------------------------------- #
def _purge_addon_modules(monkeypatch):
    for name in list(sys.modules):
        if name.startswith("resources"):
            monkeypatch.delitem(sys.modules, name, raising=False)


def _import_profile(monkeypatch, xbmc_mod=None, xbmcvfs_mod=None, addon_settings=None):
    """Import the REAL profile module (and its real siblings _kodisettings and
    nsud) under fake Kodi modules. The default stubs are inert - enough for the
    pure load()/plan() surface; apply() tests hand in live fakes."""
    if xbmc_mod is None:
        xbmc_mod = types.ModuleType("xbmc")
        xbmc_mod.log = lambda *a, **k: None
        xbmc_mod.getCondVisibility = lambda cond: False
        xbmc_mod.executeJSONRPC = lambda raw: "{}"
        xbmc_mod.executebuiltin = lambda *a, **k: None
        xbmc_mod.sleep = lambda ms: None
        xbmc_mod.getInfoLabel = lambda label: ""
        xbmc_mod.getLocalizedString = lambda sid: ""
        xbmc_mod.LOGDEBUG = 0
        xbmc_mod.LOGINFO = 1
        xbmc_mod.LOGWARNING = 2
        xbmc_mod.LOGERROR = 3
    if xbmcvfs_mod is None:
        xbmcvfs_mod = types.ModuleType("xbmcvfs")
        xbmcvfs_mod.translatePath = lambda p: p
        xbmcvfs_mod.exists = lambda p: False
        xbmcvfs_mod.File = None
    xbmcaddon_mod = types.ModuleType("xbmcaddon")
    settings = addon_settings if addon_settings is not None else {}

    class _Addon:
        def __init__(self, *a, **k):
            pass

        def getSetting(self, key):
            return settings.get(key, "")

        def setSetting(self, key, value):
            settings[key] = value

        def getAddonInfo(self, key):
            return {"path": str(ADDON_ROOT)}.get(key, "")

    xbmcaddon_mod.Addon = _Addon
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_mod)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs_mod)
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon_mod)
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    _purge_addon_modules(monkeypatch)
    return importlib.import_module("resources.lib.modules.profile")


# --------------------------------------------------------------------------- #
# Synthetic bundle builder
# --------------------------------------------------------------------------- #
def _write(p, text):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def make_bundle(
    tmp_path,
    fragments=None,
    overlay_fragments=None,
    sources_xml=None,
    addon_data=None,
    classes=("fireos", "tvos", "bench"),
):
    b = tmp_path / "bundle"
    _write(
        b / "profile.json",
        json.dumps({"name": "Test", "schema_version": 1, "bundle_version": "1"}),
    )
    for fname, body in (fragments or {}).items():
        _write(b / "settings.d" / fname, body)
    for cls in classes:
        _write(
            b / "overlays" / cls / "addon_data" / "script.ezmaintenanceplusplus"
            / "settings.xml",
            '<settings version="2">'
            '<setting id="download.path">nfs://h/x/%s/</setting>'
            "</settings>" % cls,
        )
    for cls, frags in (overlay_fragments or {}).items():
        for fname, body in frags.items():
            _write(b / "overlays" / cls / "settings.d" / fname, body)
    if sources_xml is not None:
        _write(b / "sources.xml", sources_xml)
    for aid, files in (addon_data or {}).items():
        for fname, body in files.items():
            _write(b / "overlays" / "fireos" / "addon_data" / aid / fname, body)
    return b


# --------------------------------------------------------------------------- #
# 1. load() and plan(): the pure surface
# --------------------------------------------------------------------------- #
def test_house_bundle_loads_for_every_device_class(monkeypatch):
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        assert bundle["device_class"] == cls
        assert len(bundle["class_a"]) == 16, (
            "the House bundle carries 16 class A ids (13 from the plan plus "
            "the three added to bootstrapper after 2026-08-04)"
        )
        assert len(bundle["sources"]) == 3
        assert {a["id"] for a in bundle["addons"]} == {
            "repository.tony7bones",
            "script.image.resource.select",
        }


def test_house_bundle_passes_the_authoring_catalog_gate(monkeypatch):
    """THE CI AUTHORING GATE (plan 7.1): every id in the shipped bundle exists
    in the catalog captured from a first-run Kodi 22 Piers profile. A renamed
    or typo'd id fails HERE, in CI, instead of silently doing nothing on a
    box. At runtime the same check is deliberately absent: a moved live
    catalog produces a per-item unknown-id outcome, never an aborted apply."""
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "bench"):
        bundle = profile.load(str(HOUSE), cls, known_ids=_catalog())
        assert len(bundle["class_a"]) == 16, cls


def test_house_overlays_differ_per_class_and_bench_is_deliberate(monkeypatch):
    """The backup folder leaf is overlay-only: tvos gets tvos/, fireos gets
    fireos/, and the bench REPRODUCES the fireos leaf on purpose (the bench has
    always been seeded with it; plan 7.1)."""
    profile = _import_profile(monkeypatch)
    leaves = {}
    for cls in ("fireos", "tvos", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        own = bundle["addon_data"]["script.ezmaintenanceplusplus"]["settings.xml"]
        leaves[cls] = dict(own["pairs"])["download.path"]
    assert leaves["fireos"].endswith("/fireos/")
    assert leaves["tvos"].endswith("/tvos/")
    assert leaves["bench"] == leaves["fireos"]


def test_house_esenabled_split_tvos_false_others_true(monkeypatch):
    """The event server split, pinned: tvOS carries services.esenabled FALSE
    (the 2026-08-28 watchdog-kill workaround, owner-approved as the recorded
    tvOS state - a profile run must never undo it), while fireos and the bench
    keep TRUE. On every class the id keeps its base POSITION, before
    services.esallinterfaces (parent before dependent), which is the exact
    trap the position-from-first merge rule exists for."""
    profile = _import_profile(monkeypatch)
    for cls, want in (("fireos", "true"), ("bench", "true"), ("tvos", "false")):
        bundle = profile.load(str(HOUSE), cls)
        values = dict(bundle["class_a"])
        assert values["services.esenabled"] == want, (
            "%s must ship services.esenabled=%s" % (cls, want)
        )
        order = [sid for sid, _ in bundle["class_a"]]
        assert order.index("services.esenabled") < order.index(
            "services.esallinterfaces"
        ), "%s: the overlay override moved esenabled after its dependent" % cls


def test_unresolved_device_class_is_a_hard_failure(monkeypatch):
    profile = _import_profile(monkeypatch)
    with pytest.raises(profile.ProfileError):
        profile.load(str(HOUSE), "androidtv")


def test_missing_overlay_for_the_running_class_is_a_hard_failure(
    monkeypatch, tmp_path
):
    """Without this, a box whose class has no overlay silently inherits another
    class's backup folder - an Apple TV writing into the Fire TV folder with
    nothing to notice (plan 7.1, round 1 blocking finding)."""
    profile = _import_profile(monkeypatch)
    b = make_bundle(tmp_path, classes=("fireos", "bench"))
    profile.load(str(b), "fireos")  # sanity: classes with overlays load
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(b), "tvos")
    assert "overlay" in str(e.value)


def test_default_true_is_rejected(monkeypatch, tmp_path):
    """default="true" marks a value as untouched; Kodi falls back to its own
    default instead of using what is written. A fragment carrying it would
    validate the author's intent away silently."""
    profile = _import_profile(monkeypatch)
    b = make_bundle(
        tmp_path,
        fragments={
            "20-x.xml": '<settings version="2">'
            '<setting id="services.esenabled" default="true">true</setting>'
            "</settings>"
        },
    )
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(b), "fireos")
    assert "default=" in str(e.value)


def test_never_apply_ids_are_rejected_via_the_imported_set(monkeypatch, tmp_path):
    """profile.py IMPORTS _kodisettings._BOOT_STATE_ONLY rather than restating
    it - two copies of a predicate drifting is the failure that forced
    restorecheck to import nsud._is_skin_menu_sidecar (plan 4.1). This test
    iterates the REAL set, so a new never-apply id is covered the day it is
    added."""
    profile = _import_profile(monkeypatch)
    from resources.lib.modules import _kodisettings

    assert _kodisettings._BOOT_STATE_ONLY, "the never-apply set vanished"
    for sid in _kodisettings._BOOT_STATE_ONLY:
        b = make_bundle(
            tmp_path / sid.replace(".", "_"),
            fragments={
                "20-x.xml": '<settings version="2">'
                '<setting id="%s">x</setting></settings>' % sid
            },
        )
        with pytest.raises(profile.ProfileError):
            profile.load(str(b), "fireos")


def test_unknown_id_fails_the_catalog_gate_but_not_runtime_load(
    monkeypatch, tmp_path
):
    profile = _import_profile(monkeypatch)
    b = make_bundle(
        tmp_path,
        fragments={
            "20-x.xml": '<settings version="2">'
            '<setting id="services.nosuchsetting">1</setting></settings>'
        },
    )
    with pytest.raises(profile.ProfileError):
        profile.load(str(b), "fireos", known_ids=_catalog())
    # runtime load (no catalog) accepts it; the APPLY reports unknown-id
    bundle = profile.load(str(b), "fireos")
    assert bundle["class_a"] == [("services.nosuchsetting", "1")]


def test_comment_in_addon_data_is_rejected(monkeypatch, tmp_path):
    """CAddonSettings::Load calls Attribute("id") on every child node without
    checking it is an element; a comment node is a SIGABRT on the first
    getSetting() - and under the apply order that crash lands INSIDE the flow,
    right after the add-on is enabled (plan 7.1)."""
    profile = _import_profile(monkeypatch)
    b = make_bundle(
        tmp_path,
        addon_data={
            "some.addon": {
                "settings.xml": '<settings version="2">'
                "<!-- sixteen lines of helpful prose -->"
                '<setting id="a">1</setting></settings>'
            }
        },
    )
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(b), "fireos")
    assert "comment" in str(e.value)


def test_whole_document_sources_are_rejected(monkeypatch, tmp_path):
    """The class C file carries ENTRIES, never a document. A <default> stub or
    a section other than <files> is the whole-document shape whose copy onto a
    configured box deletes every source that box already had (plan 4.3)."""
    profile = _import_profile(monkeypatch)
    whole = (
        "<sources><video><default /></video>"
        "<files><default />"
        "<source><name>A</name><path>https://x/</path></source>"
        "</files></sources>"
    )
    b = make_bundle(tmp_path, sources_xml=whole)
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(b), "fireos")
    msg = str(e.value)
    assert "<default>" in msg or "default" in msg
    assert "video" in msg


def test_source_path_rules_trailing_slash_and_no_port(monkeypatch, tmp_path):
    profile = _import_profile(monkeypatch)
    bad = (
        "<sources><files>"
        "<source><name>A</name><path>nfs://192.168.7.2:2049/x/</path></source>"
        "<source><name>B</name><path>nfs://192.168.7.2/x</path></source>"
        "</files></sources>"
    )
    b = make_bundle(tmp_path, sources_xml=bad)
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(b), "fireos")
    msg = str(e.value)
    assert "port" in msg
    assert "trailing slash" in msg


def test_overlay_override_keeps_position_from_first(monkeypatch, tmp_path):
    """THE ordering trap (plan 7.1, round 1 blocking finding): an overlay
    overriding services.esenabled must change its VALUE without moving it
    after services.esallinterfaces, because esallinterfaces is a no-op unless
    esenabled is already applied and Kodi can refuse a dependent set outright.
    Value from the LAST occurrence, position from the FIRST."""
    profile = _import_profile(monkeypatch)
    b = make_bundle(
        tmp_path,
        fragments={
            "20-services.xml": '<settings version="2">'
            '<setting id="services.esenabled">true</setting>'
            '<setting id="services.esallinterfaces">true</setting>'
            "</settings>"
        },
        overlay_fragments={
            "fireos": {
                "90-override.xml": '<settings version="2">'
                '<setting id="services.esenabled">false</setting>'
                "</settings>"
            }
        },
    )
    bundle = profile.load(str(b), "fireos")
    assert bundle["class_a"] == [
        ("services.esenabled", "false"),
        ("services.esallinterfaces", "true"),
    ]


def test_plan_order_matches_7_4(monkeypatch):
    """The apply order, asserted end to end on the real House bundle: own
    settings, staging, one refresh, non-repo enables, class A sets, ONE
    guisettings file op, sources merge, and the T7B repository enable DEAD
    LAST (plan 7.4, with the E3-measured no-swap of steps 3 and 4)."""
    profile = _import_profile(monkeypatch)
    bundle = profile.load(str(HOUSE), "tvos")
    ops = profile.plan(bundle)
    kinds = [op["kind"] for op in ops]
    assert kinds.count("write-guisettings") == 1
    assert kinds.count("refresh") == 1
    assert kinds.count("sources") == 1
    # the last op is the repo enable
    assert ops[-1]["kind"] == "enable"
    assert ops[-1]["addon"] == "repository.tony7bones"
    assert ops[-1]["last"] is True
    # ordering indexes
    idx = {k: kinds.index(k) for k in ("stage", "refresh", "enable", "set",
                                       "write-guisettings", "sources")}
    assert idx["stage"] < idx["refresh"] < idx["enable"] < idx["set"]
    assert idx["set"] < idx["write-guisettings"] < idx["sources"]
    # class A order follows the fragments; the sets carry the confirm flag for
    # exactly the three measured confirm-gated ids
    confirm = {op["id"] for op in ops if op["kind"] == "set" and op["confirm"]}
    assert confirm == {
        "addons.unknownsources",
        "services.webserver",
        "services.esallinterfaces",
    }
    sets = [op["id"] for op in ops if op["kind"] == "set"]
    assert sets.index("services.esenabled") < sets.index("services.esallinterfaces")
    assert sets.index("addons.unknownsources") < sets.index("addons.updatemode")
    assert sets.index("services.webserverpassword") < sets.index(
        "services.webserver"
    ), (
        "the password must land before the web server enables: with auth on "
        "and no password Kodi vetoes the enable with its invalid-config OK "
        "dialog (string 36635) - measured on the first full bench run"
    )


# --------------------------------------------------------------------------- #
# The live-fake rig for apply()
# --------------------------------------------------------------------------- #
_CONFIRM_TEXTS = {
    36618: "Add-ons will be given access to personal data. Proceed?",
    36632: "Anyone who has access to the web interface will control this. Proceed?",
    36633: "These services offer neither authentication nor encryption. Proceed?",
}


class FakeKodi:
    """A live Kodi stand-in: settings store, add-on registry, and the modal
    confirm machinery for the three gated ids (modelled on the measured
    behaviour: the set BLOCKS its calling thread until the dialog resolves;
    answering Yes commits, anything else leaves the old value)."""

    def __init__(self, store, settings, addons_home):
        self.store = store
        self.settings = settings
        self.addons_home = Path(addons_home)
        self.addons = {}
        self.gated = {}  # sid -> localized-string id
        self.ok_veto = {}  # sid -> OK-dialog text (invalid-config style veto)
        self.answer_dialogs = True
        self.dialog = None  # {"sid","text","event","answered","type"}
        self.focus = "10"
        self.events = []
        self.builtins = []

    # ---- xbmc surface ----------------------------------------------------- #
    def executeJSONRPC(self, raw):
        req = json.loads(raw)
        m, p = req["method"], req.get("params", {})
        if m == "Settings.GetSettingValue":
            sid = p["setting"]
            if sid not in self.settings:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps({"result": {"value": self.settings[sid]}})
        if m == "Settings.SetSettingValue":
            sid, value = p["setting"], p["value"]
            if sid not in self.settings:
                return json.dumps({"error": {"code": -32602}})
            if sid in self.ok_veto:
                # The invalid-config veto shape (NetworkServices string
                # 36635): an OK dialog explains, the set returns false, and
                # the value never commits - regardless of how the dialog is
                # dismissed.
                ev = threading.Event()
                self.dialog = {
                    "sid": sid,
                    "text": self.ok_veto[sid],
                    "event": ev,
                    "answered_yes": False,
                    "type": "ok",
                }
                ev.wait(10)
                self.dialog = None
                return json.dumps({"result": False})
            if sid in self.gated:
                ev = threading.Event()
                self.dialog = {
                    "sid": sid,
                    "text": _CONFIRM_TEXTS[self.gated[sid]],
                    "event": ev,
                    "answered_yes": False,
                    "type": "yesno",
                }
                self.focus = "10"
                ev.wait(10)
                answered_yes = self.dialog and self.dialog["answered_yes"]
                self.dialog = None
                if not answered_yes:
                    return json.dumps({"result": False})
            self.settings[sid] = value
            self.events.append(("set", sid, value))
            return json.dumps({"result": True})
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
                self.events.append(("enable", aid))
                return json.dumps({"result": "OK"})
            return json.dumps({"error": {"code": -32602}})
        return json.dumps({"result": {}})

    def executebuiltin(self, cmd):
        self.builtins.append(cmd)
        if cmd == "UpdateLocalAddons":
            if self.addons_home.is_dir():
                for d in self.addons_home.iterdir():
                    if (d / "addon.xml").exists() and d.name not in self.addons:
                        self.addons[d.name] = False
        elif cmd == "Action(right)":
            if self.dialog and self.answer_dialogs is not None:
                self.focus = "11"
        elif cmd == "Action(select)":
            if self.dialog and self.focus == "11" and self.answer_dialogs:
                self.dialog["answered_yes"] = True
                self.dialog["event"].set()
        elif cmd == "Dialog.Close(yesnodialog,true)":
            if self.dialog and self.dialog.get("type") == "yesno":
                self.dialog["answered_yes"] = False
                self.dialog["event"].set()
        elif cmd == "Dialog.Close(okdialog,true)":
            if self.dialog and self.dialog.get("type") == "ok":
                self.dialog["event"].set()

    def getCondVisibility(self, cond):
        if cond == "Window.IsActive(yesnodialog)":
            return self.dialog is not None and self.dialog.get("type") == "yesno"
        if cond == "Window.IsActive(okdialog)":
            return self.dialog is not None and self.dialog.get("type") == "ok"
        if cond == "System.Platform.TVOS":
            return self.store.platform == "tvos"
        if cond == "System.Platform.Android":
            return self.store.platform == "android"
        return False

    def getInfoLabel(self, label):
        if label == "Control.GetLabel(9)":
            return self.dialog["text"] if self.dialog else ""
        if label == "System.CurrentControlId":
            return self.focus
        return ""

    def getLocalizedString(self, sid):
        return _CONFIRM_TEXTS.get(sid, "")


_SEED_SETTINGS = {
    "services.webserver": False,
    "services.webserverport": 0,
    "services.webserverauthentication": False,
    "services.webserverusername": "",
    "services.webserverpassword": "",
    "services.esenabled": True,
    "services.esallinterfaces": False,
    "addons.unknownsources": False,
    "addons.updatemode": 0,
    "epg.selectaction": 2,
    "filelists.showparentdiritems": True,
    "pvrplayback.delaymarklastwatched": 0,
    "system.playlistspath": "",
    "locale.audiolanguage": "mediadefault",
    "locale.subtitlelanguage": "forced_only",
    "lookandfeel.enablerssfeeds": False,
}

_GUISETTINGS_SEED = (
    '<settings version="2">'
    '<setting id="lookandfeel.skin" default="true">skin.estuary</setting>'
    "</settings>"
)

_SOURCES_SEED = (
    "<sources><video><default pathversion=\"1\" /></video>"
    "<files><default pathversion=\"1\" />"
    "<source><name>Mine</name>"
    "<path pathversion=\"1\">/somewhere/mine/</path>"
    "<allowsharing>true</allowsharing></source>"
    "</files></sources>"
)


def _rig(monkeypatch, tmp_path, platform="tvos", answer_dialogs=True):
    """The full live rig: two-layer storage fake + the FakeKodi RPC surface,
    with the REAL profile, nsud and _kodisettings modules bound to them."""
    store = FakeKodiStorage(tmp_path / "kodi", platform=platform)
    store.log = []
    store.seed_disk("guisettings.xml", _GUISETTINGS_SEED.encode())
    store.seed_disk("sources.xml", _SOURCES_SEED.encode())
    xbmc_cls, vfs_cls = make_modules(store)
    addons_home = Path(store.translate("special://home/addons/"))
    addons_home.mkdir(parents=True, exist_ok=True)

    settings = dict(_SEED_SETTINGS)
    kodi = FakeKodi(store, settings, addons_home)
    kodi.answer_dialogs = answer_dialogs
    for sid in ("addons.unknownsources", "services.webserver",
                "services.esallinterfaces"):
        kodi.gated[sid] = {"addons.unknownsources": 36618,
                          "services.webserver": 36632,
                          "services.esallinterfaces": 36633}[sid]

    xbmc_mod = types.ModuleType("xbmc")
    for attr in ("LOGDEBUG", "LOGINFO", "LOGWARNING", "LOGERROR"):
        setattr(xbmc_mod, attr, getattr(xbmc_cls, attr))
    xbmc_mod.log = xbmc_cls.log
    xbmc_mod.getCondVisibility = kodi.getCondVisibility
    xbmc_mod.executeJSONRPC = kodi.executeJSONRPC
    xbmc_mod.executebuiltin = kodi.executebuiltin
    xbmc_mod.getInfoLabel = kodi.getInfoLabel
    xbmc_mod.getLocalizedString = kodi.getLocalizedString
    xbmc_mod.sleep = lambda ms: None

    xbmcvfs_mod = types.ModuleType("xbmcvfs")
    xbmcvfs_mod.File = vfs_cls.File
    xbmcvfs_mod.exists = vfs_cls.exists
    xbmcvfs_mod.delete = vfs_cls.delete
    xbmcvfs_mod.translatePath = vfs_cls.translatePath

    own_settings = {}
    profile = _import_profile(
        monkeypatch, xbmc_mod=xbmc_mod, xbmcvfs_mod=xbmcvfs_mod,
        addon_settings=own_settings,
    )
    monkeypatch.setattr(profile, "_CONFIRM_TIMEOUT_S", 2.0)
    monkeypatch.setattr(profile, "_ENABLE_POLL_S", 1.0)
    monkeypatch.setattr(profile, "_REFRESH_POLL_S", 1.0)

    vector_writes = []
    real_vfs_write = store.vfs_write

    def counting_vfs_write(path, data):
        if store.wants(path):
            vector_writes.append(store._userdata_rel(store.translate(path)))
        return real_vfs_write(path, data)

    monkeypatch.setattr(store, "vfs_write", counting_vfs_write)
    return types.SimpleNamespace(
        profile=profile, store=store, kodi=kodi, settings=settings,
        own_settings=own_settings, vectors=vector_writes,
    )


# --------------------------------------------------------------------------- #
# 2. Adversarial: these fail on the PRE-fix shapes, or they prove nothing
# --------------------------------------------------------------------------- #
def test_adversarial_per_item_persist_loop_fails_on_tvos(monkeypatch, tmp_path):
    """The 4.1 trap, demonstrated against the fake so the fake provably
    catches it: persist_one drops the POSIX copy after a confirmed vector, so
    the SECOND write_guisetting of a per-item loop returns a bare False. The
    shipped code avoids this by design (one merged write, one vector); anyone
    who rewrites it as a loop turns this red."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    from resources.lib.modules import _kodisettings, nsud

    posix = rig.store.translate("special://profile/guisettings.xml")
    assert _kodisettings.write_guisetting(posix, "a.first", "1") is True
    assert nsud.persist_one("guisettings.xml") is True
    assert rig.store.state("guisettings.xml") == "key-only", (
        "the fake no longer drops the POSIX copy after a vector; every "
        "per-item-loop assertion below is void"
    )
    assert _kodisettings.write_guisetting(posix, "b.second", "2") is False, (
        "write_guisetting succeeded against a dropped POSIX copy - the "
        "per-item loop bug (plan 4.1) would ship undetected"
    )


def test_adversarial_whole_document_class_c_write_destroys_sources(
    monkeypatch, tmp_path
):
    """A whole-document copy of the bundle's sources.xml MUST lose the box's
    pre-existing source. If this test ever fails, the entries-only document
    has grown into a copyable whole and the merge is no longer load-bearing."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    bundle_doc = (HOUSE / "sources.xml").read_text()
    posix = Path(rig.store.translate("special://profile/sources.xml"))
    posix.write_text(bundle_doc)  # the naive "copy the bundle file" move
    survived = "Mine" in posix.read_text()
    assert not survived, (
        "the whole-document write preserved the pre-existing source; the "
        "adversarial premise is broken"
    )


# --------------------------------------------------------------------------- #
# 3. The positive counterpart: apply() on the tvOS storage fake
# --------------------------------------------------------------------------- #
def test_apply_lands_every_class_a_id_with_exactly_one_vector(
    monkeypatch, tmp_path
):
    """Execute apply() for real against the two-layer tvOS fake: every class A
    id present in the final vectored artifact with the bundle's value, exactly
    ONE guisettings vector taken, the pre-existing source preserved, all three
    bundle sources merged, the add-ons staged and enabled with the repository
    LAST, and the own-settings leaf applied via setSetting. This guards the
    SHIPPED code the way the adversarial cases guard the fake."""
    import xml.etree.ElementTree as ET

    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    bundle = rig.profile.load(str(HOUSE), "tvos")
    ops = rig.profile.plan(bundle)
    record = rig.profile.apply(ops, on_step=lambda i, n, t: None)
    failures = [
        it for it in record["items"]
        if it["outcome"] not in ("applied", "already-correct")
    ]
    assert not failures, "unexpected failures: %r" % failures

    # class A: the final artifact is the NSUserDefaults key (POSIX dropped)
    assert rig.store.state("guisettings.xml") == "key-only"
    final = rig.store.vfs_read("special://profile/guisettings.xml")
    root = ET.fromstring(bytes(final))
    on_disk = {n.get("id"): (n.text or "") for n in root.iter("setting")}
    for sid, text in bundle["class_a"]:
        assert on_disk.get(sid) == text, "%s missing from the artifact" % sid
        assert rig.settings[sid] == rig.profile.coerce(text, rig.settings[sid]), (
            "%s not live-set" % sid
        )
    assert on_disk.get("lookandfeel.skin") == "skin.estuary", (
        "the re-materialize/merge path lost a pre-existing setting"
    )
    assert rig.vectors.count("guisettings.xml") == 1, (
        "class A must take exactly ONE vector (plan 4.1)"
    )

    # class C: merged, not copied
    src_final = bytes(rig.store.vfs_read("special://profile/sources.xml"))
    sroot = ET.fromstring(src_final)
    names = {
        (s.findtext("name") or "").strip()
        for s in sroot.find("files").findall("source")
    }
    assert "Mine" in names, "the merge clobbered a pre-existing source"
    assert {".T7B", "KodiShare", "KodiBackup"} <= names
    assert sroot.find("video") is not None, (
        "the merge dropped the box's other source sections"
    )
    assert rig.vectors.count("sources.xml") == 1

    # class D: staged, enabled, repo enable is the LAST enable event
    assert rig.kodi.addons["script.image.resource.select"] is True
    assert rig.kodi.addons["repository.tony7bones"] is True
    enables = [e for e in rig.kodi.events if e[0] == "enable"]
    assert enables[-1] == ("enable", "repository.tony7bones"), (
        "the T7B repository must be enabled LAST (plan 4.4)"
    )

    # own settings via setSetting only
    assert rig.own_settings["download.path"].endswith("/tvos/")
    assert rig.own_settings["restore.path"].endswith("/tvos/")

    # verify() agrees
    vitems = rig.profile.verify(ops)
    assert all(v["outcome"] == "applied" for v in vitems), vitems


def test_second_apply_is_already_correct_and_takes_no_new_vector(
    monkeypatch, tmp_path
):
    """The idempotency claim, observable: a re-run reports already-correct per
    item and takes NO storage action - no second guisettings vector, no second
    sources vector (4.3 property 5; on tvOS a no-op rewrite would still be a
    key rewrite plus a POSIX drop, a real mutation on a run that changed
    nothing)."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    bundle = rig.profile.load(str(HOUSE), "tvos")
    ops = rig.profile.plan(bundle)
    rig.profile.apply(ops)
    first_vectors = list(rig.vectors)
    record = rig.profile.apply(ops)
    outcomes = {it["outcome"] for it in record["items"]}
    assert outcomes == {"already-correct"}, record["items"]
    assert rig.vectors == first_vectors, (
        "a changed-nothing re-run mutated the storage layer"
    )


def test_unanswered_confirm_is_an_honest_timeout_not_a_hang(
    monkeypatch, tmp_path
):
    """E3's hang case: when nothing can answer Kodi's confirm, the set must
    come back as a bounded refusal/timeout with the dialog closed behind it,
    never a wedged flow and never a false 'applied'."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos", answer_dialogs=False)
    bundle = rig.profile.load(str(HOUSE), "tvos")
    ops = [op for op in rig.profile.plan(bundle)
           if op["kind"] == "set" and op["id"] == "addons.unknownsources"]
    record = rig.profile.apply(ops)
    (item,) = record["items"]
    assert item["outcome"] in ("refused", "timeout"), item
    assert rig.settings["addons.unknownsources"] is False, (
        "an unanswered confirm must leave the old value standing"
    )
    assert rig.kodi.dialog is None, "the confirm dialog was left on screen"


def test_unknown_live_id_reports_per_item_not_abort(monkeypatch, tmp_path):
    """A live catalog that moved under a validated bundle produces a per-item
    unknown-id and the rest of the apply proceeds (plan 7.1/7.5)."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    del rig.settings["epg.selectaction"]
    bundle = rig.profile.load(str(HOUSE), "tvos")
    ops = [op for op in rig.profile.plan(bundle) if op["kind"] == "set"]
    record = rig.profile.apply(ops)
    by_label = {it["label"]: it["outcome"] for it in record["items"]}
    assert by_label["epg.selectaction"] == "unknown-id"
    assert by_label["services.webserverport"] == "applied"


def test_addon_data_for_an_enabled_addon_is_refused_not_clobbered(
    monkeypatch, tmp_path
):
    """Plan 4.4 / open item 6: writing a third-party addon_data file while its
    owner is ENABLED is restore defect A with a new owner (the live add-on
    flushes its in-memory settings over the file at shutdown). Until the owner
    sanctions a bounded disable/re-enable, that leaf reports refused - and a
    DISABLED owner gets the write, before enablement, comment-free."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    doc = '<settings version="2"><setting id="k">v</setting></settings>'
    op = {"kind": "addon-data", "addon": "some.addon", "rel": "settings.xml",
          "xml": doc}
    # enabled owner: refused
    rig.kodi.addons["some.addon"] = True
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "refused"
    assert rig.store.state("addon_data/some.addon/settings.xml") == "absent"
    # disabled owner: written and vectored (settings.xml IS Kodi-read)
    rig.kodi.addons["some.addon"] = False
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "applied"
    assert rig.store.state("addon_data/some.addon/settings.xml") in (
        "key-only", "both"
    )


def test_ok_dialog_veto_is_a_bounded_refusal_with_kodis_words(
    monkeypatch, tmp_path
):
    """The failure the first full bench run actually hit (2026-08-30): a set
    vetoed by an OK dialog from OnSettingChanging (webserver enabled while
    auth was on with no password, string 36635). The engine must capture
    Kodi's explanation as the refusal detail, close the dialog so nothing
    downstream trips on it, and never time out."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    veto_text = "If web server authentication is enabled, a password must be entered as well."
    rig.kodi.ok_veto["services.webserver"] = veto_text
    op = {"kind": "set", "id": "services.webserver", "value": "true",
          "confirm": True}
    record = rig.profile.apply([op])
    (item,) = record["items"]
    assert item["outcome"] == "refused", item
    assert veto_text in item["detail"], (
        "Kodi's own explanation must be the refusal detail"
    )
    assert rig.kodi.dialog is None, "the OK dialog was left on screen"
    assert rig.settings["services.webserver"] is False


def test_unexpected_confirm_on_a_plain_set_is_bounded_and_closed(
    monkeypatch, tmp_path
):
    """A NON-gated id that unexpectedly posts a yes/no must not wedge the flow:
    the bound applies to EVERY set (plan 7.5's condition for shipping class A
    as a loop), the foreign dialog is closed unanswered, and the outcome is an
    honest refusal."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    rig.kodi.gated["epg.selectaction"] = 36633  # a surprise confirm
    op = {"kind": "set", "id": "epg.selectaction", "value": "1", "confirm": False}
    record = rig.profile.apply([op])
    (item,) = record["items"]
    assert item["outcome"] == "refused", item
    assert rig.kodi.dialog is None, "the surprise dialog was left on screen"
    assert rig.settings["epg.selectaction"] == 2, "a closed confirm must not commit"


# --------------------------------------------------------------------------- #
# 4. The marker helpers (tools.py)
# --------------------------------------------------------------------------- #
def test_profile_marker_roundtrip(monkeypatch, tmp_path):
    """Arm, read, clear - and an unreadable marker reads as None (nothing
    owed), never as a finding."""
    import importlib as _importlib

    xbmc_mod = types.ModuleType("xbmc")
    xbmc_mod.log = lambda *a, **k: None
    xbmc_mod.getInfoLabel = lambda label: ""
    xbmc_mod.executeJSONRPC = lambda raw: "{}"
    xbmc_mod.LOGDEBUG = 0
    xbmc_mod.LOGINFO = 1
    xbmc_mod.LOGWARNING = 2
    xbmc_mod.LOGERROR = 3
    xbmc_mod.getCondVisibility = lambda c: False
    xbmc_mod.sleep = lambda ms: None
    xbmcaddon_mod = types.ModuleType("xbmcaddon")
    xbmcaddon_mod.Addon = lambda *a, **k: types.SimpleNamespace(
        getSetting=lambda k2: "", setSetting=lambda k2, v: None,
        getAddonInfo=lambda k2: "",
    )
    xbmcgui_mod = types.ModuleType("xbmcgui")
    xbmcgui_mod.Dialog = lambda: types.SimpleNamespace(
        ok=lambda *a, **k: None, notification=lambda *a, **k: None,
    )
    xbmcvfs_mod = types.ModuleType("xbmcvfs")
    xbmcvfs_mod.translatePath = lambda p: p.replace(
        "special://home/", str(tmp_path) + "/"
    )
    xbmcvfs_mod.exists = lambda p: Path(p).exists()
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_mod)
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon_mod)
    monkeypatch.setitem(sys.modules, "xbmcgui", xbmcgui_mod)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs_mod)
    ui_stub = types.ModuleType("resources.lib.modules.ui")
    monkeypatch.setitem(sys.modules, "resources.lib.modules.ui", ui_stub)
    b2f = types.ModuleType("resources.lib.modules.backtothefuture")
    b2f.PY2 = False
    b2f.unicode = str
    monkeypatch.setitem(
        sys.modules, "resources.lib.modules.backtothefuture", b2f
    )
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if name == "resources.lib.modules.tools" or (
            name.startswith("resources") and name.count(".") < 3
        ):
            monkeypatch.delitem(sys.modules, name, raising=False)
    tools = _importlib.import_module("resources.lib.modules.tools")

    assert tools.profile_check_pending() is False
    assert tools.read_profile_check() is None
    payload = {"box": "aa:bb", "sources": ["nfs://h/x/"], "settings": {"a": "1"}}
    assert tools.mark_profile_check_pending(payload) is True
    assert tools.profile_check_pending() is True
    assert tools.read_profile_check() == payload
    # corruption reads as None, not a crash and not a finding
    Path(tools.PROFILE_CHECK_MARKER).write_text("{nope")
    assert tools.read_profile_check() is None
    tools.clear_profile_check_marker()
    assert tools.profile_check_pending() is False
    tools.clear_profile_check_marker()  # idempotent


def test_house_zip_payloads_are_wellformed():
    """The staged add-on zips must be rooted at their id and carry addon.xml -
    the same structural facts load() checks, asserted here directly against
    the shipped bundle so a bad re-copy fails loudly."""
    for aid, zname in (
        ("repository.tony7bones", "repository.tony7bones-3.0.0.zip"),
        ("script.image.resource.select", "script.image.resource.select-3.0.2.zip"),
    ):
        with zipfile.ZipFile(HOUSE / "addons" / zname) as z:
            names = z.namelist()
        assert all(n.startswith(aid + "/") for n in names)
        assert (aid + "/addon.xml") in names
