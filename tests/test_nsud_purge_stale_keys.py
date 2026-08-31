"""Coverage for nsud.purge_stale_keys (the vector-everything-era stale-key cleanup).

EZM++ 2026.07.08.2 through 2026.07.13.x vectored EVERY userdata *.xml into
NSUserDefaults on Apple TV. `_should_vector` was later scoped down, but the stale keys
for the now-out-of-scope files still sit in NSUserDefaults and SHADOW the POSIX files
(CTVOSFile reads the key FIRST), so a restored or hand-copied file silently never
applies. purge_stale_keys drops exactly those keys, and never anything else.

The fakes model the real tvOS mechanics these tests exist to pin:
* the NSUserDefaults backing store is a REAL plist file on disk (built with plistlib,
  gzip values, plus the 'UserdataMigrated' bookkeeping key), resolved via
  special://home/../../Preferences exactly as on hardware;
* xbmcvfs.delete on a /userdata/*.xml special path drops ONLY the plist key and leaves
  the POSIX file alone, and returns True whether or not a key existed (the storage-map
  bug-4 lying boolean) - a 'stuck' key models a delete that silently did nothing;
* xbmcvfs.listdir merges the POSIX names and every LIVE key's basename WITHOUT dedupe
  (CTVOSDirectory::GetDirectory, TVOSDirectory.cpp:48-106) - the observable the purge's
  post-delete verification rides;
* the on-disk plist can LAG the live store (state["flush_lag"]): cfprefsd flushes the
  file lazily, so right after a delete the plist still shows the dropped key. That lag
  is the atv2 2026-07-17 defect: a plist re-read counted every successful drop as
  failed and the run-once marker never set.

nsud imports only os/gzip (real) + xbmc/xbmcvfs (faked here), so the real module is
exercised in isolation.
"""

from __future__ import annotations

import gzip
import importlib
import os
import plistlib
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

