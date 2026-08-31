"""Coverage for script.ezmaintenanceplusplus's nsud.py (Apple TV restore durability).

nsud re-writes restored userdata *.xml THROUGH xbmcvfs so tvOS vectors them into
NSUserDefaults. The load-bearing correctness rule is the SINGLE write per file: Kodi's
tvOS CTVOSFile::Write REPLACES the whole NSUserDefaults key on every call, so a chunked
write would leave only the last chunk (a truncated XML fragment). The fake xbmcvfs.File
below models that replace-per-write semantics, so a regression to chunking fails these
tests exactly the way a real Apple TV would corrupt the settings.

nsud imports only os/json (real) + xbmc/xbmcvfs (faked here), so it is exercised as the
real module in isolation, no heavy add-on import chain needed.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest

ADDON_MODULES = (
    Path(__file__).parent.parent
    / "script.ezmaintenanceplusplus"
    / "resources"
    / "lib"
    / "modules"
)


class _FakeFile:
    """Models tvOS CTVOSFile: each write() REPLACES the whole stored value for the path,
    and read() serves that stored value back (as NSUserDefaults does)."""

    def __init__(self, store, writes, state, path, mode):
        self._store = store
        self._writes = writes
        self._state = state
        self._path = path
        self._mode = mode

    def write(self, data):
        self._writes.append((self._path, bytes(data)))  # record every write call
        if self._state.get("fail_writes") or self._path in self._state.get(
            "fail_paths", set()
        ):
            return False
        self._store[self._path] = bytes(
            data
        )  # REPLACE (not append) - the tvOS semantics
        return True

    def readBytes(self):
        # Read-back path. `evict_on_readback` models the tvOS store silently dropping a key
        # despite write()==True (the ~500 KB budget) - read returns empty though write said OK.
        if self._path in self._state.get("evict_on_readback", set()):
            return bytearray(b"")
        return bytearray(self._store.get(self._path, b""))

    def close(self):
        pass


@pytest.fixture
def nsud(monkeypatch, tmp_path):
    """Import the real nsud.py with faked xbmc/xbmcvfs; expose recorders.

    `tmp_path` doubles as the userdata root: the fake `translatePath` maps
    special://home/userdata/<rel> onto it, so a test that writes <tmp_path>/sources.xml
    and calls persist_one("sources.xml") is talking about the same file. That mirrors
    the real box, where persist_one derives the POSIX path from the special:// path
    rather than being handed a directory the way rewrite_userdata_xml is.
    """
    store: dict[str, bytes] = {}  # special path -> final bytes in "NSUserDefaults"
    writes: list[tuple[str, bytes]] = []  # every (path, bytes) write call
    events: list[str] = []  # ordered trace: enable:.. / sleep / write:<path>
    state = {"fail_writes": False}

    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 3
    xbmc.LOGERROR = 1
    xbmc.sleep = lambda ms: events.append("sleep")
    xbmc.log = lambda *a, **k: None

    def _execute_jsonrpc(s):
        import json

        req = json.loads(s)
        if req.get("method") == "Addons.SetAddonEnabled":
            events.append("enable:%s" % req["params"]["enabled"])
        return json.dumps({"result": "OK"})

    xbmc.executeJSONRPC = _execute_jsonrpc

    xbmcvfs = types.ModuleType("xbmcvfs")

    def _make_file(path, mode="r"):
        if "w" in mode:
            events.append(
                "write-open:%s" % path
            )  # only writes matter to ordering traces
        return _FakeFile(store, writes, state, path, mode)

    # File() records the OPEN in events on construction so ordering vs enable/sleep is
    # captured even though the write itself happens on .write().
    xbmcvfs.File = _make_file

    _USERDATA_PREFIX = "special://home/userdata/"

    def _translate_path(path):
        p = str(path)
        if p.startswith(_USERDATA_PREFIX):
            return str(tmp_path / p[len(_USERDATA_PREFIX) :])
        return p

    xbmcvfs.translatePath = _translate_path

    monkeypatch.setitem(sys.modules, "xbmc", xbmc)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs)
    monkeypatch.syspath_prepend(str(ADDON_MODULES))
    monkeypatch.delitem(sys.modules, "nsud", raising=False)
    mod = importlib.import_module("nsud")

    return types.SimpleNamespace(
        mod=mod, store=store, writes=writes, events=events, state=state
    )


def _write(base: Path, rel: str, content: bytes = b"<x/>") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


# --------------------------------------------------------------------------- #
# The core invariant: ONE write per file, full content (the anti-chunking guard).
# --------------------------------------------------------------------------- #
def test_single_write_per_file_full_content(nsud, tmp_path):
    big = b"<settings>" + b"x" * 100_000 + b"</settings>"
    _write(tmp_path, "guisettings.xml", big)

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    special = "special://home/userdata/guisettings.xml"
    per_file = [w for w in nsud.writes if w[0] == special]
    assert len(per_file) == 1, "must be exactly ONE write() per file - never chunk"
    assert nsud.store[special] == big, "the whole file must land, not a tail fragment"


# --------------------------------------------------------------------------- #
# Exclusions: the add-on's own settings (secret + boot-crash) only. IPTV is NOT special-cased.
# --------------------------------------------------------------------------- #
def test_excludes_own_settings_secret(nsud, tmp_path):
    _write(
        tmp_path,
        "addon_data/script.ezmaintenanceplusplus/settings.xml",
        b'<settings><setting id="dropbox_refresh_token">SECRET</setting></settings>',
    )
    _write(tmp_path, "guisettings.xml")

    written, skipped, failed = nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (
        "special://home/userdata/addon_data/script.ezmaintenanceplusplus/settings.xml"
        not in nsud.store
    )
    assert not any(b"SECRET" in v for v in nsud.store.values())
    assert skipped >= 1 and written >= 1


def test_general_walk_does_not_special_case_pvr(nsud, tmp_path):
    # IPTV handling is gone: the generic durability re-write treats a pvr.iptvsimple xml
    # like any other userdata xml (same-bytes rewrite so a restore sticks on tvOS). It does
    # NOT enable, disable, stage, or otherwise MANAGE the IPTV client - it only rewrites a
    # file that is already on disk. No special-casing either way.
    _write(tmp_path, "addon_data/pvr.iptvsimple/instance-settings-1.xml")
    _write(tmp_path, "RssFeeds.xml")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert "special://home/userdata/RssFeeds.xml" in nsud.store
    assert (
        "special://home/userdata/addon_data/pvr.iptvsimple/instance-settings-1.xml"
        in nsud.store
    ), "no special-casing: a present pvr xml is rewritten like any other, never managed"


def test_generic_exclude_dir_prefixes_opt_out(nsud, tmp_path):
    # The generic opt-out still works for a caller that passes it, but its DEFAULT is empty
    # and it names no add-on (no pvr special-casing baked in).
    _write(tmp_path, "addon_data/foo/settings.xml")
    _write(tmp_path, "RssFeeds.xml")

    nsud.mod.rewrite_userdata_xml(
        str(tmp_path), exclude_dir_prefixes=("addon_data/foo/",)
    )

    assert "special://home/userdata/RssFeeds.xml" in nsud.store
    assert not any("addon_data/foo" in p for p in nsud.store)


def test_no_iptv_or_pvr_management_api(nsud):
    # By construction: the IPTV/pvr enable-disable-stage-probe machinery is GONE from nsud.
    for gone in (
        "stage_iptv_disabled",
        "set_pvr_enabled",
        "pvr_is_enabled",
        "iptv_probe_targets",
        "iptv_share_reachable",
        "_set_pvr_enabled",
    ):
        assert not hasattr(nsud.mod, gone), "nsud must not expose %s" % gone


def test_non_xml_files_skipped(nsud, tmp_path):
    _write(tmp_path, "Database/MyVideos.db", b"sqlite")
    _write(tmp_path, "Thumbnails/a.jpg", b"jpeg")
    _write(tmp_path, "keyboard.xml")

    written, _skipped, _failed = nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert list(nsud.store) == ["special://home/userdata/keyboard.xml"]
    assert written == 1


def test_write_failure_leaves_source_and_counts_failed(nsud, tmp_path):
    _write(tmp_path, "guisettings.xml", b"<settings/>")
    nsud.state["fail_writes"] = True

    written, _skipped, failed = nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert written == 0 and failed == 1
    # POSIX source is untouched (no data loss - worst case is the pre-existing shadow).
    assert (tmp_path / "guisettings.xml").read_bytes() == b"<settings/>"


# --------------------------------------------------------------------------- #
# tvOS duplicate-entry fix: after a CONFIRMED vector into NSUserDefaults, and ONLY on tvOS,
# drop the redundant POSIX copy so File Manager stops listing every userdata file twice.
# The gate is a hard safety boundary - on any other platform special://home/userdata IS the
# POSIX file, so dropping it would delete what was just written.
# --------------------------------------------------------------------------- #
def _enable_tvos(monkeypatch):
    """Make the faked xbmc report Apple TV, as nsud._is_tvos() checks."""
    monkeypatch.setattr(
        sys.modules["xbmc"],
        "getCondVisibility",
        lambda cond: "TVOS" in cond,
        raising=False,
    )


def test_tvos_drops_posix_after_confirmed_vector(nsud, tmp_path, monkeypatch):
    _enable_tvos(monkeypatch)
    _write(tmp_path, "guisettings.xml", b"<settings/>")
    _write(tmp_path, "RssFeeds.xml", b"<rss/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    # Vectored into NSUserDefaults (the durable store)...
    assert nsud.store["special://home/userdata/guisettings.xml"] == b"<settings/>"
    # ...and the now-redundant POSIX copies are gone (no duplicate File Manager entry).
    assert not (tmp_path / "guisettings.xml").exists()
    assert not (tmp_path / "RssFeeds.xml").exists()


def test_non_tvos_never_drops_posix(nsud, tmp_path):
    # The default fake xbmc has no getCondVisibility -> _is_tvos() False -> POSIX kept.
    # This is the CATASTROPHE guard: on Fire TV/desktop the special:// path IS the disk file.
    _write(tmp_path, "guisettings.xml", b"<settings/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (tmp_path / "guisettings.xml").read_bytes() == b"<settings/>"


def test_tvos_keeps_posix_when_vector_fails(nsud, tmp_path, monkeypatch):
    # Ordered write-then-delete: never drop a file whose bytes are not confirmed in the store.
    _enable_tvos(monkeypatch)
    nsud.state["fail_writes"] = True
    _write(tmp_path, "guisettings.xml", b"<settings/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (tmp_path / "guisettings.xml").read_bytes() == b"<settings/>"


def test_tvos_does_not_drop_excluded_file(nsud, tmp_path, monkeypatch):
    # An excluded file is never vectored, so it must never be dropped either.
    _enable_tvos(monkeypatch)
    own = "addon_data/script.ezmaintenanceplusplus/settings.xml"
    _write(tmp_path, own, b"<settings/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (tmp_path / own).exists()


def test_drop_posix_flag_disables_tvos_drop(nsud, tmp_path, monkeypatch):
    _enable_tvos(monkeypatch)
    _write(tmp_path, "guisettings.xml", b"<settings/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path), drop_posix_on_tvos=False)

    assert (tmp_path / "guisettings.xml").exists()


def test_non_tvos_getcondvis_false_never_drops(nsud, tmp_path, monkeypatch):
    # Real Fire TV / Android / desktop: the condition EXISTS and returns False (NOT an
    # exception). This exercises the actual production guard on the dangerous platforms,
    # where special://home/userdata IS the POSIX file and a drop would destroy userdata.
    monkeypatch.setattr(
        sys.modules["xbmc"], "getCondVisibility", lambda cond: False, raising=False
    )
    _write(tmp_path, "guisettings.xml", b"<settings/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (tmp_path / "guisettings.xml").read_bytes() == b"<settings/>"


def test_is_tvos_queries_exact_condition_string(nsud, monkeypatch):
    # Pin the condition string so a typo (which real Kodi would answer False = fix silently
    # no-ops) is caught here instead of shipping.
    seen = []
    monkeypatch.setattr(
        sys.modules["xbmc"],
        "getCondVisibility",
        lambda cond: seen.append(cond) or True,
        raising=False,
    )
    assert nsud.mod._is_tvos() is True
    assert seen == ["System.Platform.TVOS"]


def test_tvos_keeps_posix_when_readback_mismatch(nsud, tmp_path, monkeypatch):
    # write() reports success, but the durable store does NOT hold the bytes on read-back
    # (models the tvOS storage budget silently evicting/truncating a key). The POSIX copy
    # must be kept - deleting it would lose the only good copy.
    _enable_tvos(monkeypatch)
    _write(tmp_path, "guisettings.xml", b"<settings/>")
    nsud.state["evict_on_readback"] = {"special://home/userdata/guisettings.xml"}

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert (tmp_path / "guisettings.xml").read_bytes() == b"<settings/>"


def test_mixed_success_and_failure_drops_only_confirmed(nsud, tmp_path, monkeypatch):
    # In one call: one file vectors cleanly (drop), another's write fails (keep).
    _enable_tvos(monkeypatch)
    _write(tmp_path, "guisettings.xml", b"<a/>")
    _write(tmp_path, "sources.xml", b"<b/>")
    nsud.state["fail_paths"] = {"special://home/userdata/sources.xml"}

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert not (tmp_path / "guisettings.xml").exists()  # confirmed vector -> dropped
    assert (tmp_path / "sources.xml").read_bytes() == b"<b/>"  # failed write -> kept


# --------------------------------------------------------------------------- #
# persist_one: the SINGLE-FILE tvOS-safe persist primitive.
#
# Every caller of this primitive (boxsetup._add_sources, boxsetup's weather settings write,
# wiz's boot-skin guisettings write) reaches it AFTER a plain POSIX write, so it is the last
# thing standing between a dual-layered file and a coherent one. It was stubbed at every
# reference and had zero coverage of its body; this block mirrors the drop/keep/skip matrix
# already pinned above for rewrite_userdata_xml.
# --------------------------------------------------------------------------- #
def test_persist_one_vectors_and_reports_true(nsud, tmp_path):
    # Off tvOS: the bytes go through the VFS (a no-op rewrite there) and the POSIX file - the
    # SAME file as the special:// path on Fire TV/desktop - must survive untouched.
    _write(tmp_path, "sources.xml", b"<sources/>")

    assert nsud.mod.persist_one("sources.xml") is True
    assert nsud.store["special://home/userdata/sources.xml"] == b"<sources/>"
    assert (tmp_path / "sources.xml").read_bytes() == b"<sources/>"


def test_persist_one_single_write_never_chunks(nsud, tmp_path):
    # The anti-chunking invariant applies to the single-file path too: CTVOSFile::Write
    # REPLACES the whole key, so two writes would leave only the tail fragment.
    big = b"<sources>" + b"x" * 100_000 + b"</sources>"
    _write(tmp_path, "sources.xml", big)

    nsud.mod.persist_one("sources.xml")

    special = "special://home/userdata/sources.xml"
    assert len([w for w in nsud.writes if w[0] == special]) == 1
    assert nsud.store[special] == big


def test_persist_one_tvos_drops_posix_after_confirmed_vector(
    nsud, tmp_path, monkeypatch
):
    _enable_tvos(monkeypatch)
    _write(tmp_path, "sources.xml", b"<sources/>")

    assert nsud.mod.persist_one("sources.xml") is True
    assert nsud.store["special://home/userdata/sources.xml"] == b"<sources/>"
    assert not (tmp_path / "sources.xml").exists(), (
        "a confirmed vector must drop the redundant POSIX copy on tvOS"
    )


def test_persist_one_private_addon_data_left_on_disk(nsud, tmp_path):
    # addon_data/<id>/<not settings.xml> is an add-on's PRIVATE data, read with plain open().
    # persist_one must NOT vector it (a key would shadow it and the drop would orphan it) and
    # must report success - the file is exactly where its owner expects it.
    rel = "addon_data/script.skinshortcuts/script.skinshortcuts.mainmenu.DATA.xml"
    _write(tmp_path, rel, b"<menu/>")

    assert nsud.mod.persist_one(rel) is True
    assert nsud.store == {}, "private add-on data must never be vectored"
    assert (tmp_path / rel).read_bytes() == b"<menu/>"


def test_persist_one_private_addon_data_kept_on_tvos_too(nsud, tmp_path, monkeypatch):
    # The dangerous direction: on tvOS the early return must fire BEFORE any drop logic, or
    # this is the skinshortcuts main-menu wipe again.
    _enable_tvos(monkeypatch)
    rel = "addon_data/script.skinshortcuts/script.skinshortcuts.mainmenu.DATA.xml"
    _write(tmp_path, rel, b"<menu/>")

    assert nsud.mod.persist_one(rel) is True
    assert (tmp_path / rel).read_bytes() == b"<menu/>"


def test_persist_one_siri_remote_keymap_never_vectored(nsud, tmp_path, monkeypatch):
    # Kodi's own WantsFile() excludes customcontroller.SiriRemote*, so it is served by
    # CPosixFile: a read-back would trivially "confirm" without a byte reaching
    # NSUserDefaults, and a drop would delete the only copy.
    _enable_tvos(monkeypatch)
    rel = "keymaps/customcontroller.SiriRemote.xml"
    _write(tmp_path, rel, b"<keymap/>")

    assert nsud.mod.persist_one(rel) is True
    assert nsud.store == {}
    assert (tmp_path / rel).read_bytes() == b"<keymap/>"


def test_persist_one_keymaps_left_on_disk_never_vectored(nsud, tmp_path, monkeypatch):
    # keymaps/ is POSIX-only by policy (2026-08-30, measured on atv1): Apple TV
    # Fixes writes keymaps with plain open(), so a key would shadow its every
    # future write. persist_one must leave the file alone and report success.
    _enable_tvos(monkeypatch)
    rel = "keymaps/t7b-siriremote.xml"
    _write(tmp_path, rel, b"<keymap/>")

    assert nsud.mod.persist_one(rel) is True
    assert nsud.store == {}, "a keymap must never be vectored"
    assert (tmp_path / rel).read_bytes() == b"<keymap/>"


# --------------------------------------------------------------------------- #
# The restore-extract API split (2026-08-30, measured on atv1): the extract lands
# every member as plain POSIX; the durability rewrite that follows must vector
# ONLY what _should_vector approves. keymaps/ is out of scope - the .30.2 rewrite
# vectored keymaps/t7b-siriremote.xml and dropped its POSIX copy, so the stale
# key shadowed every future plain-open() write Apple TV Fixes makes to that file
# (the playbook's POSIX-only contract, SKILL.md section 15).
# --------------------------------------------------------------------------- #
def test_should_vector_excludes_keymaps_master_and_per_profile(nsud):
    sv = nsud.mod._should_vector
    assert sv("keymaps/t7b-siriremote.xml") is False
    assert sv("keymaps/keyboard.xml") is False
    assert sv("profiles/Kids/keymaps/gen.xml") is False
    assert sv("keymaps\\t7b-siriremote.xml") is False  # backslash form normalizes
    # The in-scope neighbors stay in scope - the split, not a blanket retreat.
    assert sv("guisettings.xml") is True
    assert sv("sources.xml") is True
    assert sv("addon_data/pvr.iptvsimple/instance-settings-1.xml") is True


def test_rewrite_leaves_restored_keymap_posix_only_zero_key(
    nsud, tmp_path, monkeypatch
):
    # The restore sequence on tvOS, as wiz runs it: the extract wrote both files
    # plain-POSIX; the rewrite must vector guisettings (and drop its POSIX twin,
    # the load-bearing durability) while the keymap stays disk-only with ZERO key.
    _enable_tvos(monkeypatch)
    _write(tmp_path, "guisettings.xml", b"<settings/>")
    _write(tmp_path, "keymaps/t7b-siriremote.xml", b"<keymap/>")

    nsud.mod.rewrite_userdata_xml(str(tmp_path))

    assert nsud.store["special://home/userdata/guisettings.xml"] == b"<settings/>"
    assert not (tmp_path / "guisettings.xml").exists()
    assert "special://home/userdata/keymaps/t7b-siriremote.xml" not in nsud.store, (
        "the restore must never create a keymap key - it would shadow Apple TV "
        "Fixes' plain-open() keymap writes forever"
    )
    assert (tmp_path / "keymaps" / "t7b-siriremote.xml").read_bytes() == b"<keymap/>", (
        "the keymap's POSIX copy is the only copy and must stand untouched"
    )


def test_persist_one_returns_false_when_vector_fails(nsud, tmp_path):
    _write(tmp_path, "sources.xml", b"<sources/>")
    nsud.state["fail_writes"] = True

    assert nsud.mod.persist_one("sources.xml") is False
    assert (tmp_path / "sources.xml").read_bytes() == b"<sources/>"


def test_persist_one_returns_false_when_source_missing(nsud, tmp_path):
    # Nothing on disk to read -> nothing vectored -> must not claim success.
    assert nsud.mod.persist_one("sources.xml") is False
    assert nsud.store == {}


def test_persist_one_returns_false_when_readback_unconfirmed(
    nsud, tmp_path, monkeypatch
):
    """THE BUG this block was written for.

    write() reports success but the durable store does NOT hold the bytes on read-back
    (the tvOS ~500 KB budget silently evicting/truncating a key). persist_one correctly
    declines to os.remove the POSIX copy - no data loss - but it used to fall through to
    "persisted %s" and return True, so a caller could not tell a confirmed vector from a
    failed read-back. The docstring promised "True on a confirmed vector"; the code did not
    deliver it. That is the silent-incompleteness class this project has been burned by, and
    it is exactly what the _vfs_rewrite_once failure branch one step earlier gets right.
    """
    _enable_tvos(monkeypatch)
    _write(tmp_path, "sources.xml", b"<sources/>")
    nsud.state["evict_on_readback"] = {"special://home/userdata/sources.xml"}

    result = nsud.mod.persist_one("sources.xml")

    # The POSIX copy is kept either way - that part was never broken.
    assert (tmp_path / "sources.xml").read_bytes() == b"<sources/>"
    assert result is False, (
        "an unconfirmed read-back is NOT a confirmed vector; returning True hides a "
        "half-done persist from the caller"
    )


def test_persist_one_logs_truthfully_on_unconfirmed_readback(
    nsud, tmp_path, monkeypatch
):
    # The log is the only field diagnostic on Apple TV (no adb), so it must not say
    # "persisted" for a file that was not confirmed in the durable store.
    _enable_tvos(monkeypatch)
    _write(tmp_path, "sources.xml", b"<sources/>")
    nsud.state["evict_on_readback"] = {"special://home/userdata/sources.xml"}
    lines = []

    nsud.mod.persist_one("sources.xml", log=lines.append)

    assert lines, "the failure path must say something"
    assert not any("persisted" in ln for ln in lines), (
        "must not report a persist that was never confirmed: %r" % lines
    )


def test_persist_one_normalizes_backslashes(nsud, tmp_path):
    _write(tmp_path, "addon_data/weather.multi/settings.xml", b"<settings/>")

    assert nsud.mod.persist_one("addon_data\\weather.multi\\settings.xml") is True
    assert (
        nsud.store["special://home/userdata/addon_data/weather.multi/settings.xml"]
        == b"<settings/>"
    )


def test_persist_one_never_raises(nsud, tmp_path, monkeypatch):
    # Guarded by contract: every caller invokes it for effect mid-flow and a raise would
    # abort a restore/box setup.
    _enable_tvos(monkeypatch)
    _write(tmp_path, "sources.xml", b"<sources/>")
    monkeypatch.setattr(
        nsud.mod,
        "_vector_confirmed",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert nsud.mod.persist_one("sources.xml") is False


# --------------------------------------------------------------------------- #
# Wiring: the generic re-write runs AFTER apply_guisettings/UpdateLocalAddons and BEFORE the
# restart prompt. NO IPTV staging / auto-enable intent is wired anywhere (all removed).
# --------------------------------------------------------------------------- #
def test_wiz_calls_nsud_after_updatelocaladdons_before_restart():
    wiz_src = (ADDON_MODULES / "wiz.py").read_text(encoding="utf-8")
    i_apply = wiz_src.index("apply_guisettings(")
    i_update = wiz_src.index('executebuiltin("UpdateLocalAddons")')
    i_rewrite = wiz_src.index("nsud.rewrite_userdata_xml(")
    # The last userdata writes of the pass, in order: this box's preserved identity
    # settings, then the restored boot skin. Both must follow the tvOS re-vector, which
    # DROPS the POSIX copy of guisettings.xml - a write before it would be discarded.
    i_preserve = wiz_src.index("_preserve_device_settings(_rlog, preserved)")
    i_boot_skin = wiz_src.index("_apply_boot_skin(_rlog, _boot_skin.get(")
    i_marker = wiz_src.index("mark_restore_check_pending(")
    assert i_apply < i_update < i_rewrite < i_preserve < i_boot_skin < i_marker


def test_wiz_restore_has_no_iptv_or_delete_behavior():
    # By construction, the whole IPTV subsystem AND the boot-delete sweep are gone from wiz.
    wiz_src = (ADDON_MODULES / "wiz.py").read_text(encoding="utf-8")
    for gone in (
        "stage_iptv_disabled",
        "mark_iptv_autoenable_pending",
        "set_pvr_enabled",
        "pvr_is_enabled",
        "def sweep_home_root_pollution",
        "_USERDATA_STRAY_NAMES",
    ):
        assert gone not in wiz_src, "wiz.py must no longer contain %r" % gone
