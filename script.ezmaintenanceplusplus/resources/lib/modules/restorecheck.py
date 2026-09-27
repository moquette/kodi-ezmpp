# -*- coding: utf-8 -*-
"""Post-restore verification: prove the restored state before anyone reports it.

Born 2026-07-17 from the atv2 round-trip: the restore surfaced a raw mid-flight
fear ("the wipe could not remove 9 items; leftovers may shadow") and left the
owner to do the triage by hand - which proved every leftover harmless. The
pipeline itself must do that proving, then report the verdict, not the anxiety
(the same honesty rule as the manifest work, applied in both directions: never
report unproven success, and never report unproven danger).

Everything here is READ-ONLY inspection of the box plus pure functions over the
archive listing. It deliberately lives OUTSIDE the storage-contract modules
(nsud/nsub/onetap/wiz/boxsetup write or wipe; this module only looks).

Triage classes for a wipe leftover, in order of decreasing comfort:
  overwritten - the archive carries this path, so the extract (file layer) or
                the durability rewrite (key layer) replaced it. Harmless by
                construction.
  residue     - a file the archive does not carry that the wipe could not
                remove. It stays as the LIVE copy Kodi reads (a file shadows
                nothing on any OS, but it is not inert either) - yet it cannot
                override anything the RESTORE placed, because every in-archive
                path was just overwritten by the extract. So it is disk
                residue outside the restored set, not a threat to it: logged,
                never alarmed (owner spec). A wipe that leaves real residue is
                worth watching, hence the count in the log.
  shadow      - a tvOS NSUserDefaults key the archive does not carry. A key
                SHADOWS its disk twin (apple-tv SKILL.md section 8), so a surviving stale
                key is the ONLY leftover class that can override restored
                state. These are the auto-fix targets; any that survive the
                fix are what "needs attention" means.
"""

import xbmc
import xbmcvfs

# The key-bearing directories a restore has to leave clean. (These were also the
# spots the release-gate verification probed, until that gate was deleted on
# 2026-07-21; this is now the only check, not the twin of one.)
DUPLICATE_PROBE_DIRS = (
    "special://profile/addon_data/",
    "special://profile/addon_data/pvr.iptvsimple/",
    "special://profile/addon_data/script.skinshortcuts/",
)


def _log(msg):
    xbmc.log("EZ Maintenance++ : verify: %s" % msg, level=xbmc.LOGINFO)


def _norm(rel):
    return (rel or "").replace("\\", "/").lstrip("/")


def archive_rels(names, anchor):
    """The archive's members as home-relative forward-slash paths.

    A home-anchored zip already stores home-relative members; a
    userdata-anchored zip stores bare userdata contents, so its members map to
    userdata/<member> in home terms (mirrors wiz's extract-root decision).
    """
    out = set()
    for n in names or []:
        rel = _norm(n)
        if not rel or rel.endswith("/"):
            continue
        out.add(rel if anchor == "home" else "userdata/" + rel)
    return out


def triage_leftovers(leftovers, names, anchor):
    """Classify wipe leftovers against the archive.

    `leftovers` is [(kind, home_rel), ...] with kind "file" or "key" (a key's
    home_rel includes the userdata/ prefix, matching the plist key naming).
    Returns {"overwritten": [...], "residue": [...], "shadow": [...]} of
    home-relative paths. Pure function; no I/O.
    """
    arc = archive_rels(names, anchor)
    out = {"overwritten": [], "residue": [], "shadow": []}
    for kind, rel in leftovers or []:
        rel = _norm(rel)
        if not rel:
            continue
        if rel in arc:
            out["overwritten"].append(rel)
        elif kind == "key":
            out["shadow"].append(rel)
        else:
            out["residue"].append(rel)
    return out


def surviving_shadow_keys(shadow_rels):
    """Which of the triaged shadow keys still exist AFTER the restore's own
    purge + rewrite ran. Reads the NSUserDefaults plist via the proven onetap
    enumeration; [] on every non-tvOS platform (there is no key layer).
    `shadow_rels` are home-relative (userdata/...)."""
    if not shadow_rels:
        return []
    try:
        from resources.lib.modules import onetap

        live = {"userdata/" + _norm(r) for r in onetap._nsud_userdata_rels()}
    except Exception as e:
        _log("key re-read failed (%s); reporting all candidates" % type(e).__name__)
        return sorted(_norm(r) for r in shadow_rels)
    return sorted(r for r in (_norm(x) for x in shadow_rels) if r in live)


def _sidecar_predicate():
    """nsud's ``_is_skin_menu_sidecar``, or None when it cannot be imported.

    IMPORTED, never re-derived: nsud.purge_stale_keys DELIBERATELY KEEPS the
    skin's dual-layer menu keys, so if this module carried its own copy of that
    rule the two would drift and the detector would alarm about exactly the keys
    the purge is designed to preserve - which is the defect this function had
    (atv2, 23 false hits, 2026-07-19). One definition, one behaviour."""
    try:
        from resources.lib.modules import nsud

        return nsud._is_skin_menu_sidecar
    except Exception as e:
        _log(
            "sidecar predicate unavailable (%s); reporting every duplicate"
            % type(e).__name__
        )
        return None