KEY_PREFIX = "/userdata/"
SPECIAL_PREFIX = "special://home/userdata/"


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A tvOS-shaped sandbox: special://home under Library/Caches/Kodi, a REAL plist in
    Library/Preferences, and a fake xbmcvfs whose delete() behaves like CTVOSFile::Delete
    (drops only the key; the lying True return). tvOS is OFF by default - each test that
    needs Apple TV calls env.enable_tvos()."""
    home = tmp_path / "Library" / "Caches" / "Kodi"
    userdata = home / "userdata"
    userdata.mkdir(parents=True)
    prefs = tmp_path / "Library" / "Preferences"
    prefs.mkdir(parents=True)
    plist_path = prefs / "org.xbmc.kodi-tvos.plist"

    deleted: list[str] = []  # every special path handed to xbmcvfs.delete
    state: dict = {
        "stuck_keys": set(),  # keys delete() silently fails to remove (lying True)
        "flush_lag": False,  # True = cfprefsd lag: delete drops LIVE, plist stays stale
        "live_dropped": set(),  # keys removed from the LIVE store by delete()
    }

    xbmc = types.ModuleType("xbmc")
    xbmc.log = lambda *a, **k: None

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda p: str(home) if p == "special://home" else p

    def _delete(special):
        # Model CTVOSFile::Delete for a WantsFile-eligible path: DeleteKeyFromPath
        # removes only the NSUserDefaults key, never the POSIX file, and the call
        # returns True whether or not a key existed (TVOSNSUserDefaults.mm:188-202).
        # The LIVE store drops the key immediately; the on-disk plist only follows
        # when cfprefsd flushes - with flush_lag it stays stale (the atv2 defect).
        deleted.append(special)
        assert special.startswith(SPECIAL_PREFIX), (
            "purge must only ever delete special://home/userdata/ paths, got %r"
            % special
        )
        key = KEY_PREFIX + special[len(SPECIAL_PREFIX) :]
        if key in state["stuck_keys"]:
            return True  # the lying boolean: reports success, removed nothing
        state["live_dropped"].add(key)
        if not state["flush_lag"] and plist_path.exists():
            data = plistlib.loads(plist_path.read_bytes())
            if key in data:
                del data[key]
                plist_path.write_bytes(plistlib.dumps(data))
        return True

    xbmcvfs.delete = _delete

    def _live_keys():
        # The LIVE NSUserDefaults state: the plist snapshot minus every key the
        # delete() above already dropped but cfprefsd has not flushed to disk yet.
        if not plist_path.exists():
            return set()
        keys = {
            k
            for k in plistlib.loads(plist_path.read_bytes())
            if isinstance(k, str) and k.startswith(KEY_PREFIX)
        }
        return keys - state["live_dropped"]

    def _listdir(special_dir):
        # CTVOSDirectory::GetDirectory (TVOSDirectory.cpp:48-106): the POSIX
        # entries first, then every LIVE key's basename appended WITHOUT dedupe -
        # the observable nsud's post-delete verification rides.
        # Source-accurate missing-dir behavior (QA finding F1, 2026-07-17): when
        # the POSIX dir does not exist, GetDirectory returns false BEFORE the key
        # append is ever reached, and xbmcvfs.listdir swallows that into ([], []).
        # The key layer is NOT observable through a missing dir - modelling it as
        # observable let a stuck-key state pass green that hardware would mask.
        assert special_dir.startswith("special://home/userdata")
        rel = special_dir[len("special://home/userdata") :].strip("/")
        real = userdata.joinpath(*rel.split("/")) if rel else userdata
        if not real.is_dir():
            return ([], [])
        entries = sorted(os.listdir(str(real)))
        files = [n for n in entries if (real / n).is_file()]
        dirs = [n for n in entries if (real / n).is_dir()]
        prefix = KEY_PREFIX + (rel + "/" if rel else "")
        key_names = sorted(
            k[len(prefix) :]
            for k in _live_keys()
            if k.startswith(prefix) and "/" not in k[len(prefix) :]
        )
        return (dirs, files + key_names)

    xbmcvfs.listdir = _listdir

    monkeypatch.setitem(sys.modules, "xbmc", xbmc)
    monkeypatch.setitem(sys.modules, "xbmcvfs", xbmcvfs)
    monkeypatch.syspath_prepend(str(ADDON_MODULES))
    monkeypatch.delitem(sys.modules, "nsud", raising=False)
    mod = importlib.import_module("nsud")

    def seed_plist(keys: dict[str, bytes], gzip_values: bool = True):
        data: dict = {"UserdataMigrated": True}
        for k, v in keys.items():
            data[k] = gzip.compress(v) if gzip_values else v
        plist_path.write_bytes(plistlib.dumps(data))

    def plist_keys():
        return set(plistlib.loads(plist_path.read_bytes()))

    def enable_tvos():
        monkeypatch.setattr(
            xbmc, "getCondVisibility", lambda cond: "TVOS" in cond, raising=False
        )

    return types.SimpleNamespace(
        mod=mod,
        userdata=userdata,
        plist_path=plist_path,
        seed_plist=seed_plist,
        plist_keys=plist_keys,
        enable_tvos=enable_tvos,
        deleted=deleted,
        state=state,
    )


def _write(base: Path, rel: str, content: bytes = b"<x/>") -> None:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


# --------------------------------------------------------------------------- #
# Out-of-scope key WITH a disk twin: the key (the shadow) goes, the disk file stays.
# --------------------------------------------------------------------------- #
def test_out_of_scope_key_with_disk_twin_is_purged_disk_kept(env):
    env.enable_tvos()
    rel = "addon_data/plugin.video.example/private.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<stale>old data</stale>"})
    _write(env.userdata, rel, b"<fresh>restored data</fresh>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 0, 0)
    assert KEY_PREFIX + rel not in env.plist_keys(), "the shadowing key must be gone"
    assert (env.userdata / rel).read_bytes() == b"<fresh>restored data</fresh>", (
        "the POSIX file must be untouched - only the key is purged"
    )
    assert env.deleted == [SPECIAL_PREFIX + rel]


# --------------------------------------------------------------------------- #
# Key-only file: the key may be the ONLY copy - materialize to disk, THEN purge.
# --------------------------------------------------------------------------- #
def test_key_only_file_is_materialized_then_purged(env):
    env.enable_tvos()
    rel = "addon_data/plugin.video.example/private.xml"
    content = b"<data>the owner's only copy of this file</data>"
    env.seed_plist({KEY_PREFIX + rel: content})  # gzipped, as Kodi stores it

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (1, 1, 0, 0)
    assert (env.userdata / rel).read_bytes() == content, (
        "the key's decoded content must land on disk BEFORE the key is dropped - "
        "the purge never destroys the only copy"
    )
    assert KEY_PREFIX + rel not in env.plist_keys()


def test_key_only_raw_uncompressed_value_also_materializes(env):
    env.enable_tvos()
    rel = "addon_data/plugin.foo/private.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<small/>"}, gzip_values=False)

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (1, 1, 0, 0)
    assert (env.userdata / rel).read_bytes() == b"<small/>"


def test_key_only_undecodable_value_keeps_the_key_and_counts_failed(env):
    env.enable_tvos()
    rel = "addon_data/plugin.foo/private.xml"
    env.seed_plist({KEY_PREFIX + rel: b""}, gzip_values=False)  # empty = undecodable

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 0, 1)
    assert KEY_PREFIX + rel in env.plist_keys(), (
        "a key that cannot be materialized is the only copy - it must be KEPT"
    )
    assert env.deleted == []


# --------------------------------------------------------------------------- #
# In-scope keys: on tvOS the key IS the live store - NEVER purged.
# --------------------------------------------------------------------------- #
def test_in_scope_keys_are_kept(env):
    env.enable_tvos()
    in_scope = (
        "guisettings.xml",  # top-level userdata xml
        "sources.xml",  # top-level userdata xml
        "addon_data/pvr.iptvsimple/settings.xml",
        "addon_data/pvr.iptvsimple/instance-settings-1.xml",
    )
    env.seed_plist({KEY_PREFIX + rel: b"<live/>" for rel in in_scope})

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, len(in_scope), 0)
    for rel in in_scope:
        assert KEY_PREFIX + rel in env.plist_keys(), "%s must survive" % rel
        assert not (env.userdata / rel).exists(), (
            "an in-scope key is the live store - nothing to materialize"
        )
    assert env.deleted == [], "no xbmcvfs.delete call may be made for in-scope keys"


def test_mixed_store_purges_only_the_out_of_scope_keys(env):
    env.enable_tvos()
    stale = "addon_data/plugin.video.example/private.xml"
    live = "guisettings.xml"
    env.seed_plist({KEY_PREFIX + stale: b"<stale/>", KEY_PREFIX + live: b"<live/>"})
    _write(env.userdata, stale, b"<fresh/>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 1, 0)
    assert KEY_PREFIX + live in env.plist_keys()
    assert KEY_PREFIX + stale not in env.plist_keys()


# --------------------------------------------------------------------------- #
# Android / Fire TV / desktop: a strict no-op (the hard platform gate).
# --------------------------------------------------------------------------- #
def test_android_is_a_strict_noop(env):
    # Default fake xbmc has no getCondVisibility -> _is_tvos() False (the safe answer).
    rel = "addon_data/script.skinshortcuts/menu.DATA.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<stale/>"})

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 0, 0)
    assert KEY_PREFIX + rel in env.plist_keys(), "the plist must be untouched"
    assert env.deleted == []
    assert not (env.userdata / rel).exists(), "nothing may be materialized off-tvOS"


def test_non_tvos_getcondvis_false_is_a_strict_noop(env, monkeypatch):
    # Real Fire TV / desktop: the condition EXISTS and answers False.
    monkeypatch.setattr(
        sys.modules["xbmc"], "getCondVisibility", lambda cond: False, raising=False
    )
    env.seed_plist({KEY_PREFIX + "addon_data/foo/private.xml": b"<stale/>"})

    assert env.mod.purge_stale_keys(str(env.userdata)) == (0, 0, 0, 0)
    assert env.deleted == []


# --------------------------------------------------------------------------- #
# The hard delete guards: paths WantsFile excludes must never reach xbmcvfs.delete,
# because there the delete dispatches to CPosixFile and removes the REAL disk file.
# --------------------------------------------------------------------------- #
def test_siriremote_key_is_never_deleted_and_posix_file_untouched(env):
    env.enable_tvos()
    rel = "keymaps/customcontroller.SiriRemote.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<key-anomaly/>"})
    _write(env.userdata, rel, b"<the only real copy/>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 1, 0)
    assert env.deleted == [], (
        "xbmcvfs.delete on a SiriRemote path would POSIX-delete the only copy"
    )
    assert (env.userdata / rel).read_bytes() == b"<the only real copy/>"
    assert KEY_PREFIX + rel in env.plist_keys()


# --------------------------------------------------------------------------- #
# keymaps/ keys are OUT of scope (2026-08-30, measured on atv1): a keymap key
# shadows the plain-open() writes Apple TV Fixes makes to keymaps forever, so
# the purge must clean any that exist. This is the self-heal path for the stray
# /userdata/keymaps/t7b-siriremote.xml key the .30.2 restore left on atv1: the
# boot-time once-per-version purge (service._maybe_purge_stale_nsud_keys) runs
# again on the version that ships this scope change and drops it.
# --------------------------------------------------------------------------- #
def test_stray_keymap_key_only_is_materialized_then_purged(env):
    # The exact atv1 state: the old rewrite vectored the keymap AND dropped the
    # POSIX copy, so the key is the ONLY copy. The purge must put the bytes back
    # on disk with plain open() semantics BEFORE dropping the key.
    env.enable_tvos()
    rel = "keymaps/t7b-siriremote.xml"
    content = b"<keymap><global><key id='61624'>Back</key></global></keymap>"
    env.seed_plist({KEY_PREFIX + rel: content})

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (1, 1, 0, 0)
    assert (env.userdata / rel).read_bytes() == content, (
        "the keymap must be materialized to POSIX before its key is dropped"
    )
    assert KEY_PREFIX + rel not in env.plist_keys(), (
        "the keymap key must be GONE - it shadows every future plain-open() "
        "keymap write Apple TV Fixes makes"
    )
    assert env.deleted == [SPECIAL_PREFIX + rel]


def test_stray_keymap_key_with_posix_twin_is_purged_disk_kept(env):
    # The other atv1-plausible state: Apple TV Fixes re-wrote the file with
    # plain open() after the restore, so a (newer) POSIX twin exists under the
    # (stale) key. The key goes, the disk file - the truth - stays untouched.
    env.enable_tvos()
    rel = "keymaps/t7b-siriremote.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<keymap>stale restore-era copy</keymap>"})
    _write(env.userdata, rel, b"<keymap>fresh apple-tv-fixes write</keymap>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 0, 0)
    assert KEY_PREFIX + rel not in env.plist_keys()
    assert (env.userdata / rel).read_bytes() == b"<keymap>fresh apple-tv-fixes write</keymap>"
    assert env.deleted == [SPECIAL_PREFIX + rel]


def test_profile_keymap_key_is_also_purged(env):
    # Per-profile keymaps are written the same plain-open() way; the scope rule
    # is the directory, not the one file the incident happened to hit.
    env.enable_tvos()
    rel = "profiles/Kids/keymaps/gen.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<keymap/>"})
    _write(env.userdata, rel, b"<keymap/>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 0, 0)
    assert KEY_PREFIX + rel not in env.plist_keys()
    assert (env.userdata / rel).exists()


def test_siriremote_customcontroller_still_kept_inside_keymaps_scope(env):
    # The WantsFile delete guard OUTRANKS the keymaps purge scope: an (impossible
    # by construction, guarded anyway) customcontroller.SiriRemote key must still
    # never reach xbmcvfs.delete, because there the delete would dispatch to
    # CPosixFile and remove the real disk file.
    env.enable_tvos()
    rel = "keymaps/customcontroller.SiriRemote.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<key-anomaly/>"})
    _write(env.userdata, rel, b"<the only real copy/>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 1, 0)
    assert env.deleted == []
    assert (env.userdata / rel).read_bytes() == b"<the only real copy/>"


def test_non_xml_key_is_never_deleted(env):
    env.enable_tvos()
    rel = "Thumbnails/oddball.jpg"  # cannot exist as a real key; hard guard anyway
    env.seed_plist({KEY_PREFIX + rel: b"jpegbytes"})
    _write(env.userdata, rel, b"jpegbytes")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 1, 0)
    assert env.deleted == []
    assert (env.userdata / rel).exists()


# --------------------------------------------------------------------------- #
# The lying boolean: a delete that silently did nothing must count as failed,
# never as purged (confirmation comes from re-reading the plist, not the return).
# --------------------------------------------------------------------------- #
def test_stuck_key_counts_failed_and_disk_twin_is_kept(env):
    env.enable_tvos()
    rel = "addon_data/plugin.video.example/private.xml"
    key = KEY_PREFIX + rel
    env.seed_plist({key: b"<stale/>"})
    _write(env.userdata, rel, b"<fresh/>")
    env.state["stuck_keys"] = {key}  # delete() returns True but removes nothing

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 0, 1)
    assert key in env.plist_keys()
    assert (env.userdata / rel).read_bytes() == b"<fresh/>"


def test_tvos_with_no_plist_is_a_noop(env):
    env.enable_tvos()  # tvOS claimed, but no NSUserDefaults store resolves
    assert env.mod.purge_stale_keys(str(env.userdata)) == (0, 0, 0, 0)
    assert env.deleted == []


def test_empty_userdata_root_is_a_noop(env):
    env.enable_tvos()
    env.seed_plist({KEY_PREFIX + "addon_data/foo/private.xml": b"<stale/>"})
    assert env.mod.purge_stale_keys("") == (0, 0, 0, 0)
    assert env.deleted == []


# --------------------------------------------------------------------------- #
# skin.estuary7 1.0.66+ durability sidecars: every skinshortcuts *.DATA.xml key is
# a LIVE dual-layer copy the skin's syncMenu maintains on every Home load - NEVER
# purge material. Purging them started the atv2 2026-07-17 war: purge drops the
# keys each boot, the skin re-registers them, and the run-once marker never sets.
# --------------------------------------------------------------------------- #
def test_skinshortcuts_data_keys_are_never_purged(env):
    env.enable_tvos()
    dual = "addon_data/script.skinshortcuts/mainmenu.DATA.xml"
    key_only = "addon_data/script.skinshortcuts/powermenu.DATA.xml"
    env.seed_plist(
        {
            KEY_PREFIX + dual: b"<menu>owner's custom menu</menu>",
            KEY_PREFIX + key_only: b"<menu>post-purge survivor</menu>",
        }
    )
    _write(env.userdata, dual, b"<menu>owner's custom menu</menu>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 2, 0)
    assert env.deleted == [], "no delete may ever be issued for a skin sidecar"
    assert KEY_PREFIX + dual in env.plist_keys()
    assert KEY_PREFIX + key_only in env.plist_keys()
    assert (env.userdata / dual).read_bytes() == b"<menu>owner's custom menu</menu>"
    assert not (env.userdata / key_only).exists(), (
        "a key-only sidecar is left to the skin's own syncMenu re-materialize - "
        "the purge does not touch either layer of a skin-owned file"
    )


def test_war_scenario_dual_layer_sidecars_all_kept_zero_failed(env):
    """The exact atv2 2026-07-17 state: skin 1.0.66 has dual-layered every
    skinshortcuts *.DATA.xml (key + byte-identical POSIX) and in-scope keys are
    live. The purge must skip ALL of it - zero deletes, zero failed - so the boot
    service's run-once marker finally sets (it requires failed == 0)."""
    env.enable_tvos()
    sidecars = {
        "addon_data/script.skinshortcuts/%s.DATA.xml" % name: b"<menu>%s</menu>"
        % name.encode()
        for name in ("mainmenu", "powermenu", "movies", "tvshows", "music")
    }
    in_scope = {
        "guisettings.xml": b"<gui/>",
        "sources.xml": b"<sources/>",
        "addon_data/pvr.iptvsimple/instance-settings-1.xml": b"<iptv/>",
    }
    env.seed_plist(
        {KEY_PREFIX + rel: data for rel, data in {**sidecars, **in_scope}.items()}
    )
    for rel, data in sidecars.items():
        _write(env.userdata, rel, data)  # byte-identical POSIX twin (syncMenu state)

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, len(sidecars) + len(in_scope), 0)
    assert env.deleted == []
    for rel in list(sidecars) + list(in_scope):
        assert KEY_PREFIX + rel in env.plist_keys(), "%s must survive" % rel
    for rel, data in sidecars.items():
        assert (env.userdata / rel).read_bytes() == data, (
            "the skin's POSIX layer must be untouched"
        )


