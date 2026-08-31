"""Make a restore stick on Apple TV (tvOS) by re-writing the restored userdata *.xml
THROUGH xbmcvfs, so Kodi vectors each file into NSUserDefaults.

Why this exists (root cause, verified in Kodi's tvOS source `xbmc/platform/darwin/tvos/`):
tvOS gives an app ~500 KB of normal app-directory storage, so Kodi stores `userdata/*.xml`
in the app's NSUserDefaults; a key SHADOWS the disk file (reads check the key FIRST) and
Kodi NEVER copies a key back to disk (corrected fact, see CLAUDE.md and the NOTE below). A
`.xml` write THROUGH xbmcvfs is dispatched to `CTVOSFile::Write` ->
`CTVOSNSUserDefaults::SetKeyDataFromPath(..., synchronize=true)` -> `[NSUserDefaults
synchronize]`, i.e. persisted to the only durable tvOS store BEFORE the call returns, with
no dependency on a clean shutdown. But the restore extracts with plain Python `zipfile`
(`zin.extract`) = a plain POSIX write that BYPASSES CTVOSFile, so the restored files never
reach NSUserDefaults and are shadowed by the stale mirror at boot. Re-writing each restored
`.xml` through xbmcvfs here dissolves that shadow. On Fire TV / desktop the same call is a
harmless rewrite of identical bytes.

This module is deliberately IPTV-free. It does NOT enable, disable, stage, probe, or in any
way manage pvr.iptvsimple - a restore must never touch the IPTV client. It only re-writes
restored userdata *.xml so tvOS keeps them.

Hard rules (see docs/plans/atv-restore-*.md and the adversarial review that produced them):
- SINGLE write per file, NEVER chunk. `CTVOSFile::Write` REPLACES the whole NSUserDefaults
  key on every call, so a chunked loop would leave only the last chunk -> a truncated XML
  fragment -> settings reset to defaults, unrecoverable (worse than the shadow bug). Read
  the whole file with plain `open()` (per kodi-vfs-cannot-read-foreign-local-files.md the
  READ must be plain, never xbmcvfs), write it in ONE `xbmcvfs.File.write` call, check the
  return.
- EXCLUDE this add-on's own settings.xml (carries the SOURCE box's paths + Dropbox secret,
  and service.py int()-parses those at import -> would crash the boot service).
"""

import gzip
import os
import re

import xbmcvfs

ADDON_ID = "script.ezmaintenanceplusplus"

# Files (relative to userdata/, forward-slash) the general walk must NOT re-vector.
DEFAULT_EXCLUDES = (
    # Our own settings carry the source box's download/restore paths AND its
    # dropbox_refresh_token (a secret); service.py reads several at import with int(),
    # so a foreign/blank value would crash the boot service.
    "addon_data/%s/settings.xml" % ADDON_ID,
)


def _special_for(rel):
    """special:// path Kodi routes through CTVOSFile on tvOS (the /userdata key match)."""
    return "special://home/userdata/" + rel.replace("\\", "/")


def _is_tvos():
    """True ONLY on Apple TV (tvOS).

    This gate is a HARD safety boundary. On tvOS a write to
    ``special://home/userdata/<rel>`` is dispatched to ``CTVOSFile`` and vectored into
    NSUserDefaults, which is a SEPARATE entity from the POSIX disk file - so after the
    rewrite the file exists in BOTH layers and tvOS File Manager lists it twice. On EVERY
    other platform (Fire TV / Android / desktop) that same ``special://`` path IS the POSIX
    disk file, so the caller must NEVER remove the POSIX copy there - doing so would delete
    the file just written. Detected via Kodi's own platform condition; defaults to False
    (the safe answer) on any error, so the POSIX copy is only ever dropped when tvOS is
    positively confirmed.
    """
    try:
        import xbmc

        return bool(xbmc.getCondVisibility("System.Platform.TVOS"))
    except Exception:
        return False


