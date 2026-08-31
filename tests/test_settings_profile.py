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
import xml.etree.ElementTree as ET
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
        xbmc_mod.getSkinDir = lambda: ""
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
    nodes_xml=None,
    rssfeeds_xml=None,
    classes=("fireos", "tvos", "androidtv", "bench"),
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
    if nodes_xml is not None:
        _write(b / "nodes.d" / "50-test.xml", nodes_xml)
    if rssfeeds_xml is not None:
        _write(b / "RssFeeds.xml", rssfeeds_xml)
    for aid, files in (addon_data or {}).items():
        for fname, body in files.items():
            _write(b / "overlays" / "fireos" / "addon_data" / aid / fname, body)
    return b


# --------------------------------------------------------------------------- #
# 1. load() and plan(): the pure surface
# --------------------------------------------------------------------------- #
def test_house_bundle_loads_for_every_device_class(monkeypatch):
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "androidtv", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        assert bundle["device_class"] == cls
        assert len(bundle["class_a"]) == 18, (
            "the House bundle carries 18 class A ids (13 from the plan, the "
            "three added to bootstrapper after 2026-08-04, the "
            "filecache.memorysize convergence pin added 2026-08-30, plus "
            "weather.addon added 2026-08-31)"
        )
        assert len(bundle["sources"]) == 3
        assert {a["id"] for a in bundle["addons"]} == {
            "repository.tony7bones",
            "script.image.resource.select",
            "script.module.six",
            "script.module.soupsieve",
            "script.module.dateutil",
            "script.module.beautifulsoup4",
            "script.openweathermap.maps",
            "weather.multi",
        }


def test_house_bundle_passes_the_authoring_catalog_gate(monkeypatch):
    """THE CI AUTHORING GATE (plan 7.1): every id in the shipped bundle exists
    in the catalog captured from a first-run Kodi 22 Piers profile. A renamed
    or typo'd id fails HERE, in CI, instead of silently doing nothing on a
    box. At runtime the same check is deliberately absent: a moved live
    catalog produces a per-item unknown-id outcome, never an aborted apply."""
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "androidtv", "bench"):
        bundle = profile.load(str(HOUSE), cls, known_ids=_catalog())
        assert len(bundle["class_a"]) == 18, cls