def test_skinshortcuts_settings_xml_is_still_in_scope_not_a_sidecar(env):
    """The sidecar exclusion covers *.DATA.xml only. skinshortcuts' settings.xml
    is Kodi-framework-owned and already KEPT by _should_vector - pin that the two
    rules do not fight."""
    env.enable_tvos()
    rel = "addon_data/script.skinshortcuts/settings.xml"
    env.seed_plist({KEY_PREFIX + rel: b"<settings/>"})

    assert env.mod.purge_stale_keys(str(env.userdata)) == (0, 0, 1, 0)
    assert env.deleted == []
    assert KEY_PREFIX + rel in env.plist_keys()


# --------------------------------------------------------------------------- #
# The cfprefsd flush lag (the atv2 "N failed" defect): the on-disk plist does not
# reflect a delete until cfprefsd flushes, so a plist re-read right after the drop
# still shows the key. The verification must ride the LIVE layer (listdir's
# no-dedupe merge) and count the drop as purged, not failed.
# --------------------------------------------------------------------------- #
def test_purge_counts_survive_cfprefsd_flush_lag(env):
    env.enable_tvos()
    rel = "addon_data/plugin.video.example/private.xml"
    key = KEY_PREFIX + rel
    env.seed_plist({key: b"<stale/>"})
    _write(env.userdata, rel, b"<fresh/>")
    env.state["flush_lag"] = True  # delete drops the LIVE key; the plist stays stale

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 0, 0), (
        "a successful drop must count as purged even while the stale on-disk "
        "plist still shows the key - counting it failed is the every-boot "
        "retry loop from atv2"
    )
    assert key in env.plist_keys(), (
        "precondition: the stale plist snapshot really does still hold the key"
    )
    assert env.deleted == [SPECIAL_PREFIX + rel]