def _vfs_rewrite_once(posix_src, special_dst):
    """Read the whole file with PLAIN python, write it in EXACTLY ONE xbmcvfs write.

    Returns True only on a confirmed write. On any failure returns False and leaves the
    POSIX source untouched (so the worst case is the pre-existing shadow, never data loss).
    NEVER chunk here (see module docstring) and NEVER reuse ui.py's _stream_copy/_LocalReader.
    """
    try:
        with open(posix_src, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    f = None
    try:
        f = xbmcvfs.File(special_dst, "w")
        ok = f.write(
            bytearray(data)
        )  # ONE call, full payload; check the boolean return
        return bool(ok)
    except Exception:
        return False
    finally:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass


def _should_vector(rel):
    """True iff `rel` is a file KODI ITSELF reads through its VFS, and therefore the ONLY
    class of file that needs vectoring into NSUserDefaults on tvOS.

    This is the scoping our own incident doc demanded and 2026.07.08.6 never delivered:
    "the rewrite OVER-APPLIES to every *.xml rather than only the files that genuinely
    need durability" -> "a tvOS durability rewrite ... must be scoped to exactly the files
    that need it".

    Proven from Kodi's tvOS source (Omega):
    - `CTVOSFile::Exists` (TVOSFile.cpp:113-122) checks the NSUserDefaults KEY FIRST and
      only falls back to `CPosixFile`. So a key SHADOWS the disk file - that, not any
      disk-rewrite, is why a file-only restore "reverts".
    - `CPreflightHandler::MigrateUserdataXMLToNSUserDefaults` (PreflightHandler.mm:81-93)
      returns early once `UserdataMigrated` is set: Kodi does NOT rewrite disk from the
      mirror on launch. Vectoring is therefore needed ONLY to defeat that shadowing.
    - `CTVOSDirectory::GetDirectory` (TVOSDirectory.cpp:48-105) lists POSIX files and then
      `items.Add()`s every NSUserDefaults key WITHOUT deduping -> a file present in both
      layers is listed TWICE (the File Manager duplicate bug).

    So we vector exactly what Kodi reads via the VFS and nothing else:
      * top-level `userdata/*.xml` (guisettings, profiles, sources, RssFeeds, favourites...)
        EXCEPT anything under a `keymaps/` dir, which stays POSIX-only: Apple TV Fixes
        writes keymaps with plain open() (playbook section 15), so a key would shadow
        every future keymap update (measured on atv1, 2026-08-30 - see the guard below)
      * `addon_data/<id>/settings.xml` and `addon_data/<id>/instance-settings-*.xml` - BOTH
        are owned and read by Kodi's own add-on-settings framework through the VFS, not by
        the add-on with plain open(). (No IPTV special-casing: this is a generic rule about
        who READS the file, and it happens to include pvr.iptvsimple's instance settings the
        same as any other add-on's.)

    Everything ELSE under `addon_data/` is treated as an add-on's PRIVATE data and is left
    alone. PROVEN for `script.skinshortcuts` (a Python add-on): it never calls
    `xbmcvfs.File()` at all - it guards with `xbmcvfs.exists()` (which finds the key -> True)
    and then parses the REAL PATH with ElementTree/`open()` (which raises), swallows it, and
    silently falls back to the skin's SHIPPED DEFAULT menu. That mixed-mode access is exactly
    what made a restore wipe the owner's customized main menu. For OTHER add-ons (esp. binary
    ones, which can persist private xml via `kodi::vfs::CFile` -> the same VFS) this is an
    ASSUMPTION, not a proof; the conservative choice is still to leave the file on disk, since
    a POSIX copy is readable by BOTH access styles while an NSUserDefaults-only file is
    readable by only one.

    Leaving those files as plain POSIX is exactly how they behave on Fire TV / desktop.

    NOTE - a claim this module used to make is FALSE: Kodi does NOT re-materialize the disk
    file from NSUserDefaults on the next launch. `MigrateUserdataXMLToNSUserDefaults`
    (PreflightHandler.mm:81-93) returns early once `UserdataMigrated` is set, and nothing in
    TVOSFile/TVOSDirectory/PreflightHandler ever writes a key back out to POSIX. A vectored
    file simply gets SERVED from the key forever. That is why dropping the POSIX copy of a
    file whose owner reads it with plain `open()` is unrecoverable.

    DURABILITY BOUNDARY (deliberate): on tvOS the whole Kodi home lives under
    `Library/Caches` (Apple mandates it; Documents is write-prohibited), so it is purgeable
    under storage pressure. We do NOT try to buy durability for private add-on data by
    vectoring it: NSUserDefaults is a SHARED ~500 KB budget that Kodi never enforces or checks
    (TVOSNSUserDefaults.mm SetKeyData), so pushing a menu file that grows with every shortcut
    into it risks silently evicting/truncating `guisettings.xml`. The durability boundary for
    private add-on data is this add-on's own BACKUP, not NSUserDefaults.
    """
    rel = (rel or "").replace("\\", "/").lstrip("/")
    base = os.path.basename(rel)
    # Kodi's own WantsFile() (TVOSFile.cpp:41-44) EXCLUDES customcontroller.SiriRemote*, so
    # such a file is served by CPosixFile, NOT CTVOSFile. An xbmcvfs write to it is a plain
    # POSIX write and the read-back is a POSIX read of the same file: `_vector_confirmed`
    # would ALWAYS pass without a single byte ever reaching NSUserDefaults, and we would then
    # os.remove() the ONLY copy. Never vector (and therefore never drop) it.
    if base.lower().startswith("customcontroller.siriremote"):
        return False
    # keymaps/ (master profile AND profiles/<name>/keymaps/) must stay POSIX-only.
    # MEASURED on atv1 2026-08-30 (the restore-cycle hardware proof): the restore's
    # durability rewrite vectored keymaps/t7b-siriremote.xml into NSUserDefaults and
    # dropped the POSIX copy. Apple TV Fixes (service.tvos.pythonfix) writes that file
    # with plain open() - the POSIX-only contract in the Apple TV playbook, SKILL.md
    # section 15 - so once a key exists it SHADOWS every future keymap update forever
    # (Kodi reads keymaps through its VFS, key first). Excluding the whole keymaps dir
    # here keeps the extract's plain-POSIX copy authoritative, exactly as on Fire TV /
    # desktop, and makes purge_stale_keys treat any existing keymap key as
    # out-of-scope: materialize-if-only-copy, then drop (that purge is what heals the
    # stray key already sitting on atv1 at its first boot after this ships).
    if "keymaps" in rel.lower().split("/")[:-1]:
        return False
    if "addon_data/" not in rel:
        return True  # top-level userdata xml: Kodi reads it through the VFS
    return base == "settings.xml" or base.startswith("instance-settings-")


def _vector_confirmed(special_dst, posix_src):
    """Read the just-vectored file BACK through xbmcvfs (on tvOS this reads NSUserDefaults)
    and confirm it byte-matches the POSIX source. Returns True ONLY on a full, non-empty
    match.

    This is the safety gate for dropping the POSIX copy: ``xbmcvfs.File.write`` returning
    True is not proof the durable store actually holds the bytes. tvOS gives the app a tiny
    NSUserDefaults budget (the ~500 KB limit this whole mechanism exists for), so a write
    could report success while the store silently truncates or evicts the key. Deleting the
    POSIX copy then would lose the only good copy. Requiring a read-back match turns "trust
    the write bool" into "positive evidence the store holds the identical content" before any
    irreversible delete. Any read failure or mismatch -> False -> keep the POSIX copy."""
    try:
        with open(posix_src, "rb") as fh:
            expected = fh.read()
    except OSError:
        return False
    if not expected:
        return False
    f = None
    try:
        f = xbmcvfs.File(special_dst)
        got = f.readBytes()
        got = bytes(got) if got is not None else b""
    except Exception:
        return False
    finally:
        try:
            if f is not None:
                f.close()
        except Exception:
            pass
    return got == expected


def rewrite_userdata_xml(
    userdata_dir,
    exclude_rel=DEFAULT_EXCLUDES,
    exclude_dir_prefixes=(),
    log=None,
    drop_posix_on_tvos=True,
):
    """Re-write every *.xml under userdata_dir through xbmcvfs. Returns
    (written, skipped, failed). Fully guarded; never raises.

    `exclude_dir_prefixes` is a GENERIC opt-out (userdata-relative, forward-slash) with an
    empty default - no add-on is special-cased. It never singles out pvr.iptvsimple.

    tvOS duplicate-entry fix: on Apple TV the POSIX file the restore extracted and the
    NSUserDefaults key this write creates are TWO entities, so File Manager lists every
    userdata file twice (see the tvOS-restore-duplicate-userdata incident). After a
    CONFIRMED vector (``_vfs_rewrite_once`` returned True, i.e. the bytes are in
    NSUserDefaults), and ONLY when ``_is_tvos()`` positively confirms Apple TV, remove the
    now-redundant POSIX copy so only the coherent CTVOSFile/NSUserDefaults entity remains.
    The key is then the ONLY copy - Kodi never re-materializes a disk file from a key
    (corrected fact; the opposite claim caused the 2026-07-14 data loss) - a conscious
    zero-fallback trade made only for files Kodi itself reads through the VFS. This is
    ordered write-then-delete (never delete a file whose content is not already durably in
    NSUserDefaults) and is a strict no-op on Fire TV / Android / desktop, where the same
    ``special://`` path IS the POSIX file. ``drop_posix_on_tvos=False`` disables it entirely
    (kept as an escape hatch and for the pre-fix behaviour in tests)."""
    written = skipped = failed = dropped = 0
    excl = {x.replace("\\", "/") for x in exclude_rel}
    prefixes = tuple(p.replace("\\", "/") for p in exclude_dir_prefixes)
    drop = bool(drop_posix_on_tvos) and _is_tvos()
    try:
        for dirpath, _dirnames, filenames in os.walk(userdata_dir):
            for name in filenames:
                if not name.lower().endswith(".xml"):
                    continue
                posix = os.path.join(dirpath, name)
                rel = os.path.relpath(posix, userdata_dir).replace("\\", "/")
                if rel in excl or any(rel.startswith(p) for p in prefixes):
                    skipped += 1
                    continue
                if not _should_vector(rel):
                    # An add-on's PRIVATE data (addon_data/<id>/* that is not settings.xml).
                    # Its owner reads it with plain open(), never through Kodi's VFS, so a
                    # NSUserDefaults key buys nothing, duplicates the File Manager entry, and
                    # (once the POSIX copy was dropped) made the file unreadable to its owner
                    # - which silently reset the skinshortcuts main menu on every restore.
                    # Leave it as a plain POSIX file, exactly as on Fire TV / desktop.
                    skipped += 1
                    continue
                special = _special_for(rel)
                if _vfs_rewrite_once(posix, special):
                    written += 1
                    # tvOS ONLY, and ONLY after a READ-BACK confirms NSUserDefaults holds the
                    # identical bytes: drop the redundant POSIX copy so File Manager stops
                    # listing the file twice. The disk file returns coherently on the next
                    # launch. Read-back (not just the write bool) guards the tvOS store
                    # silently truncating a large key. Guarded; a failed remove just leaves
                    # the (harmless) duplicate, never an exception.
                    # Only files Kodi reads THROUGH its VFS reach here (see _should_vector),
                    # so dropping the POSIX shadow is correct for exactly those and can no
                    # longer orphan an add-on's private data (the skinshortcuts menu bug).
                    if drop and _vector_confirmed(special, posix):
                        try:
                            os.remove(posix)
                            dropped += 1
                        except OSError:
                            pass
                else:
                    failed += 1
    except Exception:
        pass
    if log:
        log(
            "nsud: userdata xml re-write: %d written, %d skipped, %d failed, "
            "%d posix-dropped (tvOS)" % (written, skipped, failed, dropped)
        )
    return (written, skipped, failed)


def persist_one(rel, log=None):
    """Persist ONE already-on-disk userdata file (userdata-relative, e.g.
    "sources.xml") the tvOS-safe way, for a caller that just wrote it with plain
    POSIX and would otherwise leave it dual-layered on Apple TV.

    Same ordered write-through -> read-back -> drop-POSIX as
    ``rewrite_userdata_xml``, for a single file: vector it through xbmcvfs (->
    NSUserDefaults on tvOS) and, ONLY on tvOS and ONLY after a read-back confirms
    the store holds the identical bytes, drop the now-redundant POSIX copy so File
    Manager stops listing the file (and its contents) twice - the
    tvOS-restore-duplicate-userdata bug. A strict no-op rewrite of identical bytes
    on Fire TV / Android / desktop (there the special:// path IS the POSIX file).

    Returns True iff the file is now coherently persisted: the vector was written AND (on
    tvOS) a read-back confirmed the durable store holds the identical bytes, OR the file is
    private add-on data that is deliberately left as plain POSIX. Returns False on a failed
    write and on an UNCONFIRMED read-back - in both cases the POSIX copy is intentionally
    left standing, so False means "your bytes are still on disk but NOT durably vectored",
    never "your data is gone". Guarded, never raises."""
    rel = (rel or "").replace("\\", "/")
    special = _special_for(rel)
    posix = xbmcvfs.translatePath(special)
    try:
        if not _should_vector(rel):
            # An add-on's PRIVATE data: its owner reads it with plain open(), never through
            # Kodi's VFS. Vectoring buys nothing, duplicates the File Manager entry, and
            # dropping the POSIX copy would make it unreadable to its owner. Leave it alone.
            if log:
                log("nsud.persist_one: %s is private add-on data - left on disk" % rel)
            return True
        if not _vfs_rewrite_once(posix, special):
            if log:
                log("nsud.persist_one: vector failed for %s (POSIX stands)" % rel)
            return False
        if _is_tvos():
            if not _vector_confirmed(special, posix):
                # write() said OK but the read-back does not match, so the durable store
                # does NOT provably hold the bytes (the tvOS budget silently evicting or
                # truncating a key). Keeping the POSIX copy is right - it is now the only
                # good copy - but this is NOT the confirmed vector the caller asked for, so
                # say so instead of reporting success. Same shape as the _vfs_rewrite_once
                # failure above: log + return False, never a half-truth.
                if log:
                    log(
                        "nsud.persist_one: read-back did not confirm %s "
                        "(POSIX stands as the only copy)" % rel
                    )
                return False
            try:
                os.remove(posix)
            except OSError:
                pass
        if log:
            log("nsud.persist_one: persisted %s" % rel)
        return True
    except Exception:  # noqa: BLE001 - never abort the caller
        return False


# --------------------------------------------------------------------------- #
# Stale-key purge (the vector-everything-era cleanup).
# --------------------------------------------------------------------------- #

# NSUserDefaults key namespace Kodi uses for vectored userdata files
# (TVOSNSUserDefaults::IsKeyFromPath: the path under <home>/userdata -> "/userdata/<rel>").
_NSUD_KEY_PREFIX = "/userdata/"


def _load_plist(path):
    """Load one binary/xml plist; None on any failure (never raises)."""
    try:
        import plistlib

        with open(path, "rb") as fh:
            return plistlib.load(fh)
    except Exception:
        return None


def _find_nsud_plist():
    """Locate Kodi's NSUserDefaults backing store: the plist at
    `<sandbox>/Library/Preferences/<bundle-id>.plist`. Same resolution as nsub.py's
    capture (kept local so nsud never imports nsub): from special://home
    (.../Library/Caches/Kodi on tvOS) walk up to Library/Preferences and pick the
    .plist that actually holds `/userdata/*` keys - confirmed by CONTENT, never by
    name alone. Returns (path, loaded_dict) or (None, None). This shape only
    resolves on tvOS; on Fire TV / desktop the dir does not exist (or holds no such
    plist), so callers no-op."""
    try:
        home = xbmcvfs.translatePath("special://home")
    except Exception:
        return (None, None)
    prefs = os.path.normpath(os.path.join(home, "..", "..", "Preferences"))
    try:
        names = [n for n in os.listdir(prefs) if n.endswith(".plist")]
    except OSError:
        return (None, None)
    names.sort(key=lambda n: ("kodi" not in n.lower(), n))
    for name in names:
        path = os.path.join(prefs, name)
        data = _load_plist(path)
        if data is None:
            continue
        if any(isinstance(k, str) and k.startswith(_NSUD_KEY_PREFIX) for k in data):
            return (path, data)
    return (None, None)


def _decode_key_value(v):
    """An NSUserDefaults plist value -> the real file bytes. Kodi gzip-compresses the
    value (small ones may be stored raw). Returns bytes, or None if empty/undecodable."""
    try:
        raw = bytes(v)
    except Exception:
        return None
    if not raw:
        return None
    if raw[:2] == b"\x1f\x8b":  # gzip magic
        try:
            return gzip.decompress(raw)
        except Exception:
            return None
    return raw


# skin.estuary7 1.0.66+ deliberately DUAL-LAYERS every skinshortcuts *.DATA.xml on
# tvOS: the skin's syncMenu re-registers a byte-identical NSUserDefaults key for
# each menu file on every Home load, as the durability sidecar that lets the
# owner's custom menu survive a Library/Caches purge. Those keys are maintained
# LIVE by the skin - they are NOT vector-everything-era leftovers, and purging
# them starts an infinite war the purge cannot win: it drops them each boot, the
# skin re-registers them at the next Home load, and the run-once marker never
# sets (observed on atv2, 2026-07-17). They are KEPT, always.
_SKINSHORTCUTS_SIDECAR_PREFIX = "addon_data/script.skinshortcuts/"
_SKINSHORTCUTS_SIDECAR_SUFFIX = ".data.xml"


def _is_skin_menu_sidecar(rel):
    """True iff `rel` is a skinshortcuts menu file whose NSUserDefaults key is the
    skin's own durability sidecar (see the comment above) - never purge material.

    Matches the master profile form AND the per-profile form
    (profiles/<name>/addon_data/...): a secondary profile's menu sidecar is just
    as skin-maintained, and purging it would leave that profile's menu with only
    the purgeable POSIX copy until its next Home load (QA finding F1)."""
    r = (rel or "").replace("\\", "/").lstrip("/").lower()
    if not r.endswith(_SKINSHORTCUTS_SIDECAR_SUFFIX):
        return False
    if r.startswith(_SKINSHORTCUTS_SIDECAR_PREFIX):
        return True
    if r.startswith("profiles/"):
        parts = r.split("/", 2)
        return len(parts) == 3 and parts[2].startswith(_SKINSHORTCUTS_SIDECAR_PREFIX)
    return False


def _vfs_dir_names(reldir):
    """File names xbmcvfs.listdir reports for special://home/userdata/<reldir>,
    or None when no listing could be observed.

    On tvOS this is the one public observable that can see the LIVE key layer
    next to the POSIX layer: CTVOSDirectory::GetDirectory
    (TVOSDirectory.cpp:48-106) lists the POSIX files and then appends every
    NSUserDefaults key in the dir WITHOUT dedupe. So with a POSIX twin on disk, a
    basename listed TWICE proves the key is still live, and a single listing
    proves the key is gone. Re-reading the backing plist off disk instead is NOT
    a valid post-delete observation: cfprefsd owns that file and flushes it
    lazily, so a just-dropped key can sit in the stale on-disk snapshot
    indefinitely - which made every successful drop count as failed and kept the
    boot service retrying forever (atv2, 2026-07-17)."""
    special = "special://home/userdata" + ("/" + reldir if reldir else "")
    try:
        _dirs, files = xbmcvfs.listdir(special)
        return [
            f.decode("utf-8", "replace") if isinstance(f, bytes) else str(f)
            for f in files
        ]
    except Exception:
        return None


def purge_stale_keys(userdata_root, log=None):
    """Purge NSUserDefaults keys left over from the vector-everything era. tvOS ONLY.

    EZM++ 2026.07.08.2 through 2026.07.13.x vectored EVERY restored userdata *.xml into
    NSUserDefaults. `_should_vector` was later scoped down to only the files Kodi itself
    reads through its VFS, but the stale keys for the now-out-of-scope files (add-on
    private xml such as script.skinshortcuts' *.DATA.xml) are still in the store - and a
    key SHADOWS the POSIX file (CTVOSFile::Exists/Open check the key FIRST,
    TVOSFile.cpp:113-122), so a restored or hand-copied file silently never applies.
    This is the general cleanup: enumerate every `/userdata/*` key and drop the ones the
    current policy would not have created.

    Per key:
      * IN-SCOPE (`_should_vector(rel)` True): KEPT, always. On tvOS the key IS the live
        durable store for that file; purging it would destroy the setting.
      * customcontroller.SiriRemote* or any non-.xml relpath: KEPT and NEVER passed to
        xbmcvfs.delete. Kodi's WantsFile (TVOSFile.cpp:39-45) excludes those paths from
        CTVOSFile, so the delete would dispatch to CPosixFile and remove the REAL disk
        file - the only copy. (No such key should exist, since only VFS writes create
        keys; this is a hard guard, not an expectation.)
      * addon_data/script.skinshortcuts/*.DATA.xml: KEPT, always. Since skin.estuary7
        1.0.66 the skin deliberately dual-layers every menu file on tvOS - its syncMenu
        re-registers a byte-identical durable key on every Home load, so the owner's
        custom menu survives a Library/Caches purge. Those keys are the skin's LIVE
        durability sidecars, not stale-era leftovers. Purging them is an infinite war
        the purge cannot win: the skin re-registers each key at the next Home load and
        the run-once marker never sets (observed on atv2, 2026-07-17).
      * OUT-OF-SCOPE: purged. SAFETY FIRST: if no POSIX file exists at
        `<userdata_root>/<rel>` the key is the ONLY copy of that file, so its decoded
        content is materialized to disk with plain open() BEFORE the key is dropped.
        Plain open() is deliberate twice over: on tvOS an xbmcvfs write to a userdata
        xml goes to NSUserDefaults and never reaches disk, and the out-of-scope files
        are exactly the ones whose owners read them with plain open(). A key that cannot
        be decoded or written is KEPT and counted failed - the purge never destroys the
        only copy of anything.

    The drop itself is `xbmcvfs.delete("special://home/userdata/<rel>")`: on tvOS that
    dispatches to CTVOSFile::Delete -> DeleteKeyFromPath, which removes ONLY the key and
    (for WantsFile-eligible paths) never touches the POSIX file - the one job that API
    is actually good for. Its boolean return is True whether or not a key existed
    (TVOSNSUserDefaults.mm:188-202), so it proves nothing. Re-reading the backing plist
    proves nothing either: cfprefsd flushes that file lazily, so right after the delete
    it still shows the OLD snapshot and every successful drop counts as failed (the
    marker-never-sets every-boot retry loop, atv2 2026-07-17). Each drop is instead
    verified against the LIVE key layer via xbmcvfs.listdir's no-dedupe merge
    (TVOSDirectory.cpp:48-106, see _vfs_dir_names): every purge candidate has a POSIX
    twin on disk by construction (pre-existing or just materialized), so its basename
    listed TWICE means the key survived (failed) and a single listing means it is gone
    (purged). When no listing is observable at all, the drop is trusted:
    CTVOSFile::Delete (TVOSFile.cpp:101-111) is an unconditional removeObjectForKey for
    a WantsFile-eligible path and cannot leave the key behind.

    Hard-gated: a strict (0, 0, 0, 0) no-op unless `_is_tvos()` positively confirms
    Apple TV (and the NSUserDefaults plist resolves, which it only does there). Fully
    guarded, never raises. Returns (materialized, purged, kept, failed)."""
    materialized = purged = kept = failed = 0
    if not _is_tvos() or not userdata_root:
        return (0, 0, 0, 0)
    to_purge = []  # (rel, key) pairs safe to drop (any only-copy already on disk)
    try:
        _plist_path, store = _find_nsud_plist()
        if not store:
            return (0, 0, 0, 0)
        for key in sorted(k for k in store if isinstance(k, str)):
            if not key.startswith(_NSUD_KEY_PREFIX):
                continue  # bookkeeping keys like 'UserdataMigrated'
            rel = key[len(_NSUD_KEY_PREFIX) :].replace("\\", "/").lstrip("/")
            if not rel:
                continue
            base = os.path.basename(rel).lower()
            if not base.endswith(".xml") or base.startswith(
                "customcontroller.siriremote"
            ):
                # WantsFile excludes these paths, so xbmcvfs.delete would be a POSIX
                # delete of the real disk file. Keep the key, touch nothing.
                kept += 1
                continue
            if _should_vector(rel):
                kept += 1  # in-scope: the key is the live store - NEVER purge
                continue
            if _is_skin_menu_sidecar(rel):
                # The skin's own durability sidecar (skin.estuary7 1.0.66+
                # syncMenu dual-layers every skinshortcuts *.DATA.xml): a LIVE key
                # the skin maintains, not a stale-era leftover. Purging it only
                # makes the skin re-register it at the next Home load - keep it,
                # and leave both layers to the skin (it also re-materializes the
                # POSIX copy from the key after a Caches purge).
                kept += 1
                continue
            posix = os.path.join(userdata_root, *rel.split("/"))
            # isfile, not exists: a directory squatting on the rel is not a disk
            # twin, and treating it as one would let the key drop destroy the
            # only real copy (QA finding F2).
            if not os.path.isfile(posix):
                # The key is the ONLY copy. Materialize it to disk first, or keep it.
                data = _decode_key_value(store[key])
                if data is None:
                    failed += 1
                    if log:
                        log(
                            "nsud.purge_stale_keys: %s is key-only and undecodable "
                            "- key kept" % rel
                        )
                    continue
                try:
                    d = os.path.dirname(posix)
                    if d:
                        os.makedirs(d, exist_ok=True)
                    with open(posix, "wb") as fh:
                        fh.write(data)
                except OSError:
                    failed += 1
                    if log:
                        log(
                            "nsud.purge_stale_keys: could not materialize %s "
                            "- key kept" % rel
                        )
                    continue
                materialized += 1
            to_purge.append((rel, key))
        issued = []  # (rel, key) pairs whose delete call completed
        for rel, key in to_purge:
            try:
                # tvOS: drops ONLY the NSUserDefaults key; the POSIX file stands.
                xbmcvfs.delete(_special_for(rel))
                issued.append((rel, key))
            except Exception:
                failed += 1  # the drop was never issued; the key still shadows
        # xbmcvfs.delete's boolean lies (True even when nothing happened), and the
        # backing plist is cfprefsd-lazy (re-reading it right after the delete
        # shows the OLD snapshot - how every successful drop once counted as
        # failed and the run-once marker never set). Verify against the LIVE key
        # layer instead: every issued entry has a POSIX twin on disk (pre-existing
        # or just materialized), so a basename xbmcvfs.listdir reports TWICE
        # proves the key survived (TVOSDirectory does not dedupe) and anything
        # less means it is gone. With no listing observable at all, trust
        # CTVOSFile::Delete: for a WantsFile-eligible path (all of these, by the
        # guards above) it is an unconditional removeObjectForKey that cannot
        # leave the key behind (TVOSFile.cpp:101-111, TVOSNSUserDefaults.mm:188-202).
        listings = {}
        for rel, _key in issued:
            reldir, _sep, base = rel.rpartition("/")
            if reldir not in listings:
                listings[reldir] = _vfs_dir_names(reldir)
            names = listings[reldir]
            if names is not None and names.count(base) >= 2:
                failed += 1
            else:
                purged += 1
    except Exception:  # noqa: BLE001 - never abort the caller (counts stand as-is)
        pass
    if log:
        log(
            "nsud.purge_stale_keys: %d materialized, %d purged, %d kept, %d failed"
            % (materialized, purged, kept, failed)
        )
    return (materialized, purged, kept, failed)


# --------------------------------------------------------------------------- #
# IPTV stray-instance sweep (the 2026-07-08 duplicate-instance brick guard).
# Lives HERE - with the rest of the tvOS two-layer storage machinery, inside the
# hardware gate's fingerprint - and is called by wiz.restore() AFTER the extract.
# --------------------------------------------------------------------------- #
_IPTV_ADDON_DATA = "addon_data/pvr.iptvsimple/"
_INSTANCE_XML_RE = re.compile(r"^instance-settings-\d+\.xml$", re.IGNORECASE)
_PROFILE_IPTV_RE = re.compile(
    r"^(profiles/[^/]+/)addon_data/pvr\.iptvsimple/", re.IGNORECASE
)


def sweep_iptv_instances(userdata_root, archive_rels=None, log=None):
    """Delete the target's STRAY pvr.iptvsimple instance-settings-*.xml: files the
    ARCHIVE does not carry, under the profile prefixes where the archive DOES carry
    pvr.iptvsimple addon_data. Called after the extract (the archive's own instance
    files were just written in place), so a canceled restore never costs the box the
    config it already had. Enumerates BOTH layers - the POSIX listing AND (tvOS) the
    NSUserDefaults plist - because an instance key can exist with no disk file:
    exactly the residue that produced the duplicate numbering. Every key drop is
    VERIFIED against the LIVE key layer via the xbmcvfs.listdir dup-count
    (xbmcvfs.delete's boolean lies on tvOS, and re-reading the backing plist is
    worse: cfprefsd flushes it lazily, so a just-dropped key still shows in the
    snapshot and a SUCCESSFUL drop counts as failed - the same false-failure
    class the v2026.07.17.6 purge fix closed). A name still listed beyond its
    surviving POSIX file means the key survived and is counted FAILED, never
    removed. archive_rels are the archive's userdata-relative paths. Returns
    (removed, failed_list); never raises.
    """
    removed = 0
    failed = set()
    try:
        rels_in = [str(r).replace("\\", "/").lstrip("/") for r in (archive_rels or [])]
        prefixes = set()
        carried = set()
        for rel in rels_in:
            if rel.lower().startswith(_IPTV_ADDON_DATA):
                prefixes.add("")
            else:
                m = _PROFILE_IPTV_RE.match(rel)
                if not m:
                    continue
                prefixes.add(m.group(1))
            base = rel.rsplit("/", 1)[-1]
            if _INSTANCE_XML_RE.match(base):
                carried.add(rel.lower())
        if not prefixes:
            return (0, [])
        targets = set()
        for prefix in prefixes:
            posix_dir = os.path.join(
                userdata_root, *(prefix + _IPTV_ADDON_DATA).rstrip("/").split("/")
            )
            try:
                names = os.listdir(posix_dir)
            except OSError:
                names = []
            for nm in names:
                if _INSTANCE_XML_RE.match(nm):
                    targets.add(prefix + _IPTV_ADDON_DATA + nm)
        plist_path, store = _find_nsud_plist()
        if store:
            for key in store:
                if not (isinstance(key, str) and key.startswith(_NSUD_KEY_PREFIX)):
                    continue
                rel = key[len(_NSUD_KEY_PREFIX) :].replace("\\", "/").lstrip("/")
                base = rel.rsplit("/", 1)[-1]
                if not _INSTANCE_XML_RE.match(base):
                    continue
                for prefix in prefixes:
                    if rel == prefix + _IPTV_ADDON_DATA + base:
                        targets.add(rel)
                        break
        strays = sorted(t for t in targets if t.lower() not in carried)
        if not strays:
            return (0, [])
        for rel in strays:
            try:
                # tvOS: drops ONLY the NSUserDefaults key (verified below); on Fire
                # TV / desktop this IS the POSIX delete.
                xbmcvfs.delete(_special_for(rel))
            except Exception:
                pass  # the layer checks below count it as failed
            posix = os.path.join(userdata_root, *rel.split("/"))
            try:
                if os.path.exists(posix):
                    os.remove(posix)
            except OSError:
                pass
            if os.path.exists(posix):
                failed.add(rel)
        # xbmcvfs.delete's boolean lies (True even when nothing happened), and the
        # backing plist lags the live store (cfprefsd flushes lazily), so the KEY
        # layer is confirmed against the LIVE layer: xbmcvfs.listdir merges POSIX
        # names and key names with no dedupe (TVOSDirectory.cpp), so a swept name
        # listed MORE times than its surviving POSIX file accounts for means the
        # key survived - it will shadow the restore and counts as FAILED. When no
        # listing is observable the drop is trusted: CTVOSFile::Delete removes
        # the key unconditionally for any translatable path (TVOSFile.cpp:101-111).
        _dir_names = {}
        for rel in strays:
            reldir, base = rel.rsplit("/", 1)
            if reldir not in _dir_names:
                _dir_names[reldir] = _vfs_dir_names(reldir)
            names = _dir_names[reldir]
            if names is None:
                continue  # unobservable: trust the drop
            posix = os.path.join(userdata_root, *rel.split("/"))
            expected = 1 if os.path.isfile(posix) else 0
            if names.count(base) > expected:
                failed.add(rel)
        removed = len(strays) - len(failed)
        if log and (removed or failed):
            log(
                "nsud.sweep_iptv_instances: removed %d stray(s), failed %d (%s)"
                % (removed, len(failed), ", ".join(sorted(failed)[:5]) or "none")
            )
    except Exception:  # noqa: BLE001 - never abort the caller (restore reports counts)
        failed.add("sweep aborted")
    return (removed, sorted(failed))