def test_house_overlays_differ_per_class_and_bench_is_deliberate(monkeypatch):
    """The backup folder leaf is overlay-only: tvos gets tvos/, fireos gets
    fireos/, androidtv gets androidtv/ (the 2026-08-31 split - before it the
    Shield landed in the Fire TV folder), and the bench REPRODUCES the fireos
    leaf on purpose (the bench has always been seeded with it; plan 7.1)."""
    profile = _import_profile(monkeypatch)
    leaves = {}
    for cls in ("fireos", "tvos", "androidtv", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        own = bundle["addon_data"]["script.ezmaintenanceplusplus"]["settings.xml"]
        leaves[cls] = dict(own["pairs"])["download.path"]
    assert leaves["fireos"].endswith("/fireos/")
    assert leaves["tvos"].endswith("/tvos/")
    assert leaves["androidtv"].endswith("/androidtv/")
    assert leaves["bench"] == leaves["fireos"]


def _rig_detector(monkeypatch, profile, tvos, android, out=None, raises=None):
    """Point detect_device_class at a scripted platform: the two condvis flags
    and what getprop returns (or raises). Returns the list of spawned argvs so
    a test can assert getprop is only ever consulted on Android."""
    flags = {"System.Platform.TVOS": tvos, "System.Platform.Android": android}
    monkeypatch.setattr(profile.xbmc, "getCondVisibility", lambda f: flags[f])
    calls = []

    def check_output(argv, timeout=None):
        calls.append(list(argv))
        if raises is not None:
            raise raises
        return out

    monkeypatch.setattr(profile.subprocess, "check_output", check_output)
    return calls


def test_detect_device_class_is_a_four_way_split(monkeypatch):
    """The classification (owner-approved 2026-08-31): TVOS -> tvos; Android
    with an Amazon manufacturer -> fireos; Android without -> androidtv;
    neither flag -> bench. Kodi has no Amazon flag, so the Android split reads
    ``ro.product.manufacturer`` via getprop - the byte strings here are the
    ones MEASURED from inside Kodi's own Python on 2026-08-31 (RunScript in a
    running Kodi): the bedroom Fire TV (AFTHA001) returned b"Amazon\n", the
    Shield returned b"NVIDIA\n"."""
    profile = _import_profile(monkeypatch)

    calls = _rig_detector(monkeypatch, profile, tvos=True, android=True)
    assert profile.detect_device_class() == "tvos"
    assert calls == [], "tvOS must classify on the flag alone, no getprop"

    calls = _rig_detector(
        monkeypatch, profile, tvos=False, android=True, out=b"Amazon\n"
    )
    assert profile.detect_device_class() == "fireos"
    assert calls == [["/system/bin/getprop", "ro.product.manufacturer"]]

    calls = _rig_detector(
        monkeypatch, profile, tvos=False, android=True, out=b"NVIDIA\n"
    )
    assert profile.detect_device_class() == "androidtv"

    calls = _rig_detector(monkeypatch, profile, tvos=False, android=False)
    assert profile.detect_device_class() == "bench"
    assert calls == [], "a non-Android box must never spawn getprop"


def test_android_manufacturer_probe_failure_falls_back_to_fireos(monkeypatch):
    """The safe default, by design: fireos is what EVERY Android box was
    classified as before the 2026-08-31 split, so any probe failure - missing
    binary, hung getprop, empty value - keeps a box where it already was.
    A failure may leave the Shield in the old (wrong) folder; it must never
    strand a Fire box in a NEW wrong one."""
    profile = _import_profile(monkeypatch)

    for reason, kwargs in (
        ("getprop missing", {"raises": OSError("no such file")}),
        (
            "getprop hung",
            {
                "raises": __import__("subprocess").TimeoutExpired(
                    ["/system/bin/getprop"], 10
                )
            },
        ),
        ("empty value", {"out": b""}),
        ("whitespace value", {"out": b"  \n"}),
    ):
        _rig_detector(monkeypatch, profile, tvos=False, android=True, **kwargs)
        assert profile.detect_device_class() == "fireos", reason

    # And the pre-existing outermost net: a condvis blow-up still lands on
    # bench, exactly as before the split.
    def boom(flag):
        raise RuntimeError("no xbmc")

    monkeypatch.setattr(profile.xbmc, "getCondVisibility", boom)
    assert profile.detect_device_class() == "bench"


def test_amazon_match_is_case_insensitive_and_exact(monkeypatch):
    """"Amazon" however cased is Amazon; any OTHER manufacturer, however
    exotic, is androidtv - the equality is exact, so a hypothetical
    "Amazonia" vendor is not swallowed into fireos."""
    profile = _import_profile(monkeypatch)
    for out, want in (
        (b"amazon\n", "fireos"),
        (b"AMAZON\n", "fireos"),
        (b"Amazonia\n", "androidtv"),
        (b"NVIDIA\n", "androidtv"),
    ):
        _rig_detector(monkeypatch, profile, tvos=False, android=True, out=out)
        assert profile.detect_device_class() == want, out


def test_house_androidtv_overlay_points_both_paths_at_the_androidtv_folder(
    monkeypatch,
):
    """The whole point of the split: the Shield's backup and restore defaults
    land in Backup/androidtv/ on the mini share, not the Fire TV folder."""
    profile = _import_profile(monkeypatch)
    bundle = profile.load(str(HOUSE), "androidtv")
    own = bundle["addon_data"]["script.ezmaintenanceplusplus"]["settings.xml"]
    pairs = dict(own["pairs"])
    assert (
        pairs["download.path"]
        == "nfs://192.168.7.2/Users/moquette/Kodi/Backup/androidtv/"
    )
    assert pairs["restore.path"] == pairs["download.path"]
    # No event-server override rides along: the esenabled false workaround is
    # tvOS-specific (the 2026-08-28 watchdog kill), never Android's.
    assert dict(bundle["class_a"])["services.esenabled"] == "true"


def test_androidtv_addition_left_the_existing_overlay_files_byte_unchanged():
    """The 2026-08-31 androidtv split ADDED a tree; the three existing classes
    ship the exact bytes they shipped before it. Pinned by content hash, so a
    tidy-in-passing rewrite fails here even when the values survive. A later
    DELIBERATE overlay change updates these pins in the same commit."""
    import hashlib

    pins = {
        "overlays/fireos/addon_data/script.ezmaintenanceplusplus/settings.xml":
            "1149adf5a98b842de941ae2ad7f1beb1430fd5bf6d280055f6afe5a5be310421",
        "overlays/bench/addon_data/script.ezmaintenanceplusplus/settings.xml":
            "1149adf5a98b842de941ae2ad7f1beb1430fd5bf6d280055f6afe5a5be310421",
        "overlays/tvos/addon_data/script.ezmaintenanceplusplus/settings.xml":
            "e949c7d8418e8374adb2ef498c939fb4ebcbdc3f2ec24e896aee095cbeedfad7",
        "overlays/tvos/settings.d/20-services.xml":
            "772a8934cb378847da57eaaa3afeb8b5c5da880f101507f6e5ec64f96bf2fdbb",
    }
    for rel, want in pins.items():
        got = hashlib.sha256((HOUSE / rel).read_bytes()).hexdigest()
        assert got == want, "%s changed bytes" % rel


def test_house_esenabled_split_tvos_false_others_true(monkeypatch):
    """The event server split, pinned: tvOS carries services.esenabled FALSE
    (the 2026-08-28 watchdog-kill workaround, owner-approved as the recorded
    tvOS state - a profile run must never undo it), while fireos, androidtv
    and the bench keep TRUE (the workaround is tvOS-specific by design). On every class the id keeps its base POSITION, before
    services.esallinterfaces (parent before dependent), which is the exact
    trap the position-from-first merge rule exists for."""
    profile = _import_profile(monkeypatch)
    for cls, want in (
        ("fireos", "true"),
        ("androidtv", "true"),
        ("bench", "true"),
        ("tvos", "false"),
    ):
        bundle = profile.load(str(HOUSE), cls)
        values = dict(bundle["class_a"])
        assert values["services.esenabled"] == want, (
            "%s must ship services.esenabled=%s" % (cls, want)
        )
        order = [sid for sid, _ in bundle["class_a"]]
        assert order.index("services.esenabled") < order.index(
            "services.esallinterfaces"
        ), "%s: the overlay override moved esenabled after its dependent" % cls


def test_house_pins_filecache_memorysize_to_20_on_every_class(monkeypatch):
    """The video cache buffer convergence pin (owner accepted 2026-08-30): 20,
    Kodi's own default and the exact value every restore resets the id to
    (tools.KODI_DEFAULT_MB aliases the same _kodisettings constant, so the two
    mechanisms cannot drift). ALL device classes, no overlay split - the point
    is fleet convergence; before the pin the id floated across restore/flush
    cycles (archive 64, box 20, measured on atv1, 2026-08-30)."""
    profile = _import_profile(monkeypatch)
    from resources.lib.modules import _kodisettings

    for cls in ("fireos", "tvos", "androidtv", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        values = dict(bundle["class_a"])
        assert values.get("filecache.memorysize") == "20", (
            "%s must pin filecache.memorysize to 20" % cls
        )
    # The pinned value IS the restore-reset target, by construction not by luck.
    assert str(_kodisettings.KODI_DEFAULT_CACHE_MB) == "20"


def test_filecache_pin_rejected_at_any_value_but_the_restore_reset_target(
    monkeypatch, tmp_path
):
    """The carve-out is the VALUE, not the id: a bundle pinning any number the
    restore does not reset to would flip-flop the fleet between the profile and
    every restore, forever. Rejected at load, per occurrence, overlays included."""
    profile = _import_profile(monkeypatch)
    ok = make_bundle(
        tmp_path / "ok",
        fragments={
            "20-x.xml": '<settings version="2">'
            '<setting id="filecache.memorysize">20</setting></settings>'
        },
    )
    bundle = profile.load(str(ok), "fireos")
    assert ("filecache.memorysize", "20") in bundle["class_a"]

    bad = make_bundle(
        tmp_path / "bad",
        fragments={
            "20-x.xml": '<settings version="2">'
            '<setting id="filecache.memorysize">64</setting></settings>'
        },
    )
    with pytest.raises(profile.ProfileError) as e:
        profile.load(str(bad), "fireos")
    assert "20" in str(e.value)

    # An overlay sneaking a different value over a clean base is caught too:
    # the check runs per OCCURRENCE, not on the merged winner alone.
    sneaky = make_bundle(
        tmp_path / "sneaky",
        fragments={
            "20-x.xml": '<settings version="2">'
            '<setting id="filecache.memorysize">20</setting></settings>'
        },
        overlay_fragments={
            "tvos": {
                "20-x.xml": '<settings version="2">'
                '<setting id="filecache.memorysize">200</setting></settings>'
            }
        },
    )
    with pytest.raises(profile.ProfileError):
        profile.load(str(sneaky), "tvos")


def test_unresolved_device_class_is_a_hard_failure(monkeypatch):
    # androidtv graduated to a real class 2026-08-31, so the unknown example
    # here has to be something the fleet will never grow.
    profile = _import_profile(monkeypatch)
    with pytest.raises(profile.ProfileError):
        profile.load(str(HOUSE), "webos")


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
    assert kinds.count("guisettings-nodes") == 1
    assert kinds.count("rss-feeds") == 1
    # the last op is the repo enable
    assert ops[-1]["kind"] == "enable"
    assert ops[-1]["addon"] == "repository.tony7bones"
    assert ops[-1]["last"] is True
    # ordering indexes
    idx = {k: kinds.index(k) for k in ("addon-data", "stage", "refresh",
                                       "enable", "set", "write-guisettings",
                                       "skin-bool", "guisettings-nodes",
                                       "sources", "rss-feeds")}
    assert idx["addon-data"] < idx["stage"], (
        "the weather location file must be on disk before weather.multi is "
        "ever known to Kodi (an enabled owner's live settings flush over the "
        "file at shutdown - defect A's shape)"
    )
    assert idx["stage"] < idx["refresh"] < idx["enable"] < idx["set"]
    assert idx["set"] < idx["write-guisettings"] < idx["skin-bool"]
    assert idx["skin-bool"] < idx["guisettings-nodes"]
    assert idx["guisettings-nodes"] < idx["sources"] < idx["rss-feeds"]
    # parent before dependent ACROSS classes: weather.multi must be enabled
    # before the weather.addon set, or Kodi rejects the provider id (the
    # addon-type setting validates against enabled add-ons)
    enable_weather = next(
        i for i, op in enumerate(ops)
        if op["kind"] == "enable" and op["addon"] == "weather.multi"
    )
    set_provider = next(
        i for i, op in enumerate(ops)
        if op["kind"] == "set" and op["id"] == "weather.addon"
    )
    assert enable_weather < set_provider, (
        "weather.multi must be enabled before weather.addon is set"
    )
    # and its four bundled pure-python deps enable before weather.multi itself
    enables = [op["addon"] for op in ops if op["kind"] == "enable"]
    for dep in ("script.module.six", "script.module.soupsieve",
                "script.module.dateutil", "script.module.beautifulsoup4",
                "script.openweathermap.maps"):
        assert enables.index(dep) < enables.index("weather.multi"), dep
    # the addon-data op carries the pairs the values-idempotence compare needs
    weather_data = next(
        op for op in ops
        if op["kind"] == "addon-data" and op["addon"] == "weather.multi"
    )
    assert weather_data["pairs"], "addon-data ops must carry their pairs"
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
        # The live skin surface. skin_bools models CSkinSettings; the
        # Skin.HasSetting probe below reproduces TranslateBool's measured
        # MUTATION (an unseen id is inserted default-false), so a test can
        # never mistake the probe for a read-only one.
        self.skin = "skin.estuary"
        self.skin_bools = {}

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

    def executebuiltin(self, cmd, wait=False):
        self.builtins.append(cmd)
        if cmd.startswith("Skin.SetBool("):
            inner = cmd[len("Skin.SetBool("):-1]
            sid, _, val = inner.partition(",")
            self.skin_bools[sid.strip()] = val.strip() == "true"
        elif cmd == "UpdateLocalAddons":
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
        if cond.startswith("Skin.HasSetting("):
            sid = cond[len("Skin.HasSetting("):-1].strip()
            # TranslateBool's measured side effect: probing an unseen id
            # INSERTS it, default false, and schedules a save.
            if sid not in self.skin_bools:
                self.skin_bools[sid] = False
            return self.skin_bools[sid]
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
    # A fresh box has no weather provider (guisettings carries the id at its
    # empty default - measured in the ts1 before capture, 2026-08-31).
    "weather.addon": "",
    # A drifted box (the atv1 archive carried 64): the convergence pin must
    # actually move it to 20, not find it already there.
    "filecache.memorysize": 64,
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
    xbmc_mod.getSkinDir = lambda: kodi.skin
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
    for aid in ("script.module.six", "script.module.soupsieve",
                "script.module.dateutil", "script.module.beautifulsoup4",
                "script.openweathermap.maps", "weather.multi"):
        assert rig.kodi.addons[aid] is True, "%s not enabled" % aid
    enables = [e for e in rig.kodi.events if e[0] == "enable"]
    assert enables[-1] == ("enable", "repository.tony7bones"), (
        "the T7B repository must be enabled LAST (plan 4.4)"
    )

    # the weather payload: provider live, location file on the layer Kodi
    # reads with the ts1-measured values, skin toggle set on the live skin
    assert rig.settings["weather.addon"] == "weather.multi"
    wraw = bytes(
        rig.store.vfs_read("special://profile/addon_data/weather.multi/settings.xml")
    )
    wvals = {
        n.get("id"): (n.text or "")
        for n in ET.fromstring(wraw).iter("setting")
    }
    assert wvals == {
        "loc1_name": "Sacramento, CA, US",
        "loc1_url": "us/ca/sacramento",
        "loc1_lat": "38.675",
        "loc1_lon": "-121.525",
        "loc1_id": "12798021",
    }
    assert rig.vectors.count("addon_data/weather.multi/settings.xml") == 1
    assert rig.kodi.skin_bools.get("show_weatherinfo") is True

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
    # ONE deliberate carve-out: the guisettings-nodes op stays "applied" while
    # its shutdown-window write is still pending (the level is genuinely NOT
    # correct yet - the file reads 1 until Kodi closes - and reporting
    # already-correct here would suppress the restart offer the landing
    # depends on). It re-arms the same marker, which is not a userdata
    # storage layer. Everything else is already-correct, and the storage
    # vectors are untouched.
    for it in record["items"]:
        want = "applied" if it["kind"] == "guisettings-nodes" else "already-correct"
        assert it["outcome"] == want, it
    assert rig.vectors == first_vectors, (
        "a changed-nothing re-run mutated the storage layer"
    )
    # After the shutdown-window write lands, the THIRD run is pure
    # already-correct: the true idempotent fixed point.
    assert rig.profile.flush_deferred_guisettings_nodes(log=lambda m: None)
    third_vectors = list(rig.vectors)
    record = rig.profile.apply(ops)
    outcomes = {it["outcome"] for it in record["items"]}
    assert outcomes == {"already-correct"}, record["items"]
    assert rig.vectors == third_vectors, (
        "the post-landing re-run mutated the storage layer"
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
        ("script.module.six", "script.module.six-1.16.0+matrix.1.zip"),
        ("script.module.soupsieve", "script.module.soupsieve-2.4.1.zip"),
        ("script.module.dateutil", "script.module.dateutil-2.8.2.zip"),
        ("script.module.beautifulsoup4",
         "script.module.beautifulsoup4-4.12.2.zip"),
        ("script.openweathermap.maps", "script.openweathermap.maps-1.0.6.zip"),
        ("weather.multi", "weather.multi-1.1.6.zip"),
    ):
        with zipfile.ZipFile(HOUSE / "addons" / zname) as z:
            names = z.namelist()
        assert all(n.startswith(aid + "/") for n in names)
        assert (aid + "/addon.xml") in names


# The six weather-stack zips, pinned to the official mirrors.kodi.tv sha256
# sidecars fetched at authoring time (2026-08-31). "Byte-identical official
# artifact" is a claim; this makes a silent re-copy or corruption fail in CI,
# the same argument as HOUSE_RSS_MD5.
WEATHER_ZIP_SHA256 = {
    "script.module.six-1.16.0+matrix.1.zip":
        "15fa60d61fb067d4e81233109fc28c02310e3374798b2a88d9f399abf22958ef",
    "script.module.soupsieve-2.4.1.zip":
        "043395a24acc40412e615552f12aaf041c8a7cd78c3209e42949addbddf1b114",
    "script.module.dateutil-2.8.2.zip":
        "cc17745aa3e37ebd6709ab1042ee43405c992402b5f1d67eea260a99f51a9360",
    "script.module.beautifulsoup4-4.12.2.zip":
        "d6ec47f6cd94b5191f501882be2a670a35e41dadeb07fa32bdf9bb8c3fe63a2f",
    "script.openweathermap.maps-1.0.6.zip":
        "137569ea1632edeb009587176846f6aa23243c81a078c8fc2bc1d92213cdea50",
    "weather.multi-1.1.6.zip":
        "1ba06630390e8ea10127eea01a49d42ecb1969b40cf8b7905d28737ea4b5c0d8",
}


def test_house_weather_zips_are_the_official_artifacts():
    import hashlib

    for zname, want in WEATHER_ZIP_SHA256.items():
        raw = (HOUSE / "addons" / zname).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == want, zname


def test_house_weather_payload(monkeypatch):
    """The 2026-08-31 weather addition, measured off the owner's hand-set ts1:
    weather.multi staged with its four bundled pure-python deps plus the maps
    dependency (all normal mode, deps listed before their dependents), the
    provider id as the LAST class A entry, the complete loc1 block as
    addon_data, and the top-bar toggle as a skin bool - on every device
    class, no overlay split."""
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        values = dict(bundle["class_a"])
        assert values["weather.addon"] == "weather.multi", cls
        order = [sid for sid, _ in bundle["class_a"]]
        assert order[-1] == "weather.addon", (
            "%s: the provider id rides the last fragment (70-weather.xml)" % cls
        )
        doc = bundle["addon_data"]["weather.multi"]["settings.xml"]
        assert doc["pairs"] == [
            ("loc1_name", "Sacramento, CA, US"),
            ("loc1_url", "us/ca/sacramento"),
            ("loc1_lat", "38.675"),
            ("loc1_lon", "-121.525"),
            ("loc1_id", "12798021"),
        ], cls
        assert "<!--" not in doc["xml"], (
            "a comment in a Kodi-read settings.xml is a SIGABRT"
        )
        assert 'version="2"' in doc["xml"], (
            "values documents carry version 2: every Kodi this fleet has run "
            "accepts it, while the ts1 build's 4 is rejected by older builds"
        )
        assert bundle["skin_bools"] == [("show_weatherinfo", "true")], cls
        modes = {a["id"]: a["mode"] for a in bundle["addons"]}
        for aid in ("script.module.six", "script.module.soupsieve",
                    "script.module.dateutil", "script.module.beautifulsoup4",
                    "script.openweathermap.maps", "weather.multi"):
            assert modes[aid] == "normal", aid


def test_skinsettings_validation(monkeypatch, tmp_path):
    """Structural rejection for the skin-bool leaf: wrong root, a non-bool
    type (string support is deliberately unbuilt), a non-boolean value, an id
    unsafe to interpolate into a builtin, and a duplicate id."""
    profile = _import_profile(monkeypatch)
    cases = (
        ("<skinsettings><setting id='x' type='bool'>true</setting>"
         "</skinsettings>", "root element"),
        ("<settings><setting id='x' type='string'>v</setting></settings>",
         "only bool"),
        ("<settings><setting id='x' type='bool'>maybe</setting></settings>",
         "not true/false"),
        ("<settings><setting id='a,b' type='bool'>true</setting></settings>",
         "bad setting id"),
        ("<settings><setting id='x' type='bool'>true</setting>"
         "<setting id='x' type='bool'>false</setting></settings>",
         "duplicate"),
    )
    for body, needle in cases:
        b = make_bundle(tmp_path / needle.replace(" ", "_").replace("/", "-"))
        _write(b / "skinsettings.xml", body)
        with pytest.raises(profile.ProfileError) as e:
            profile.load(str(b), "fireos")
        assert any(needle in p for p in e.value.problems), (needle, e.value.problems)
    # and the well-formed shape loads
    b = make_bundle(tmp_path / "good")
    _write(
        b / "skinsettings.xml",
        "<settings><setting id='show_weatherinfo' type='bool'>true</setting>"
        "</settings>",
    )
    bundle = profile.load(str(b), "fireos")
    assert bundle["skin_bools"] == [("show_weatherinfo", "true")]


def test_addon_data_values_idempotence_beats_the_enabled_guard(
    monkeypatch, tmp_path
):
    """THE RE-RUN STATE, adversarial against the pre-2026.08.31.3 shape: after
    a converged first apply the owner add-on IS enabled and its file has been
    rewritten by Kodi with every default filled in (so byte equality is gone
    forever), yet every bundle value stands. The old enabled-first guard
    reported that as refused; the values compare must call it already-correct
    and touch NO storage layer. A file that genuinely differs still hits the
    guard: enabled owner, different value, refused, nothing written."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    # what Kodi leaves after weather.multi has run once: our values plus
    # defaults it filled in itself
    live = (
        '<settings version="4">'
        '<setting id="loc1_name">Sacramento, CA, US</setting>'
        '<setting id="loc1_url">us/ca/sacramento</setting>'
        '<setting id="loc1_lat">38.675</setting>'
        '<setting id="loc1_lon">-121.525</setting>'
        '<setting id="loc1_id">12798021</setting>'
        '<setting id="loc2_name" default="true" />'
        '<setting id="WAdd" default="true">false</setting>'
        "</settings>"
    )
    rig.store.seed_disk("addon_data/weather.multi/settings.xml", live.encode())
    rig.kodi.addons["weather.multi"] = True
    bundle = rig.profile.load(str(HOUSE), "tvos")
    op = next(
        o for o in rig.profile.plan(bundle)
        if o["kind"] == "addon-data" and o["addon"] == "weather.multi"
    )
    vectors_before = list(rig.vectors)
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "already-correct", record["items"]
    assert rig.vectors == vectors_before, (
        "a values-correct file must not be rewritten or re-vectored"
    )
    # a genuinely different value with the owner enabled: still refused
    drifted = live.replace("us/ca/sacramento", "us/ny/newyork")
    rig.store.seed_disk("addon_data/weather.multi/settings.xml", drifted.encode())
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "refused", record["items"]
    kept = bytes(
        rig.store.vfs_read("special://profile/addon_data/weather.multi/settings.xml")
    )
    assert b"us/ny/newyork" in kept, "a refused leaf must not write"


def test_skin_bool_apply_already_and_no_skin_paths(monkeypatch, tmp_path):
    """The three honest outcomes of the skin-bool leaf: set on the live skin
    (two-argument builtin, the wiz mechanism), already-correct with NO builtin
    emitted when the probe answers the bundle's value, and an explicit refusal
    when there is no live skin session to set it in."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    op = {"kind": "skin-bool", "id": "show_weatherinfo", "value": "true"}
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "applied", record["items"]
    assert "Skin.SetBool(show_weatherinfo,true)" in rig.kodi.builtins
    assert rig.kodi.skin_bools["show_weatherinfo"] is True
    # re-run: already-correct, no second builtin
    before = len([b for b in rig.kodi.builtins if b.startswith("Skin.SetBool")])
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "already-correct", record["items"]
    after = len([b for b in rig.kodi.builtins if b.startswith("Skin.SetBool")])
    assert after == before, "already-correct must emit no builtin"
    # no live skin session: refused, honestly, and nothing emitted
    rig.kodi.skin = ""
    rig.kodi.skin_bools.clear()
    record = rig.profile.apply([op])
    assert record["items"][0]["outcome"] == "refused", record["items"]
    assert "no live skin session" in record["items"][0]["detail"]
    assert not rig.kodi.skin_bools, "a refused leaf must not probe or set"


# --------------------------------------------------------------------------- #
# 5. Guisettings nodes (the expert settings level) and RssFeeds.xml -
#    the 2026.08.30.4 additions. Mechanism measured on the macOS bench,
#    2026-08-30 (Kodi 22.0-BETA1, a872eae1a5, isolated first-run HOME):
#    arm 1: a guisettings.xml file write made while Kodi runs is CLOBBERED by
#           the clean-close flush (wrote 3, quit, file read 1), while a
#           mid-session RssFeeds.xml write SURVIVES the same quit untouched;
#    arm 2: a write made in the service's abort window lands AFTER the one
#           "Saving settings" flush (log: flush 37.547s, abort 38.550, write
#           38.556) and the file holds 3 after exit;
#    arm 3: the relaunched GUI settings window read "Expert", and a further
#           clean quit with nothing armed re-serialized 3 (self-sustaining).
# --------------------------------------------------------------------------- #
HOUSE_RSS_MD5 = "70c3e435e2c272225142fd1dbba8b836"  # the owner's curated list

_FLUSHED_GUISETTINGS_STANDARD = (
    '<settings version="2">'
    '<setting id="lookandfeel.skin" default="true">skin.estuary</setting>'
    "<general><settinglevel>1</settinglevel></general>"
    "</settings>"
)


def test_house_carries_the_expert_settinglevel_node(monkeypatch):
    """The owner's expert UI ships as a file NODE (never a fake setting id),
    value 3, on EVERY device class, no overlay split."""
    profile = _import_profile(monkeypatch)
    for cls in ("fireos", "tvos", "bench"):
        bundle = profile.load(str(HOUSE), cls)
        assert bundle["nodes"] == [("general/settinglevel", "3")], cls


def test_house_rssfeeds_is_the_owners_curated_list(monkeypatch):
    """The bundle's RssFeeds.xml is the owner's curated list, md5 pinned: the
    file fetched from the mini's share minus the two dead kodi.tv addon feeds
    (404 on every boot, trimmed 2026.08.31.4), 6 feeds - a silent re-copy or
    hand-edit fails here, in CI."""
    import hashlib

    profile = _import_profile(monkeypatch)
    raw = (HOUSE / "RssFeeds.xml").read_bytes()
    assert hashlib.md5(raw).hexdigest() == HOUSE_RSS_MD5
    root = ET.fromstring(raw)
    feeds = [n for n in root.iter("feed") if (n.text or "").strip()]
    assert len(feeds) == 6
    for cls in ("fireos", "tvos", "bench"):
        assert profile.load(str(HOUSE), cls)["rssfeeds"] == raw, cls


def test_node_fragment_validation(monkeypatch, tmp_path):
    """Structural rejection: wrong root, a single-segment path (that is the
    <setting id> space's job), an uppercase path, a valueless node."""
    profile = _import_profile(monkeypatch)
    for bad, why in (
        ("<settings><node path='general/settinglevel'>3</node></settings>",
         "root element"),
        ("<nodes><node path='general'>3</node></nodes>", "bad node path"),
        ("<nodes><node path='General/Level'>3</node></nodes>", "bad node path"),
        ("<nodes><node path='general/settinglevel'></node></nodes>",
         "has no value"),
        ("<nodes><setting id='x'>3</setting></nodes>", "unexpected"),
    ):
        b = make_bundle(tmp_path / why.replace(" ", "_"), nodes_xml=bad)
        with pytest.raises(profile.ProfileError) as e:
            profile.load(str(b), "fireos")
        assert any(why in p for p in e.value.problems), (bad, e.value.problems)


def test_rssfeeds_validation(monkeypatch, tmp_path):
    """A truncated, misrooted or feedless RssFeeds.xml fails the LOAD loudly -
    validation failure applies nothing, so a mangled list can never be
    half-written onto a box."""
    profile = _import_profile(monkeypatch)
    for bad, why in (
        ("<rssfeeds><set id='1'><feed>x</feed></set>", "parse failure"),
        ("<feeds><set id='1'><feed>x</feed></set></feeds>", "root element"),
        ("<rssfeeds><set id='1'></set></rssfeeds>", "no <feed> entries"),
    ):
        b = make_bundle(tmp_path / why.replace(" ", "_"), rssfeeds_xml=bad)
        with pytest.raises(profile.ProfileError) as e:
            profile.load(str(b), "fireos")
        assert any(why in p for p in e.value.problems), (bad, e.value.problems)


def test_settinglevel_apply_arms_and_never_writes_guisettings(
    monkeypatch, tmp_path
):
    """Apply ARMS the deferred write and touches NO guisettings storage layer:
    the bench measured a live write being clobbered by the flush (arm 1), so a
    write here would be a false 'applied'. The marker is the whole action."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    before = bytes(rig.store.vfs_read("special://profile/guisettings.xml"))
    ops = [{"kind": "guisettings-nodes",
            "nodes": [("general/settinglevel", "3")]}]
    record = rig.profile.apply(ops)
    assert record["items"][0]["outcome"] == "applied"
    assert "boot check" in record["items"][0]["detail"]
    assert rig.profile.deferred_nodes_pending()
    assert rig.profile.read_deferred_nodes() == [("general/settinglevel", "3")]
    assert bytes(rig.store.vfs_read("special://profile/guisettings.xml")) == before
    assert rig.vectors == [], "arming must take no vector"


def test_settinglevel_apply_already_correct_arms_nothing(monkeypatch, tmp_path):
    """A box already at expert reports already-correct, arms no marker, and
    disarms a stale one - the shutdown window stays a one-stat no-op."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    rig.store.seed_disk(
        "guisettings.xml",
        b'<settings version="2"><general><settinglevel>3</settinglevel>'
        b"</general></settings>",
    )
    ops = [{"kind": "guisettings-nodes",
            "nodes": [("general/settinglevel", "3")]}]
    assert rig.profile.mark_deferred_nodes_pending(
        [("general/settinglevel", "3")]
    )  # a stale marker from an interrupted earlier run
    record = rig.profile.apply(ops)
    assert record["items"][0]["outcome"] == "already-correct"
    assert not rig.profile.deferred_nodes_pending()
    assert rig.vectors == []


def test_settinglevel_flush_lands_on_both_tvos_layers(monkeypatch, tmp_path):
    """The shutdown-window write against the two-layer fake: Kodi's flush has
    just rewritten guisettings (key layer, level 1); the flush patches the
    node, keeps every other setting, takes exactly ONE vector, ends key-only
    (POSIX dropped - both layers agree), and consumes the marker."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    ops = [{"kind": "guisettings-nodes",
            "nodes": [("general/settinglevel", "3")]}]
    rig.profile.apply(ops)
    # Kodi's own clean-shutdown flush, as it happens on tvOS: one VFS write.
    rig.store.vfs_write(
        "special://profile/guisettings.xml",
        _FLUSHED_GUISETTINGS_STANDARD.encode(),
    )
    base_vectors = list(rig.vectors)
    assert rig.profile.flush_deferred_guisettings_nodes(log=lambda m: None)
    assert not rig.profile.deferred_nodes_pending(), "marker must be consumed"
    final = bytes(rig.store.vfs_read("special://profile/guisettings.xml"))
    root = ET.fromstring(final)
    assert root.find("general/settinglevel").text == "3"
    assert root.find("setting[@id='lookandfeel.skin']") is not None, (
        "the patch lost a pre-existing setting"
    )
    assert rig.store.state("guisettings.xml") == "key-only", (
        "both tvOS layers must agree (vector confirmed, POSIX dropped)"
    )
    assert rig.vectors == base_vectors + ["guisettings.xml"], (
        "the landing takes exactly ONE vector"
    )


def test_settinglevel_flush_already_correct_touches_nothing(
    monkeypatch, tmp_path
):
    """The user set expert by hand mid-session: the flushed file already
    carries 3, so the shutdown window consumes the marker WITHOUT writing -
    a rewrite would be a key rewrite plus a POSIX drop on a run that changed
    nothing (the 4.3 argument, again)."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    assert rig.profile.mark_deferred_nodes_pending(
        [("general/settinglevel", "3")]
    )
    rig.store.vfs_write(
        "special://profile/guisettings.xml",
        b'<settings version="2"><general><settinglevel>3</settinglevel>'
        b"</general></settings>",
    )
    base_vectors = list(rig.vectors)
    assert rig.profile.flush_deferred_guisettings_nodes(log=lambda m: None)
    assert not rig.profile.deferred_nodes_pending()
    assert rig.vectors == base_vectors, "a no-op landing must take no vector"