def test_flush_lag_with_stuck_key_still_counts_failed(env):
    """flush lag must not blind the verification to a GENUINELY stuck key: the
    live layer (listdir dup-count) still shows it twice, so it counts failed."""
    env.enable_tvos()
    rel = "addon_data/plugin.video.example/private.xml"
    key = KEY_PREFIX + rel
    env.seed_plist({key: b"<stale/>"})
    _write(env.userdata, rel, b"<fresh/>")
    env.state["flush_lag"] = True
    env.state["stuck_keys"] = {key}

    assert env.mod.purge_stale_keys(str(env.userdata)) == (0, 0, 0, 1)
    assert key in env.plist_keys()


def test_genuinely_stale_keys_purge_while_sidecars_survive(env):
    """The mixed post-1.0.66 box: skin sidecars + vector-everything-era leftovers
    in one store. Only the leftovers go; failed stays 0 so the marker can set."""
    env.enable_tvos()
    sidecar = "addon_data/script.skinshortcuts/mainmenu.DATA.xml"
    stale = "addon_data/plugin.video.example/private.xml"
    env.seed_plist({KEY_PREFIX + sidecar: b"<menu/>", KEY_PREFIX + stale: b"<old/>"})
    _write(env.userdata, sidecar, b"<menu/>")
    _write(env.userdata, stale, b"<new/>")

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 1, 1, 0)
    assert KEY_PREFIX + sidecar in env.plist_keys()
    assert KEY_PREFIX + stale not in env.plist_keys()
    assert env.deleted == [SPECIAL_PREFIX + stale]


