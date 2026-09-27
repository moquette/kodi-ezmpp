# -*- coding: utf-8 -*-
"""The share host constant, the host migration, the PVR share step and the
Tailscale nudge (resources/lib/modules/sharehost.py, pvrshare.py, tailnet.py,
the profile's migration paths and service.py's wiring).

Owner decision 2026-09-26: every box reaches the mini over Tailscale. The
address lives in ONE constant, every bundle artifact is rendered from it, a
box still carrying the LAN address is migrated (profile run or boot), and
pvr.iptvsimple's instance files follow the share's templates automatically.

Rig: the two-layer storage fake (tests/fake_kodi_storage.py) for the box, a
dict for the share reachable over nfs://, and a small JSON-RPC fake for the
add-on enable/disable dance. The REAL sharehost, tailnet, pvrshare, nsud and
profile modules run against it.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from fake_kodi_storage import FakeKodiStorage, make_modules
from test_service_pov_reuse_invoker import _Env, _load_service, _nsud_stub

HERE = Path(__file__).parent
ADDON_ROOT = HERE.parent / "script.ezmaintenanceplusplus"
HOUSE = ADDON_ROOT / "resources" / "profiles" / "house"

NEW = "100.121.59.123"
OLD = "192.168.7.2"
IPTV = "nfs://%s/Users/moquette/Kodi/Share/iptv/" % NEW
SHARE_ROOT = "nfs://%s/Users/moquette/Kodi/" % NEW

TEMPLATE_OLD = (
    '<settings version="2">\n'
    '    <setting id="kodi_addon_instance_name">Network 24</setting>\n'
    '    <setting id="m3uPath">nfs://%s/Users/moquette/Kodi/Share/iptv/Network24.m3u</setting>\n'
    '    <setting id="epgPath">nfs://%s/Users/moquette/Kodi/Share/iptv/Network24-epg.xml.gz</setting>\n'
    "</settings>\n"
) % (OLD, OLD)
TEMPLATE_NEW = TEMPLATE_OLD.replace(OLD, NEW)


# --------------------------------------------------------------------------- #
# The rig
# --------------------------------------------------------------------------- #
class _Rig:
    def __init__(self, store, platform):
        self.store = store
        self.platform = platform
        self.share = {}  # url -> bytes; a directory url -> None marks reachability
        self.share_up = True
        self.addons = {"pvr.iptvsimple": True}
        self.events = []
        self.playing = False
        self.logs = []
        self.android_starts = []
        self.start_result = True
        self.reconnect_after_start = False
        self.markers = []

    # -- xbmcvfs --
    def listdir(self, url):
        if not self.share_up:
            return [], []
        if url == SHARE_ROOT:
            return ["Share", "Backup"], []
        if url == IPTV:
            names = [u[len(IPTV):] for u in self.share if u.startswith(IPTV)]
            return [], sorted(names)
        return [], []

    # -- JSON-RPC --
    def rpc(self, raw):
        req = json.loads(raw)
        m, p = req["method"], req.get("params", {})
        if m == "Addons.GetAddonDetails":
            aid = p["addonid"]
            if aid not in self.addons:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps(
                {"result": {"addon": {"addonid": aid, "enabled": self.addons[aid]}}}
            )
        if m == "Addons.SetAddonEnabled":
            aid = p["addonid"]
            self.events.append(("enable" if p["enabled"] else "disable", aid))
            if aid in self.addons:
                self.addons[aid] = bool(p["enabled"])
            return json.dumps({"result": "OK"})
        return json.dumps({"result": None})

    def start_activity(self, package, *a, **k):
        self.android_starts.append(package)
        if self.reconnect_after_start:
            self.share_up = True
        return self.start_result


def _rig(monkeypatch, tmp_path, platform="android", own_settings=None):
    store = FakeKodiStorage(tmp_path / "kodi", platform="tvos" if platform == "tvos" else "android")
    store.log = []
    rig = _Rig(store, platform)
    xbmc_cls, vfs_cls = make_modules(store)

    real_file = vfs_cls.File

    class _File:
        """Serves nfs:// urls from the share dict, everything else from the
        two-layer store."""

        def __init__(self, path, mode="r"):
            self._share = path.startswith("nfs://")
            self._path = path
            if not self._share:
                self._inner = real_file(path, mode)

        def readBytes(self, n=None):
            if self._share:
                if not rig.share_up:
                    raise IOError("share down")
                return rig.share.get(self._path) or b""
            return self._inner.readBytes(n)

        def read(self):
            return bytes(self.readBytes())

        def write(self, data):
            return self._inner.write(data)

        def close(self):
            if not self._share:
                self._inner.close()

    xbmcvfs_mod = types.ModuleType("xbmcvfs")
    xbmcvfs_mod.File = _File
    xbmcvfs_mod.exists = vfs_cls.exists
    xbmcvfs_mod.delete = vfs_cls.delete
    xbmcvfs_mod.translatePath = vfs_cls.translatePath
    xbmcvfs_mod.listdir = rig.listdir

    xbmc_mod = types.ModuleType("xbmc")
    for attr in ("LOGDEBUG", "LOGINFO", "LOGWARNING", "LOGERROR"):
        setattr(xbmc_mod, attr, getattr(xbmc_cls, attr))
    xbmc_mod.log = lambda msg, level=1: rig.logs.append(msg)

    def cond(c):
        if c == "System.Platform.TVOS":
            return platform == "tvos"
        if c == "System.Platform.Android":
            return platform == "android"
        return False

    xbmc_mod.getCondVisibility = cond
    xbmc_mod.executeJSONRPC = rig.rpc
    xbmc_mod.executebuiltin = lambda *a, **k: None
    xbmc_mod.sleep = lambda ms: None
    xbmc_mod.getInfoLabel = lambda label: ""
    xbmc_mod.getLocalizedString = lambda sid: ""
    xbmc_mod.getSkinDir = lambda: ""
    xbmc_mod.Player = lambda: types.SimpleNamespace(
        isPlaying=lambda: rig.playing, isPlayingVideo=lambda: rig.playing
    )
    xbmc_mod.startAndroidActivity = rig.start_activity

    settings = own_settings if own_settings is not None else {}

    class _Addon:
        def __init__(self, *a, **k):
            pass

        def getSetting(self, key):
            return settings.get(key, "")

        def setSetting(self, key, value):
            settings[key] = value

        def getAddonInfo(self, key):
            return {"path": str(ADDON_ROOT)}.get(key, "")

    xbmcaddon_mod = types.ModuleType("xbmcaddon")
    xbmcaddon_mod.Addon = _Addon

    tools_stub = types.ModuleType("resources.lib.modules.tools")
    tools_stub.mark_pvr_paused = lambda: rig.markers.append("mark") or True
    tools_stub.clear_pvr_pause_marker = lambda: rig.markers.append("clear")

    for name in list(sys.modules):
        if name.startswith("resources"):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_mod)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs_mod)
    monkeypatch.setitem(sys.modules, "xbmcaddon", xbmcaddon_mod)
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    profile = importlib.import_module("resources.lib.modules.profile")
    monkeypatch.setitem(sys.modules, "resources.lib.modules.tools", tools_stub)
    rig.profile = profile
    rig.pvrshare = sys.modules["resources.lib.modules.pvrshare"]
    rig.sharehost = sys.modules["resources.lib.modules.sharehost"]
    rig.tailnet = sys.modules["resources.lib.modules.tailnet"]
    rig.tailnet.reset()
    rig.settings = settings
    return rig


def _box_copy(rig, name):
    raw = rig.store.vfs_read(
        "special://profile/addon_data/pvr.iptvsimple/%s" % name
    )
    return bytes(raw) if raw else b""


def _seed_box(rig, name, text):
    rig.store.seed_disk("addon_data/pvr.iptvsimple/%s" % name, text.encode())


# --------------------------------------------------------------------------- #
# 1. sharehost: the constant and the rewrite
# --------------------------------------------------------------------------- #
def test_sharehost_constant_and_urls(monkeypatch, tmp_path):
    sh = _rig(monkeypatch, tmp_path).sharehost
    assert sh.SHARE_HOST == NEW
    assert sh.SHARE_URL == "nfs://%s/Users/moquette/Kodi/Share/" % NEW
    assert sh.BACKUP_URL == "nfs://%s/Users/moquette/Kodi/Backup/" % NEW
    assert sh.IPTV_URL == IPTV
    assert sh.backup_url("fireos") == sh.BACKUP_URL + "fireos/"
    assert ":" not in sh.NFS_ROOT[len("nfs://"):].split("/", 1)[0]


def test_sharehost_migrate_rewrites_only_legacy_hosts_and_drops_the_port(
    monkeypatch, tmp_path
):
    sh = _rig(monkeypatch, tmp_path).sharehost
    assert sh.migrate("nfs://%s/Users/moquette/Kodi/Share/" % OLD) == (
        "nfs://%s/Users/moquette/Kodi/Share/" % NEW,
        1,
    )
    assert sh.migrate("nfs://%s:2049/Users/moquette/Kodi/Backup/tvos/" % OLD) == (
        "nfs://%s/Users/moquette/Kodi/Backup/tvos/" % NEW,
        1,
    )
    # not ours: another host, smb, the new host itself
    for keep in (
        "nfs://192.168.7.20/x/",
        "smb://%s/KodiShare/" % OLD,
        "nfs://%s/Users/moquette/Kodi/Share/" % NEW,
        "",
    ):
        assert sh.migrate(keep) == (keep, 0)
    text, n = sh.migrate(TEMPLATE_OLD)
    assert n == 2 and text == TEMPLATE_NEW
    assert sh.migrate(text) == (text, 0)  # idempotent
    assert sh.names_legacy_host(TEMPLATE_OLD) and not sh.names_legacy_host(TEMPLATE_NEW)


def test_house_bundle_is_rendered_from_the_constant_and_names_no_host_literally(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path)
    for cls in ("fireos", "tvos", "androidtv", "bench"):
        bundle = rig.profile.load(str(HOUSE), cls)
        paths = dict(bundle["sources"])
        assert paths["KodiShare"] == rig.sharehost.SHARE_URL
        assert paths["KodiBackup"] == rig.sharehost.BACKUP_URL
        own = dict(
            bundle["addon_data"]["script.ezmaintenanceplusplus"]["settings.xml"]["pairs"]
        )
        folder = "fireos" if cls == "bench" else cls
        assert own["download.path"] == rig.sharehost.backup_url(folder)
        assert own["restore.path"] == rig.sharehost.backup_url(folder)
    # No bundle file names an address: the token is the only host spelling.
    for path in HOUSE.rglob("*.xml"):
        text = path.read_text()
        assert OLD not in text and NEW not in text, path
        if "nfs://" in text:
            assert "nfs://@SHARE_HOST@/" in text, path


def test_load_rejects_a_bundle_that_names_the_legacy_host_literally(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path)
    from test_settings_profile import make_bundle

    b = make_bundle(
        tmp_path,
        sources_xml=(
            "<sources><files><source><name>KodiShare</name>"
            "<path pathversion=\"1\">nfs://%s/Users/moquette/Kodi/Share/</path>"
            "</source></files></sources>" % OLD
        ),
    )
    with pytest.raises(rig.profile.ProfileError) as e:
        rig.profile.load(str(b), "fireos")
    assert any("legacy share host" in p for p in e.value.problems), e.value.problems


# --------------------------------------------------------------------------- #
# 2. Host migration through the profile and at boot
# --------------------------------------------------------------------------- #
_SOURCES_OLD = (
    '<sources><video><default pathversion="1" /></video>'
    '<files><default pathversion="1" />'
    "<source><name>Mine</name><path pathversion=\"1\">/somewhere/mine/</path>"
    "<allowsharing>true</allowsharing></source>"
    "<source><name>KodiShare</name>"
    "<path pathversion=\"1\">nfs://%s/Users/moquette/Kodi/Share/</path>"
    "<allowsharing>true</allowsharing></source>"
    "<source><name>KodiBackup</name>"
    "<path pathversion=\"1\">nfs://%s/Users/moquette/Kodi/Backup/</path>"
    "<allowsharing>true</allowsharing></source>"
    "</files></sources>"
) % (OLD, OLD)


def _files_paths(raw):
    root = ET.fromstring(raw)
    return {
        (s.findtext("name") or ""): (s.findtext("path") or "")
        for s in root.find("files").findall("source")
    }


def test_profile_sources_step_migrates_legacy_entries_in_place_idempotently(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    rig.store.seed_disk("sources.xml", _SOURCES_OLD.encode())
    bundle = rig.profile.load(str(HOUSE), "tvos")
    op = [o for o in rig.profile.plan(bundle) if o["kind"] == "sources"][0]
    logs = []
    ctx = {"items": [], "warnings": [], "log": logs.append}
    outcome, detail = rig.profile._apply_sources(op, ctx)
    assert outcome == "applied", detail
    assert "2 moved to %s" % NEW in detail
    paths = _files_paths(bytes(rig.store.vfs_read("special://profile/sources.xml")))
    assert paths["KodiShare"] == rig.sharehost.SHARE_URL
    assert paths["KodiBackup"] == rig.sharehost.BACKUP_URL
    assert paths["Mine"] == "/somewhere/mine/"
    assert len(paths) == 4  # .T7B added, no duplicate KodiShare/KodiBackup
    moved = [m for m in logs if "share host migrated" in m]
    assert len(moved) == 2 and all(OLD in m and NEW in m for m in moved), logs
    # second run: nothing moves, nothing written
    before = rig.store.state("sources.xml")
    logs.clear()
    outcome, detail = rig.profile._apply_sources(op, ctx)
    assert outcome == "already-correct", detail
    assert not [m for m in logs if "migrated" in m]
    assert rig.store.state("sources.xml") == before


def test_profile_own_setting_step_reports_the_host_migration(monkeypatch, tmp_path):
    rig = _rig(
        monkeypatch,
        tmp_path,
        own_settings={
            "download.path": "nfs://%s/Users/moquette/Kodi/Backup/fireos/" % OLD,
            "restore.path": "nfs://%s/Users/moquette/Kodi/Backup/fireos/" % OLD,
        },
    )
    bundle = rig.profile.load(str(HOUSE), "fireos")
    ops = [o for o in rig.profile.plan(bundle) if o["kind"] == "own-setting"]
    ctx = {"items": [], "warnings": [], "log": lambda m: None}
    for op in ops:
        outcome, detail = rig.profile._apply_own_setting(op, ctx)
        assert outcome == "applied" and "migrated to %s" % NEW in detail, (op, detail)
    assert rig.settings["download.path"] == rig.sharehost.backup_url("fireos")
    for op in ops:
        assert rig.profile._apply_own_setting(op, ctx) == ("already-correct", "")


def test_boot_migration_moves_settings_and_sources_then_is_a_no_op(
    monkeypatch, tmp_path
):
    rig = _rig(
        monkeypatch,
        tmp_path,
        platform="tvos",
        own_settings={
            "download.path": "nfs://%s:2049/Users/moquette/Kodi/Backup/tvos/" % OLD,
            "restore.path": "nfs://%s/Users/moquette/Kodi/Backup/tvos/" % OLD,
        },
    )
    rig.store.seed_disk("sources.xml", _SOURCES_OLD.encode())
    logs = []
    out = rig.profile.ensure_share_host_migrated(log=logs.append)
    assert out["settings"] == 2 and out["sources"] == 2, out
    assert rig.settings["download.path"] == rig.sharehost.backup_url("tvos")
    assert rig.settings["restore.path"] == rig.sharehost.backup_url("tvos")
    paths = _files_paths(bytes(rig.store.vfs_read("special://profile/sources.xml")))
    assert paths["KodiShare"] == rig.sharehost.SHARE_URL
    assert paths["KodiBackup"] == rig.sharehost.BACKUP_URL
    assert paths["Mine"] == "/somewhere/mine/"
    assert len(paths) == 3  # migration adds nothing; that is the profile's job
    assert len([m for m in logs if "share host migrated" in m]) == 4, logs
    # tvOS: the rewrite went through nsud (one vector, POSIX dropped)
    assert rig.store.state("sources.xml") == "key-only"
    # second boot: nothing to do, nothing logged, no storage touched
    logs.clear()
    out = rig.profile.ensure_share_host_migrated(log=logs.append)
    assert out == {"settings": 0, "sources": 0, "detail": ""}
    assert logs == []


def test_boot_migration_leaves_a_box_on_the_new_host_alone(monkeypatch, tmp_path):
    rig = _rig(
        monkeypatch,
        tmp_path,
        own_settings={"download.path": "nfs://%s/x/" % NEW, "restore.path": ""},
    )
    rig.store.seed_disk("sources.xml", _SOURCES_OLD.replace(OLD, NEW).encode())
    logs = []
    out = rig.profile.ensure_share_host_migrated(log=logs.append)
    assert out == {"settings": 0, "sources": 0, "detail": ""}
    assert logs == []


# --------------------------------------------------------------------------- #
# 3. The PVR share step
# --------------------------------------------------------------------------- #
def test_pvr_share_installs_a_missing_instance_file_and_reloads_once(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    rig.share[IPTV + "instance-settings-2.xml"] = TEMPLATE_NEW.replace(
        "Network 24", "Streamvision"
    ).encode()
    rig.share[IPTV + "genres.xml"] = b"<genres/>"  # not an instance file
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "applied", res
    assert res["changed"] == ["instance-settings-1.xml", "instance-settings-2.xml"]
    assert _box_copy(rig, "instance-settings-1.xml") == TEMPLATE_NEW.encode()
    assert rig.events == [("disable", "pvr.iptvsimple"), ("enable", "pvr.iptvsimple")]
    assert res["reload"][0] == "applied"
    assert rig.markers == ["mark", "clear"]
    assert [m for m in rig.logs if "installed from the share" in m] == [
        "instance-settings-1.xml installed from the share",
        "instance-settings-2.xml installed from the share",
    ]
    tmp_left = list(
        Path(rig.store.translate("special://profile/addon_data/pvr.iptvsimple/")).glob("*.ezm-tmp")
    )
    assert tmp_left == []


def test_pvr_share_updates_a_differing_copy_and_migrates_a_legacy_template(
    monkeypatch, tmp_path
):
    """The share still carries the LAN address (the builder has not moved
    yet): the box gets the tailnet form anyway, and the log says so."""
    rig = _rig(monkeypatch, tmp_path)
    _seed_box(rig, "instance-settings-1.xml", TEMPLATE_OLD)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_OLD.encode()
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "applied", res
    assert _box_copy(rig, "instance-settings-1.xml") == TEMPLATE_NEW.encode()
    assert any(
        "instance-settings-1.xml updated from the share (2 legacy host url(s) "
        "migrated to %s)" % NEW == m
        for m in rig.logs
    ), rig.logs
    assert len(rig.events) == 2
    # once the builder moves the template, the compare is a no-op
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    rig.events.clear()
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "already-correct", res
    assert rig.events == []


def test_pvr_share_is_a_no_op_when_the_box_matches(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    _seed_box(rig, "instance-settings-1.xml", TEMPLATE_NEW)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    before = rig.store.state("addon_data/pvr.iptvsimple/instance-settings-1.xml")
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "already-correct" and res["changed"] == [], res
    assert res["reload"] is None and rig.events == [] and rig.markers == []
    assert rig.store.state("addon_data/pvr.iptvsimple/instance-settings-1.xml") == before


def test_pvr_share_skips_when_the_share_is_unreachable_and_touches_nothing(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    _seed_box(rig, "instance-settings-1.xml", TEMPLATE_OLD)
    rig.share_up = False
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "skipped", res
    assert "unreachable" in res["detail"]
    assert _box_copy(rig, "instance-settings-1.xml") == TEMPLATE_OLD.encode()
    assert rig.events == []
    # tvOS: the nudge is not attempted, the tailnet-down line is logged
    assert any("tailnet down" in m for m in rig.logs), rig.logs
    assert rig.android_starts == []


def test_pvr_share_never_writes_an_unparsable_template(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    _seed_box(rig, "instance-settings-1.xml", TEMPLATE_OLD)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW[:-30].encode()
    rig.share[IPTV + "instance-settings-2.xml"] = b"<notsettings/>"
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "skipped", res
    assert "does not parse as XML" in res["detail"]
    assert "not <settings>" in res["detail"]
    assert _box_copy(rig, "instance-settings-1.xml") == TEMPLATE_OLD.encode()
    assert _box_copy(rig, "instance-settings-2.xml") == b""
    assert rig.events == []


def test_pvr_share_defers_during_playback(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    rig.playing = True
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "skipped" and res["detail"].startswith("deferred"), res
    assert _box_copy(rig, "instance-settings-1.xml") == b""
    # the explicit argument wins over the live probe
    res = rig.pvrshare.sync(log=rig.logs.append, playing=False)
    assert res["outcome"] == "applied"


def test_pvr_share_never_enables_a_client_the_box_had_off(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.addons["pvr.iptvsimple"] = False
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "applied", res
    assert _box_copy(rig, "instance-settings-1.xml") == TEMPLATE_NEW.encode()
    assert res["reload"][0] == "skipped" and "disabled" in res["reload"][1]
    assert rig.events == [] and rig.markers == []


def test_pvr_share_reload_failure_keeps_the_pause_marker_for_boot_recovery(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path)
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    real = rig.rpc

    def stuck(raw):
        out = real(raw)
        if json.loads(raw)["method"] == "Addons.SetAddonEnabled" and json.loads(raw)["params"]["enabled"]:
            rig.addons["pvr.iptvsimple"] = False  # the enable never lands
        return out

    monkeypatch.setattr(rig.pvrshare, "_RELOAD_POLL_S", 0.01)
    monkeypatch.setattr(sys.modules["xbmc"], "executeJSONRPC", stuck)
    res = rig.pvrshare.sync(log=rig.logs.append)
    assert res["outcome"] == "error", res
    assert "re-enabled at next boot" in res["detail"]
    assert rig.markers == ["mark"]  # never cleared: service heals it at boot


def test_pvr_share_runs_as_a_profile_step_after_the_sources(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    bundle = rig.profile.load(str(HOUSE), "fireos")
    kinds = [o["kind"] for o in rig.profile.plan(bundle)]
    assert "pvr-share" in kinds
    assert kinds.index("pvr-share") > kinds.index("sources")
    assert kinds[-1] == "enable"  # the repository still enables LAST
    assert rig.profile._op_label({"kind": "pvr-share"}) == "PVR share settings"
    assert rig.profile.SKIPPED in rig.profile._OK_OUTCOMES
    rig.share[IPTV + "instance-settings-1.xml"] = TEMPLATE_NEW.encode()
    ctx = {"items": [], "warnings": [], "log": rig.logs.append}
    assert rig.profile._apply_pvr_share({"kind": "pvr-share"}, ctx)[0] == "applied"
    assert rig.profile._apply_pvr_share({"kind": "pvr-share"}, ctx)[0] == "already-correct"
    rig.share_up = False
    assert rig.profile._apply_pvr_share({"kind": "pvr-share"}, ctx)[0] == "skipped"


# --------------------------------------------------------------------------- #
# 4. The Tailscale nudge
# --------------------------------------------------------------------------- #
def test_tailnet_android_starts_tailscale_once_and_retests(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.share_up = False
    rig.reconnect_after_start = True
    ok, detail = rig.tailnet.ensure_share_reachable(rig.logs.append, sleep=lambda s: None)
    assert ok is True, detail
    assert rig.android_starts == ["com.tailscale.ipn"]
    assert "started Tailscale" in detail and "answered after" in detail
    assert [m for m in rig.logs if "Tailscale" in m] == [detail]


def test_tailnet_android_bounded_wait_then_gives_up_and_never_loops(
    monkeypatch, tmp_path
):
    rig = _rig(monkeypatch, tmp_path)
    rig.share_up = False
    slept = []
    ok, detail = rig.tailnet.ensure_share_reachable(
        rig.logs.append, wait_s=6, sleep=slept.append
    )
    assert ok is False and "still did not answer after 6 s" in detail
    assert sum(slept) == 6
    assert rig.android_starts == ["com.tailscale.ipn"]
    # a second call in the same process does NOT start it again
    ok, detail = rig.tailnet.ensure_share_reachable(rig.logs.append, sleep=slept.append)
    assert ok is False and "already started once" in detail
    assert rig.android_starts == ["com.tailscale.ipn"]


def test_tailnet_android_start_failure_is_one_honest_line(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    rig.share_up = False
    rig.start_result = False
    ok, detail = rig.tailnet.ensure_share_reachable(rig.logs.append, sleep=lambda s: None)
    assert ok is False and "could not be started" in detail
    assert rig.logs.count(detail) == 1


def test_tailnet_tvos_logs_down_and_starts_nothing(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path, platform="tvos")
    rig.share_up = False
    ok, detail = rig.tailnet.ensure_share_reachable(rig.logs.append)
    assert ok is False and "tailnet down" in detail and "tvOS" in detail
    assert rig.android_starts == []


def test_tailnet_reachable_share_is_silent(monkeypatch, tmp_path):
    rig = _rig(monkeypatch, tmp_path)
    ok, detail = rig.tailnet.ensure_share_reachable(rig.logs.append)
    assert ok is True and rig.logs == [] and rig.android_starts == []


# --------------------------------------------------------------------------- #
# 5. service.py wiring
# --------------------------------------------------------------------------- #
def _profile_stub(migration):
    m = types.ModuleType("resources.lib.modules.profile")
    m.APPLIED, m.ALREADY, m.ERROR = "applied", "already-correct", "error"
    m.ensure_share_host_migrated = lambda log=None: migration
    m.ensure_pov_reuse_invoker_disabled = lambda log=None: {
        "settings": "already-correct", "addon_xml": "already-correct", "detail": ""
    }
    m.ensure_autocompletion_hidden = lambda log=None: ("already-correct", "")
    m.ensure_pov_resume_seek_applied = lambda log=None: ("already-correct", "")
    return m


def _pvrshare_stub(results):
    m = types.ModuleType("resources.lib.modules.pvrshare")
    m.SKIPPED, m.ERROR, m.APPLIED, m.ALREADY = "skipped", "error", "applied", "already-correct"
    m.calls = []

    def sync(log=None, playing=None, reload=True):
        m.calls.append(playing)
        return results.pop(0)

    m.sync = sync
    return m


def _inject(monkeypatch, name, mod):
    monkeypatch.setitem(sys.modules, "resources.lib.modules." + name, mod)
    setattr(sys.modules["resources.lib.modules"], name, mod)


def test_service_startup_sequence_migrates_the_share_host(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    _inject(monkeypatch, "profile", _profile_stub(
        {"settings": 2, "sources": 2, "detail": "2 setting(s), 2 source(s) moved to %s" % NEW}
    ))
    monkeypatch.setattr(mod, "_maybe_purge_stale_nsud_keys", lambda: None)
    monkeypatch.setattr(mod, "_purge_stale_bytecode", lambda: None)
    monkeypatch.setattr(mod, "_maybe_resume_paused_pvr", lambda: None)
    monkeypatch.setattr(mod, "_maybe_restore_check", lambda m: None)
    monkeypatch.setattr(mod, "_maybe_profile_check", lambda m: None)
    mod._startup_sequence(types.SimpleNamespace(abortRequested=lambda: False))
    assert any("share host migrated" in m and NEW in m for _l, m in env.logs), env.logs


def test_service_startup_sequence_is_silent_when_nothing_migrates(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    _inject(monkeypatch, "profile", _profile_stub({"settings": 0, "sources": 0, "detail": ""}))
    mod._maybe_migrate_share_host()
    assert not [m for _l, m in env.logs if "share host" in m], env.logs


def test_service_pvr_share_step_is_owed_again_only_when_deferred(monkeypatch, tmp_path):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    stub = _pvrshare_stub([
        {"outcome": "skipped", "detail": "deferred: something is playing", "changed": [], "reload": None},
        {"outcome": "skipped", "detail": "share unreachable or carries no instance files; skipped", "changed": [], "reload": None},
        {"outcome": "error", "detail": "instance-settings-1.xml: write failed", "changed": [], "reload": None},
    ])
    _inject(monkeypatch, "pvrshare", stub)
    assert mod._maybe_sync_pvr_share() is False  # deferred: retry later
    assert mod._maybe_sync_pvr_share(playing=False) is True  # unreachable: done
    assert mod._maybe_sync_pvr_share(playing=False) is True  # error: done, warned
    assert stub.calls == [None, False, False]
    assert any("write failed" in m and level == 2 for level, m in env.logs), env.logs


def test_service_loop_retries_an_owed_pvr_share_step_on_the_next_idle_tick(
    monkeypatch, tmp_path
):
    env = _Env(tmp_path)
    mod = _load_service(monkeypatch, env, _nsud_stub())
    stub = _pvrshare_stub([
        {"outcome": "applied", "detail": "instance-settings-1.xml written", "changed": ["instance-settings-1.xml"], "reload": ("applied", "reloaded")},
    ])
    _inject(monkeypatch, "pvrshare", stub)
    mod._PVR_SHARE_OWED["owed"] = True
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
    assert mod._PVR_SHARE_OWED["owed"] is False