def _probe_rel(probe_dir, name):
    """A probe dir + basename -> the userdata-relative path nsud's key naming
    uses (``addon_data/<id>/<name>``), so the sidecar predicate sees the same
    shape it sees for an NSUserDefaults key."""
    return _norm(probe_dir.split("special://profile/", 1)[-1]) + _norm(name)


def duplicate_listing_hits(dirs=DUPLICATE_PROBE_DIRS):
    """Names listed more than once in the key-bearing directories - the
    two-layer divergence signature the release gate probes over JSON-RPC.
    Uses the same VFS layer Kodi itself reads. [] when clean or unreadable.

    Skin-menu sidecars are NOT hits. skin.estuary7 1.0.66+ deliberately
    dual-layers every skinshortcuts ``*.DATA.xml`` on tvOS (its syncMenu
    re-registers a byte-identical key on every Home load, so the owner's custom
    menu survives a Library/Caches purge), and CTVOSDirectory::GetDirectory does
    not dedupe - so a healthy Apple TV lists each of them TWICE by design. That
    is not divergence, it is the durability mechanism working, and treating it
    as a finding made every tvOS restore end with a "needs attention" toast the
    user could do nothing about. The exclusion is exactly the ``*.DATA.xml``
    sidecar pattern, borrowed from nsud so the two modules cannot disagree:
    ``script.skinshortcuts/settings.xml`` and every non-sidecar duplicate STILL
    hit, because a genuine stale key shadowing a restored file is the one thing
    this probe exists to catch."""
    is_sidecar = _sidecar_predicate()
    hits = []
    for d in dirs:
        try:
            dirs_l, files_l = xbmcvfs.listdir(d)
        except Exception:
            continue
        seen, dup = set(), set()
        for name in list(dirs_l) + list(files_l):
            if name in seen:
                dup.add(name)
            seen.add(name)
        for name in sorted(dup):
            if is_sidecar is not None and is_sidecar(_probe_rel(d, name)):
                continue
            hits.append(d + name)
    return hits


def verify_restored_state(leftovers, names, anchor):
    """The full post-restore verification. Returns (attention, detail_lines):
    `attention` is the list of proven-dangerous findings (empty = verified
    clean); `detail_lines` is the complete triage for the log. Never raises."""
    detail = []
    attention = []
    try:
        tri = triage_leftovers(leftovers, names, anchor)
        if tri["overwritten"]:
            detail.append(
                "%d wipe leftover(s) overwritten by the archive (harmless): %s"
                % (len(tri["overwritten"]), ", ".join(sorted(tri["overwritten"])[:10]))
            )
        if tri["residue"]:
            detail.append(
                "%d wipe leftover file(s) not in the archive (disk residue "
                "outside the restored set; cannot override restored paths): %s"
                % (len(tri["residue"]), ", ".join(sorted(tri["residue"])[:10]))
            )
        survivors = surviving_shadow_keys(tri["shadow"])
        cleared = sorted(set(_norm(r) for r in tri["shadow"]) - set(survivors))
        if cleared:
            detail.append(
                "%d stale key(s) cleared by the restore's purge (auto-fixed): %s"
                % (len(cleared), ", ".join(cleared[:10]))
            )
        if survivors:
            # A surviving key is stale-key CRUFT, NOT a restore problem: it is a
            # path the ARCHIVE does not carry (triage already sorted the in-archive
            # ones into "overwritten"), so there is no restored file at that path for
            # it to shadow. It could only shadow a FUTURE write - latent, not a
            # current failure - so it is LOGGED, never alarmed. The genuine "a
            # RESTORED file is being shadowed" signal is a duplicate two-layer
            # listing (same path in both layers), checked below; THAT is the
            # attention case. Flagging bare survivors as attention cried wolf on a
            # benign wipe leftover (atv2 2026-07-17 false "needs attention").
            detail.append(
                "%d stale NSUserDefaults key(s) the wipe could not clear (cruft, not "
                "in the archive, shadow nothing restored): %s"
                % (len(survivors), ", ".join(survivors[:10]))
            )
        dups = duplicate_listing_hits()
        if dups:
            attention.append(
                "%d restored file(s) shadowed by a stale key (duplicate two-layer "
                "listing): %s" % (len(dups), ", ".join(dups[:10]))
            )
    except Exception as e:  # verification must never break a restore
        # A diagnostic that could not run is NOT a restore failure - the extract
        # already decided that. Log it; do NOT alarm the user about their (likely
        # fine) restore because a probe hiccuped.
        detail.append("verification could not complete (%s)" % type(e).__name__)
    for line in detail:
        _log(line)
    for line in attention:
        _log("ATTENTION: " + line)
    if not attention:
        _log("restored state verified clean")
    return attention, detail