def test_profile_prefixed_skin_sidecar_is_never_purged(env):
    """QA finding F1: a secondary profile's skinshortcuts sidecar
    (profiles/<name>/addon_data/script.skinshortcuts/*.DATA.xml) is just as
    skin-maintained as the master profile's and must be kept, both layers
    untouched."""
    env.enable_tvos()
    profile_sidecar = "profiles/Kids/addon_data/script.skinshortcuts/mainmenu.DATA.xml"
    env.seed_plist({KEY_PREFIX + profile_sidecar: b"<menu>kids menu</menu>"})

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 1, 0)
    assert env.deleted == [], "profile-prefixed sidecars are not purge material"
    assert KEY_PREFIX + profile_sidecar in env.plist_keys()
    assert not (env.userdata / profile_sidecar).exists(), (
        "re-materialization is the skin's job, same as the master-profile rule"
    )


def test_directory_shaped_twin_does_not_satisfy_only_copy_check(env):
    """QA finding F2: a DIRECTORY squatting on a stale key's rel is not a disk
    twin. The purge must not treat it as one - the materialize path runs, fails
    on the directory (OSError), and the key is KEPT as the only copy."""
    env.enable_tvos()
    stale = "addon_data/plugin.video.example/private.xml"
    env.seed_plist({KEY_PREFIX + stale: b"<only-copy/>"})
    (env.userdata / stale).mkdir(parents=True)  # adversarial dir shaped like the file

    result = env.mod.purge_stale_keys(str(env.userdata))

    assert result == (0, 0, 0, 1), "only-copy key must be kept and counted failed"
    assert env.deleted == [], "no delete may be issued while the key is the only copy"
    assert KEY_PREFIX + stale in env.plist_keys()


