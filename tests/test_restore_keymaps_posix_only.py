"""The restore's per-file write-API split, proven on the two-layer tvOS storage fake.

MEASURED on atv1, 2026-08-30 (the restore-cycle hardware proof): the .30.2 restore's
durability rewrite vectored `keymaps/t7b-siriremote.xml` into NSUserDefaults and dropped
the POSIX copy. Kodi's own CTVOSFile::WantsFile has NO keymaps carve-out - it vectors ANY
userdata *.xml written through the VFS - so the scope decision is OURS (`_should_vector`),
and before 2026-08-30 it approved every non-addon_data userdata xml, keymaps included.

Why that is a data-shadow bug: Apple TV Fixes (service.tvos.pythonfix) writes
`keymaps/t7b-siriremote.xml` with plain open() - the POSIX-only contract in the Apple TV
playbook, SKILL.md section 15. A key SHADOWS the disk file (CTVOSFile reads the key
FIRST), so once the restore created that key, every future keymap update was invisible to
Kodi forever.

The contract pinned here, on FakeKodiStorage (the WantsFile-accurate two-layer model, so a
regression fails exactly the way the real Apple TV failed):

  * the extract lands every member as plain POSIX (seed_disk IS that write);
  * the durability rewrite vectors ONLY what `_should_vector` approves: an in-scope
    member (guisettings.xml) ends key-only - that durability is load-bearing and stays;
  * a keymaps/ member ends disk-only with ZERO key created, master and per-profile both;
  * off tvOS nothing changes for anyone (the split is invisible on Fire TV / desktop).

The real nsud module runs against the fake - one predicate, `_should_vector`, decides
both the rewrite and the purge, so the two can never drift (the 23-false-hits lesson).
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from fake_kodi_storage import FakeKodiStorage, make_modules  # noqa: E402

ADDON_MODULES = (
    Path(__file__).parent.parent
    / "script.ezmaintenanceplusplus"
    / "resources"
    / "lib"
    / "modules"
)


@pytest.fixture
def tvos(monkeypatch, tmp_path):
    """The real nsud, imported against the two-layer tvOS fake."""
    store = FakeKodiStorage(tmp_path / "kodi", platform="tvos")
    xbmc_cls, vfs_cls = make_modules(store)
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_cls)
    monkeypatch.setitem(sys.modules, "xbmcvfs", vfs_cls)
    monkeypatch.syspath_prepend(str(ADDON_MODULES))
    monkeypatch.delitem(sys.modules, "nsud", raising=False)
    nsud = importlib.import_module("nsud")
    return store, nsud


def test_fake_models_wantsfile_with_no_keymaps_carveout(tvos):
    # The load-bearing premise: Kodi itself WOULD vector a keymap written through
    # the VFS (WantsFile matches any userdata xml bar customcontroller.SiriRemote*).
    # If this ever goes False the fake stopped modeling the mechanism and every
    # green below is vacuous.
    store, _nsud = tvos
    assert store.wants("special://home/userdata/keymaps/t7b-siriremote.xml") is True
    assert (
        store.wants("special://home/userdata/keymaps/customcontroller.SiriRemote.xml")
        is False
    )


def test_restore_sequence_keymap_stays_posix_only_in_scope_still_vectors(tvos):
    store, nsud = tvos
    # The extract: plain POSIX writes, exactly what zipfile.extract produces.
    store.seed_disk("guisettings.xml", b"<settings><setting id='x'>1</setting></settings>")
    store.seed_disk("keymaps/t7b-siriremote.xml", b"<keymap><global/></keymap>")
    store.seed_disk("profiles/Kids/keymaps/gen.xml", b"<keymap/>")

    # The restore's durability rewrite (wiz calls this right after the extract).
    written, skipped, failed = nsud.rewrite_userdata_xml(store.userdata)

    assert failed == 0
    # In scope: vectored into the key layer, POSIX twin dropped - the durable
    # tvOS state this whole mechanism exists to produce. Still load-bearing.
    assert store.state("guisettings.xml") == "key-only"
    # keymaps: POSIX-only, ZERO key created, master and per-profile both. This is
    # the state Apple TV Fixes' plain-open() writes require to stay visible.
    assert store.state("keymaps/t7b-siriremote.xml") == "disk-only"
    assert store.state("profiles/Kids/keymaps/gen.xml") == "disk-only"
    assert not any("keymaps" in k for k in store.keys), (
        "no keymap key may exist after a restore: a key shadows every future "
        "plain-open() keymap write forever (atv1, 2026-08-30)"
    )


def test_restored_keymap_readable_after_rewrite_both_access_styles(tvos):
    # The point of POSIX-only: BOTH access styles see the restored bytes. Kodi's
    # VFS read falls back to CPosixFile when no key exists; Apple TV Fixes reads
    # and writes with plain open().
    store, nsud = tvos
    payload = b"<keymap><global><key id='61624'>Back</key></global></keymap>"
    store.seed_disk("keymaps/t7b-siriremote.xml", payload)

    nsud.rewrite_userdata_xml(store.userdata)

    special = "special://home/userdata/keymaps/t7b-siriremote.xml"
    assert bytes(store.vfs_read(special)) == payload, "Kodi's VFS read (disk fallback)"
    real = store.translate(special)
    with open(real, "rb") as fh:
        assert fh.read() == payload, "the plain-open() read Apple TV Fixes does"


def test_android_restore_sequence_is_all_posix_no_behavior_change(monkeypatch, tmp_path):
    store = FakeKodiStorage(tmp_path / "kodi", platform="android")
    xbmc_cls, vfs_cls = make_modules(store)
    monkeypatch.setitem(sys.modules, "xbmc", xbmc_cls)
    monkeypatch.setitem(sys.modules, "xbmcvfs", vfs_cls)
    monkeypatch.syspath_prepend(str(ADDON_MODULES))
    monkeypatch.delitem(sys.modules, "nsud", raising=False)
    nsud = importlib.import_module("nsud")

    store.seed_disk("guisettings.xml", b"<settings/>")
    store.seed_disk("keymaps/t7b-siriremote.xml", b"<keymap/>")

    written, skipped, failed = nsud.rewrite_userdata_xml(store.userdata)

    assert failed == 0
    assert store.state("guisettings.xml") == "disk-only"  # no key store on Fire TV
    assert store.state("keymaps/t7b-siriremote.xml") == "disk-only"