def test_settinglevel_flush_unreadable_keeps_the_marker(monkeypatch, tmp_path):
    """An unreadable guisettings at the flush window is a KEPT marker and a
    False, never a guessed write - the next clean shutdown retries."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    assert rig.profile.mark_deferred_nodes_pending(
        [("general/settinglevel", "3")]
    )
    rig.store.vfs_delete("special://profile/guisettings.xml")
    import os as _os

    posix = rig.store.translate("special://profile/guisettings.xml")
    if _os.path.exists(posix):
        _os.remove(posix)
    assert not rig.profile.flush_deferred_guisettings_nodes(log=lambda m: None)
    assert rig.profile.deferred_nodes_pending(), "the write is still owed"


def test_rssfeeds_two_layer_write_and_byte_idempotence(monkeypatch, tmp_path):
    """The curated list lands on both tvOS layers (one vector, POSIX dropped,
    key holds the exact bundle bytes) and a byte-equal re-run touches no
    storage layer at all."""
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    raw = (HOUSE / "RssFeeds.xml").read_bytes()
    ops = [{"kind": "rss-feeds", "xml": raw}]
    record = rig.profile.apply(ops)
    assert record["items"][0]["outcome"] == "applied"
    assert bytes(rig.store.vfs_read("special://profile/RssFeeds.xml")) == raw
    assert rig.store.state("RssFeeds.xml") == "key-only"
    assert rig.vectors.count("RssFeeds.xml") == 1
    assert record["warnings"] == [], "the vector must be CONFIRMED on tvOS"
    record = rig.profile.apply(ops)
    assert record["items"][0]["outcome"] == "already-correct"
    assert rig.vectors.count("RssFeeds.xml") == 1, (
        "a byte-equal re-run mutated the storage layer"
    )


def test_rssfeeds_current_md5_reads_the_vfs_layer(monkeypatch, tmp_path):
    """The boot check's md5 comes from the VFS read (on tvOS the key - the
    layer Kodi actually reads), so its verdict is about the bytes that feed
    the ticker."""
    import hashlib

    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    assert rig.profile.rssfeeds_current_md5() == ""
    raw = (HOUSE / "RssFeeds.xml").read_bytes()
    rig.profile.apply([{"kind": "rss-feeds", "xml": raw}])
    assert rig.profile.rssfeeds_current_md5() == hashlib.md5(raw).hexdigest()
    assert rig.profile.rssfeeds_current_md5() == HOUSE_RSS_MD5