# ---------------------------------------------------------------------------
# sweep_iptv_instances: the restore-time stray sweep shares the purge's
# verification doctrine (live-layer dup-count, never a plist re-read). These
# pin the v2026.07.17.7 fix for the same cfprefsd-lag false-failure class.
# ---------------------------------------------------------------------------

IPTV_DIR = "addon_data/pvr.iptvsimple/"
ARMED = [IPTV_DIR + "settings.xml"]  # archive carries iptv addon_data -> sweep armed


def test_sweep_key_drop_survives_cfprefsd_flush_lag(env):
    """A successfully dropped stray-instance key must count REMOVED even while
    the stale on-disk plist snapshot still shows it (the restore's false
    'needs attention' class)."""
    env.enable_tvos()
    stray = IPTV_DIR + "instance-settings-2.xml"
    env.seed_plist({KEY_PREFIX + stray: b"<iptv-stray/>"})
    env.state["flush_lag"] = True  # delete drops the LIVE key; plist stays stale

    removed, failed = env.mod.sweep_iptv_instances(str(env.userdata), ARMED)

    assert (removed, failed) == (1, []), (
        "the lagging plist snapshot must not turn a successful sweep into a "
        "failed one - that is the purge's atv2 defect, in the restore path"
    )
    assert KEY_PREFIX + stray in env.plist_keys(), (
        "precondition: the stale snapshot really does still show the key"
    )


def test_sweep_stuck_key_still_counts_failed(env):
    """Dup-count verification must not go blind: a key whose delete silently
    did nothing (lying True) is still listed in the live layer and must be
    reported failed - it would shadow the restored config.

    The POSIX dir is seeded because that is the hardware-guaranteed shape at
    sweep time (the sweep runs post-extract; arming requires the archive to
    carry files under this exact dir). A MISSING dir makes the key layer
    unobservable on real Kodi (listdir returns empty before the key append) -
    that state is a loud PARTIAL restore already, not this test's subject."""
    env.enable_tvos()
    _write(env.userdata, IPTV_DIR + "settings.xml", b"<iptv/>")
    stray = IPTV_DIR + "instance-settings-3.xml"
    key = KEY_PREFIX + stray
    env.seed_plist({key: b"<iptv-stray/>"})
    env.state["stuck_keys"] = {key}

    removed, failed = env.mod.sweep_iptv_instances(str(env.userdata), ARMED)

    assert (removed, failed) == (0, [stray])
    assert key in env.plist_keys()


def test_sweep_multiple_strays_one_dir_mixed_outcome(env):
    """QA promotion: several strays in one dir through the cached listing -
    the clean drop counts removed, the stuck one counts failed, each exactly
    once (the listing is taken once per dir strictly AFTER all deletes)."""
    env.enable_tvos()
    _write(env.userdata, IPTV_DIR + "settings.xml", b"<iptv/>")
    clean = IPTV_DIR + "instance-settings-5.xml"
    stuck = IPTV_DIR + "instance-settings-6.xml"
    env.seed_plist({KEY_PREFIX + clean: b"<a/>", KEY_PREFIX + stuck: b"<b/>"})
    env.state["stuck_keys"] = {KEY_PREFIX + stuck}
    env.state["flush_lag"] = True

    removed, failed = env.mod.sweep_iptv_instances(str(env.userdata), ARMED)

    assert (removed, failed) == (1, [stuck])


def test_sweep_dual_layer_stray_removed_cleanly(env):
    """A stray present in BOTH layers (POSIX file + key) sweeps clean: both
    copies gone, counted removed once, nothing failed."""
    env.enable_tvos()
    stray = IPTV_DIR + "instance-settings-4.xml"
    env.seed_plist({KEY_PREFIX + stray: b"<iptv-stray/>"})
    _write(env.userdata, stray, b"<iptv-stray/>")
    env.state["flush_lag"] = True

    removed, failed = env.mod.sweep_iptv_instances(str(env.userdata), ARMED)

    assert (removed, failed) == (1, [])
    assert not (env.userdata / stray).exists()


def test_sweep_carried_instance_is_never_touched(env):
    """An instance file the archive carries is not a stray: neither layer may
    be touched, in any lag state."""
    env.enable_tvos()
    carried = IPTV_DIR + "instance-settings-1.xml"
    env.seed_plist({KEY_PREFIX + carried: b"<iptv-carried/>"})
    _write(env.userdata, carried, b"<iptv-carried/>")
    env.state["flush_lag"] = True

    removed, failed = env.mod.sweep_iptv_instances(str(env.userdata), ARMED + [carried])

    assert (removed, failed) == (0, [])
    assert env.deleted == []
    assert (env.userdata / carried).read_bytes() == b"<iptv-carried/>"
