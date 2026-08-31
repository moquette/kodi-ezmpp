"""Settings Profile: load / plan / apply / verify for the one-command box setup.

One menu row applies the owner's standard settings to a running Kodi: core
settings (class A), file manager sources (class C), staged add-ons (class D)
and this add-on's own backup folder. Everything it applies is DATA in
``resources/profiles/house/``; no payload value lives in this file.

The design is ``docs/settings-profile-plan.md`` (signed off 2026-08-04) as
amended by the phase 0 measurements in
``docs/settings-profile-experiments-2026-08-30.md``. The rules that shaped the
code, so they are not re-derived:

* **Additive and idempotent.** Nothing is ever removed. A re-run reports
  ``already-correct`` per item, distinct from ``applied``, or the idempotency
  claim would be untestable (plan 2, 7.5).
* **Class A is live sets first, then ONE merged file write, then ONE vector.**
  A per-item write/persist loop is broken on tvOS: ``nsud.persist_one`` drops
  the POSIX copy after a confirmed vector, so every later
  ``write_guisetting`` returns a bare False (plan 4.1). The file half is
  re-materialized from the VFS first, never a stub.
* **Three payload ids are CONFIRM-GATED by Kodi itself** (measured 2026-08-30):
  ``addons.unknownsources`` (string 36618, fired post-commit from
  ``CAddonSystemSettings::OnSettingChanged``), ``services.webserver`` (36632)
  and ``services.esallinterfaces`` (36633), both pre-commit vetoes in
  ``CNetworkServices::OnSettingChanging``. A set of one of these blocks the
  calling thread in a modal yes/no with NO timeout. Measured: a dialog posted
  by a remote JSON-RPC transport is input-dead (and esallinterfaces over TCP
  deadlocks the TCP server against itself, SIGKILL territory); a dialog posted
  by an IN-PROCESS script thread is answerable by ordinary GUI input. So these
  ids are set from a worker thread while the main thread verifies the dialog
  is EXACTLY Kodi's own confirm (by localized text) and answers Yes, bounded.
  The user consented at the top of the flow; the dialog asks about the very
  thing he asked for. A miss cannot corrupt anything: unlike the
  ``lookandfeel.skin`` countdown there is no auto-revert-on-silence, and every
  outcome is read back and reported honestly (``refused`` / ``timeout``).
* **Class C is an additive MERGE through the VFS**, the deleted
  ``boxsetup.add_media_sources`` algorithm: dedupe on name AND path, same-URL
  consolidation, ``<default>`` stub insert, and BOTH the write and the vector
  gated behind ``if added or renamed`` so a no-op re-run mutates no storage
  layer (plan 4.3). Survival to the next boot was measured in all four E1 arms:
  Kodi 22 saves sources on modification only; shutdown clears, never saves.
* **Class D stages the official hub zips and enables through Kodi**
  (``Addons.SetAddonEnabled`` + a bounded ``GetAddonDetails`` poll), never
  ``InstallAddon`` (async, can prompt, cannot resolve before a repo fetch).
  Enabling works while ``addons.unknownsources`` is false (measured), so
  staging needs no precondition. The T7B repository enables LAST, because the
  hub advertises this add-on's own releases and Kodi is otherwise free to
  update EZ Maintenance++ while EZ Maintenance++ is mid-apply (plan 4.4).
* **This add-on's own settings go through ``setSetting()`` only** - it is
  already enabled and running, so a file write is the wrong half (plan 7.4
  step 2b).
* ``load()`` and ``plan()`` are PURE functions of the bundle directory and the
  injected device class / catalog: no Kodi reads inside, so the most valuable
  unit tests exist (plan 7.3). ``xbmcgui`` is never imported here; progress
  goes through the ``on_step`` callback.

Result vocabulary (plan 7.5), per ITEM because classes C and D have no ids:
``applied`` / ``already-correct`` / ``refused`` / ``unknown-id`` / ``timeout``
/ ``error``.
"""

import json
import os
import threading
import time
import xml.etree.ElementTree as ET
import zipfile

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.modules import _kodisettings, nsud

SCHEMA_VERSION = 1
DEVICE_CLASSES = ("fireos", "tvos", "bench")
OWN_ID = "script.ezmaintenanceplusplus"

# The three ids Kodi gates behind its own modal confirm, mapped to the core
# localized string id of the dialog TEXT (heading is 19098, "Warning"). The
# text match is the guard that we only ever answer KODI'S question for the id
# we just set, never some other dialog that happened to appear.
CONFIRM_GATED = {
    "addons.unknownsources": 36618,
    "services.webserver": 36632,
    "services.esallinterfaces": 36633,
}

# Outcomes (plan 7.5). `already-correct` being distinct from `applied` is what
# makes the idempotency claim testable at all.
APPLIED = "applied"
ALREADY = "already-correct"
REFUSED = "refused"
UNKNOWN = "unknown-id"
TIMEOUT = "timeout"
ERROR = "error"

_OK_OUTCOMES = (APPLIED, ALREADY)

# E3 measured the unknownsources confirm holding its thread 17.3 s (until
# answered); the answer loop polls every 300 ms, so 20 s is generous without
# letting a wedged set park the flow for a minute.
_CONFIRM_TIMEOUT_S = 20.0
_ENABLE_POLL_S = 10.0
_REFRESH_POLL_S = 10.0


class ProfileError(Exception):
    """Structural bundle validation failure. Validation failure applies
    NOTHING; there is no partial-bundle mode (plan 7.1)."""

    def __init__(self, problems):
        self.problems = [str(p) for p in problems]
        super(ProfileError, self).__init__("; ".join(self.problems))


# --------------------------------------------------------------------------- #
# Environment helpers (NOT used inside load/plan, which stay pure)
# --------------------------------------------------------------------------- #
def detect_device_class():
    """tvOS is System.Platform.TVOS (as nsud._is_tvos does), Fire TV is
    System.Platform.Android, and the bench is neither (plan 7.1)."""
    try:
        if xbmc.getCondVisibility("System.Platform.TVOS"):
            return "tvos"
        if xbmc.getCondVisibility("System.Platform.Android"):
            return "fireos"
    except Exception:
        pass
    return "bench"


def default_bundle_dir():
    """The shipped House bundle, inside the add-on directory - `tools/build.py`
    walks ADDON_DIR and nothing else, so a bundle anywhere else would ship a
    reader with no data (plan 7.1)."""
    base = xbmcaddon.Addon().getAddonInfo("path")
    return os.path.join(base, "resources", "profiles", "house")


# --------------------------------------------------------------------------- #
# load(): structural, offline validation. Parses, checks the rules, needs no
# running Kodi. Raises ProfileError with EVERY problem, not just the first.
# --------------------------------------------------------------------------- #
def load(bundle_dir, device_class, known_ids=None):
    """Return a validated bundle dict, or raise ProfileError.

    `device_class` is a PARAMETER, never read from the platform in here, so the
    overlay merge stays a pure function. `known_ids` is the authoring-time
    catalog gate (tests inject `tests/data/kodi22-setting-ids.txt`); the
    RUNTIME caller passes None, because a live catalog that moved under a
    validated bundle must produce a per-item `unknown-id`, not abort the whole
    apply (plan 7.1)."""
    problems = []
    if device_class not in DEVICE_CLASSES:
        raise ProfileError(
            [
                "unresolved device class %r (know %s); refusing to guess which "
                "overlay applies" % (device_class, ", ".join(DEVICE_CLASSES))
            ]
        )
    if not os.path.isdir(bundle_dir):
        raise ProfileError(["bundle directory missing: %s" % bundle_dir])

    meta = _load_meta(bundle_dir, problems)

    overlay_dir = os.path.join(bundle_dir, "overlays", device_class)
    if not os.path.isdir(overlay_dir):
        # Device-scoped leaves are overlay-only and absent from the base, so a
        # class with no overlay would silently inherit another class's backup
        # folder - an Apple TV writing into the Fire TV folder with nothing to
        # notice. Hard failure by design (plan 7.1).
        problems.append(
            "no overlay for device class %r (overlays/%s/ missing)"
            % (device_class, device_class)
        )

    class_a = _load_class_a(bundle_dir, overlay_dir, known_ids, problems)
    sources = _load_sources(os.path.join(bundle_dir, "sources.xml"), problems)
    addons = _load_addons(bundle_dir, problems)
    addon_data = _load_addon_data(bundle_dir, overlay_dir, problems)

    if problems:
        raise ProfileError(problems)
    return {
        "dir": bundle_dir,
        "name": meta.get("name", "Profile"),
        "bundle_version": meta.get("bundle_version", ""),
        "device_class": device_class,
        "class_a": class_a,
        "sources": sources,
        "addons": addons,
        "addon_data": addon_data,
    }


def _load_meta(bundle_dir, problems):
    path = os.path.join(bundle_dir, "profile.json")
    try:
        with open(path) as f:
            meta = json.load(f)
    except Exception as e:
        problems.append("profile.json unreadable: %s" % e)
        return {}
    if meta.get("schema_version") != SCHEMA_VERSION:
        problems.append(
            "profile.json schema_version %r != %d"
            % (meta.get("schema_version"), SCHEMA_VERSION)
        )
    for key in ("name", "bundle_version"):
        if not meta.get(key):
            problems.append("profile.json missing %r" % key)
    return meta


def _fragment_paths(bundle_dir, overlay_dir):
    """Base fragments in glob order, then overlay fragments in glob order.
    Glob order is precedence: a later occurrence of an id wins the VALUE."""
    out = []
    for d in (os.path.join(bundle_dir, "settings.d"),
              os.path.join(overlay_dir, "settings.d")):
        if os.path.isdir(d):
            out.extend(
                os.path.join(d, n) for n in sorted(os.listdir(d))
                if n.endswith(".xml")
            )
    return out


def _load_class_a(bundle_dir, overlay_dir, known_ids, problems):
    """Merge the fragments: value from the LAST occurrence, position from the
    FIRST. Precedence order and apply order are two axes - a naive last-wins
    dedupe would move an overlay-overridden `services.esenabled` AFTER
    `services.esallinterfaces` and silently break the parent-before-dependent
    rule (plan 7.1)."""
    order = []
    value = {}
    for path in _fragment_paths(bundle_dir, overlay_dir):
        short = os.path.basename(path)
        try:
            root = ET.parse(path).getroot()
        except Exception as e:
            problems.append("%s: parse failure: %s" % (short, e))
            continue
        if root.tag != "settings":
            problems.append("%s: root element is <%s>, not <settings>" % (short, root.tag))
            continue
        for node in root.iter("setting"):
            sid = node.get("id")
            if not sid:
                problems.append("%s: <setting> without an id" % short)
                continue
            if node.get("default") is not None:
                # default="true" marks a value as untouched and Kodi falls back
                # to its own default instead of using what is written.
                problems.append(
                    "%s: %s carries a default= attribute" % (short, sid)
                )
            if sid in _kodisettings._BOOT_STATE_ONLY:
                # Imported, never restated: two copies of this predicate
                # drifting is the failure that forced restorecheck to import
                # nsud._is_skin_menu_sidecar (plan 4.1). One carve-out, also
                # imported: _PROFILE_MAY_PIN allows filecache.memorysize, and
                # ONLY at the exact value every restore resets it to
                # (KODI_DEFAULT_CACHE_MB) - a bundle pinning any other number
                # would flip-flop against the restore reset forever, so it is
                # rejected here, per occurrence, overlays included.
                allowed = _kodisettings._PROFILE_MAY_PIN.get(sid)
                text = (node.text or "").strip()
                if allowed is None:
                    problems.append(
                        "%s: %s is in _kodisettings._BOOT_STATE_ONLY and must "
                        "never be applied" % (short, sid)
                    )
                elif text != allowed:
                    problems.append(
                        "%s: %s may only be pinned to %s (the value every "
                        "restore resets it to), got %r" % (short, sid, allowed, text)
                    )
            if known_ids is not None and sid not in known_ids:
                problems.append(
                    "%s: %s is not in the captured setting-id catalog" % (short, sid)
                )
            if sid not in value:
                order.append(sid)
            value[sid] = (node.text or "").strip()
    return [(sid, value[sid]) for sid in order]


def _load_sources(path, problems):
    """Class C carries source ENTRIES, never a document. The whole-document
    shape (section stubs, `<default>`) is REJECTED here so this file can never
    quietly become copyable - copying a full document onto a configured box
    deletes every source that box already had (plan 4.3)."""
    if not os.path.exists(path):
        return []
    try:
        root = ET.parse(path).getroot()
    except Exception as e:
        problems.append("sources.xml: parse failure: %s" % e)
        return []
    if root.tag != "sources":
        problems.append("sources.xml: root element is <%s>" % root.tag)
        return []
    entries = []
    for section in root:
        if section.tag != "files":
            problems.append(
                "sources.xml: carries a <%s> section; entries-only means "
                "<files> and nothing else" % section.tag
            )
            continue
        for node in section:
            if node.tag == "default":
                problems.append(
                    "sources.xml: carries a <default> stub - that is a whole "
                    "document, not entries"
                )
                continue
            if node.tag != "source":
                problems.append("sources.xml: unexpected <%s> in <files>" % node.tag)
                continue
            name = (node.findtext("name") or "").strip()
            spath = (node.findtext("path") or "").strip()
            if not name or not spath:
                problems.append("sources.xml: a source is missing name or path")
                continue
            if not spath.endswith("/"):
                # Kodi dedupes on the exact path string; .../Share and
                # .../Share/ are two different sources.
                problems.append(
                    "sources.xml: %s path %r lacks the trailing slash" % (name, spath)
                )
            if spath.startswith("nfs://"):
                host = spath[len("nfs://"):].split("/", 1)[0]
                if ":" in host:
                    # Kodi's own browse dialog hands back nfs://host:2049/...,
                    # which breaks directory listing and registers as a
                    # DIFFERENT source than the port-free form.
                    problems.append(
                        "sources.xml: %s path %r carries a port" % (name, spath)
                    )
            entries.append((name, spath))
    return entries


def _load_addons(bundle_dir, problems):
    path = os.path.join(bundle_dir, "addons.list")
    if not os.path.exists(path):
        return []
    out = []
    last_count = 0
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("|")
            if len(parts) != 3 or parts[2] not in ("normal", "last"):
                problems.append("addons.list: bad line %r" % line)
                continue
            aid, zrel, mode = parts
            zpath = os.path.join(bundle_dir, zrel)
            if not os.path.exists(zpath):
                problems.append("addons.list: %s zip missing: %s" % (aid, zrel))
                continue
            try:
                with zipfile.ZipFile(zpath) as z:
                    names = z.namelist()
            except Exception as e:
                problems.append("addons.list: %s zip unreadable: %s" % (aid, e))
                continue
            prefix = aid + "/"
            if not names or not all(n.startswith(prefix) for n in names):
                problems.append(
                    "addons.list: %s zip is not rooted at %s" % (aid, prefix)
                )
            if prefix + "addon.xml" not in names:
                problems.append("addons.list: %s zip carries no addon.xml" % aid)
            if mode == "last":
                last_count += 1
            out.append({"id": aid, "zip": zpath, "mode": mode})
    if last_count > 1:
        problems.append("addons.list: more than one enable=last entry")
    return out


def _load_addon_data(bundle_dir, overlay_dir, problems):
    """Base addon_data (device-neutral) merged with the overlay's
    (device-scoped, wins per id). Comment nodes are rejected outright:
    `CAddonSettings::Load` calls Attribute("id") on every child node without
    checking it is an element, and a comment is a SIGABRT on the first
    getSetting() - and under the apply order that crash would land INSIDE the
    flow, right after the add-on is enabled (plan 7.1)."""
    merged = {}
    for base in (os.path.join(bundle_dir, "addon_data"),
                 os.path.join(overlay_dir, "addon_data")):
        if not os.path.isdir(base):
            continue
        for aid in sorted(os.listdir(base)):
            adir = os.path.join(base, aid)
            if not os.path.isdir(adir):
                continue
            for fname in sorted(os.listdir(adir)):
                if not fname.endswith(".xml"):
                    continue
                fpath = os.path.join(adir, fname)
                try:
                    with open(fpath) as f:
                        raw = f.read()
                except Exception as e:
                    problems.append("addon_data %s/%s unreadable: %s" % (aid, fname, e))
                    continue
                if "<!--" in raw:
                    problems.append(
                        "addon_data %s/%s contains a comment node - a comment "
                        "in a Kodi-read settings.xml is a SIGABRT on the first "
                        "getSetting()" % (aid, fname)
                    )
                    continue
                try:
                    root = ET.fromstring(raw)
                except Exception as e:
                    problems.append("addon_data %s/%s: parse failure: %s" % (aid, fname, e))
                    continue
                pairs = []
                for node in root.iter("setting"):
                    sid = node.get("id")
                    if not sid:
                        problems.append(
                            "addon_data %s/%s: <setting> without an id" % (aid, fname)
                        )
                        continue
                    if node.get("default") is not None:
                        problems.append(
                            "addon_data %s/%s: %s carries a default= attribute"
                            % (aid, fname, sid)
                        )
                    pairs.append((sid, (node.text or "").strip()))
                doc = merged.setdefault(aid, {}).setdefault(
                    fname, {"pairs": [], "xml": raw}
                )
                # overlay wins per id, position from the first occurrence
                have = {s for s, _ in doc["pairs"]}
                doc["xml"] = raw
                for sid, text in pairs:
                    if sid in have:
                        doc["pairs"] = [
                            (s, text if s == sid else v) for s, v in doc["pairs"]
                        ]
                    else:
                        doc["pairs"].append((sid, text))
                        have.add(sid)
    return merged


# --------------------------------------------------------------------------- #
# plan(): the ordered operation list, performed by nothing (plan 7.3/7.4)
# --------------------------------------------------------------------------- #
def plan(bundle):
    """Plan order is 7.4's, with the E3-measured amendment that steps 3 and 4
    need no swap (enabling a staged add-on works while unknownsources is
    false):

      1. this add-on's own settings, `setSetting()` only
      2. third-party addon_data, written BEFORE the add-ons that own them are
         enabled (unexercised by the current payload, by design)
      3. class D staging, one UpdateLocalAddons, enablement (repo excluded)
      4. class A live sets in fragment order
      5. class A file half: re-materialize, ONE merged write, ONE persist_one
      6. class C source merge, write and vector gated on `if added or renamed`
      7. the T7B repository enable, LAST
    """
    ops = []
    own = bundle["addon_data"].get(OWN_ID, {})
    for fname in sorted(own):
        for sid, text in own[fname]["pairs"]:
            ops.append({"kind": "own-setting", "id": sid, "value": text})
    for aid in sorted(bundle["addon_data"]):
        if aid == OWN_ID:
            continue
        for fname in sorted(bundle["addon_data"][aid]):
            ops.append(
                {
                    "kind": "addon-data",
                    "addon": aid,
                    "rel": fname,
                    "xml": bundle["addon_data"][aid][fname]["xml"],
                }
            )
    normal = [a for a in bundle["addons"] if a["mode"] == "normal"]
    last = [a for a in bundle["addons"] if a["mode"] == "last"]
    for a in normal + last:
        ops.append({"kind": "stage", "addon": a["id"], "zip": a["zip"]})
    if normal or last:
        ops.append({"kind": "refresh", "addons": [a["id"] for a in normal + last]})
    for a in normal:
        ops.append({"kind": "enable", "addon": a["id"], "last": False})
    for sid, text in bundle["class_a"]:
        ops.append(
            {"kind": "set", "id": sid, "value": text, "confirm": sid in CONFIRM_GATED}
        )
    if bundle["class_a"]:
        ops.append({"kind": "write-guisettings", "values": list(bundle["class_a"])})
    if bundle["sources"]:
        ops.append({"kind": "sources", "entries": list(bundle["sources"])})
    for a in last:
        ops.append({"kind": "enable", "addon": a["id"], "last": True})
    return ops


# --------------------------------------------------------------------------- #
# JSON-RPC and coercion helpers
# --------------------------------------------------------------------------- #
def _rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(req)))


def _get_value(sid):
    """(True, value) or (False, None). Uses Settings.GetSettingValue, NOT the
    GetSettings catalog: system.playlistspath is absent from the catalog at
    every level yet fully readable and settable (measured 2026-08-30)."""
    try:
        resp = _rpc("Settings.GetSettingValue", {"setting": sid})
    except Exception:
        return False, None
    result = resp.get("result")
    if not isinstance(result, dict) or "value" not in result:
        return False, None
    return True, result["value"]


def coerce(text, current):
    """Coerce fragment TEXT to the python type of the live value. Raises
    ValueError when the text does not parse as that type."""
    if isinstance(current, bool):
        low = str(text).strip().lower()
        if low in ("true", "1", "yes", "on"):
            return True
        if low in ("false", "0", "no", "off"):
            return False
        raise ValueError("%r is not a boolean" % text)
    if isinstance(current, int):
        return int(str(text).strip())
    if isinstance(current, float):
        return float(str(text).strip())
    return "" if text is None else str(text)


def values_match(live, text):
    """True iff the live value equals the fragment text under coercion. Shared
    with the boot check so the two verdicts can never drift."""
    try:
        return live == coerce(text, live)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# apply(): performs the plan, returns per-item outcomes (plan 7.5)
# --------------------------------------------------------------------------- #
def apply(ops, on_step=None, log=None):
    """Perform the ops. `on_step(i, n, text)` drives whatever progress display
    the caller owns (profile.py never imports xbmcgui). Returns
    {"items": [...], "warnings": [...]}; a failure never aborts the flow and
    never leaves a step silently half-applied - it is recorded and surfaces in
    the result (plan 7.4)."""
    # LOGINFO, not the default LOGDEBUG: the per-item outcomes are the result
    # record's audit trail and must be readable from a normal kodi.log (the
    # first bench run logged them at DEBUG and they never reached the file).
    log = log or (
        lambda msg: xbmc.log(
            "ezmaintenanceplus: profile: %s" % msg, level=xbmc.LOGINFO
        )
    )
    ctx = {"items": [], "warnings": [], "log": log}
    n = len(ops)
    handlers = {
        "own-setting": _apply_own_setting,
        "addon-data": _apply_addon_data,
        "stage": _apply_stage,
        "refresh": _apply_refresh,
        "enable": _apply_enable,
        "set": _apply_set,
        "write-guisettings": _apply_write_guisettings,
        "sources": _apply_sources,
    }
    for i, op in enumerate(ops):
        label = _op_label(op)
        if on_step:
            try:
                on_step(i + 1, n, label)
            except Exception:
                pass
        try:
            outcome, detail = handlers[op["kind"]](op, ctx)
        except Exception as e:  # noqa: BLE001 - recorded, never aborts the box
            outcome, detail = ERROR, "%s: %s" % (type(e).__name__, e)
        ctx["items"].append(
            {"kind": op["kind"], "label": label, "outcome": outcome, "detail": detail}
        )
        log("%s -> %s%s" % (label, outcome, (" (%s)" % detail) if detail else ""))
    return {"items": ctx["items"], "warnings": ctx["warnings"]}


def _op_label(op):
    kind = op["kind"]
    if kind in ("set",):
        return op["id"]
    if kind == "own-setting":
        return "%s %s" % (OWN_ID, op["id"])
    if kind == "addon-data":
        return "addon_data %s/%s" % (op["addon"], op["rel"])
    if kind in ("stage", "enable"):
        return "%s %s" % (kind, op["addon"])
    if kind == "refresh":
        return "UpdateLocalAddons"
    if kind == "write-guisettings":
        return "guisettings.xml file half"
    if kind == "sources":
        return "file manager sources"
    return kind


def _apply_own_setting(op, ctx):
    """setSetting() updates the live store and the file together; EZ
    Maintenance++ is already enabled and running, so 'write the file before
    enablement' is meaningless for it and a raw write is wrong (plan 7.4 2b)."""
    addon = xbmcaddon.Addon()
    sid, text = op["id"], op["value"]
    if addon.getSetting(sid) == text:
        return ALREADY, ""
    addon.setSetting(sid, text)
    if addon.getSetting(sid) == text:
        return APPLIED, ""
    return REFUSED, "setSetting read-back mismatch"


def _apply_addon_data(op, ctx):
    """Write a third-party add-on's settings file BEFORE that add-on is ever
    enabled. If it is ALREADY enabled, Kodi holds its settings in memory and
    flushes them over the file at the clean shutdown the flow's restart
    triggers, so silently writing anyway would be restore defect A with a new
    owner. The bounded disable/re-enable alternative is owner-gated (plan open
    item 6), so until he sanctions it this leaf reports honestly instead."""
    aid = op["addon"]
    try:
        resp = _rpc(
            "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
        )
        enabled = bool(resp.get("result", {}).get("addon", {}).get("enabled"))
    except Exception:
        enabled = False
    if enabled:
        return REFUSED, (
            "%s is already enabled; its live settings would flush over this "
            "file at shutdown (owner-gated: plan open item 6)" % aid
        )
    rel = "addon_data/%s/%s" % (aid, op["rel"])
    target = xbmcvfs.translatePath("special://profile/" + rel)
    d = os.path.dirname(target)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(target, "w") as f:
        f.write(op["xml"])
    persisted = nsud.persist_one(rel, log=ctx["log"])
    if not persisted and _is_tvos():
        ctx["warnings"].append("%s: tvOS vector unconfirmed" % rel)
    return APPLIED, ""


def _addons_home():
    return xbmcvfs.translatePath("special://home/addons/")


def _apply_stage(op, ctx):
    """Stage the official zip into special://home/addons. An add-on Kodi
    already knows is left alone - updates are the repository's job, and
    overwriting a newer installed copy with the bundled one would be a
    downgrade wearing an install costume."""
    aid = op["addon"]
    try:
        resp = _rpc(
            "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
        )
        if isinstance(resp.get("result"), dict):
            return ALREADY, "already installed"
    except Exception:
        pass
    if os.path.isdir(os.path.join(_addons_home(), aid)):
        return ALREADY, "already on disk"
    with zipfile.ZipFile(op["zip"]) as z:
        z.extractall(_addons_home())
    return APPLIED, ""


def _apply_refresh(op, ctx):
    """UpdateLocalAddons, then a bounded poll until Kodi's own view lists every
    staged id - an immediate GetAddonDetails races the scan. When Kodi already
    knows every id (the idempotent re-run), the scan is skipped and the op
    reports already-correct, keeping the re-run free of side effects."""
    want = list(op.get("addons", ()))
    known = []
    for aid in want:
        try:
            resp = _rpc(
                "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
            )
            if isinstance(resp.get("result"), dict):
                known.append(aid)
        except Exception:
            pass
    if len(known) == len(want):
        return ALREADY, ""
    xbmc.executebuiltin("UpdateLocalAddons")
    deadline = time.time() + _REFRESH_POLL_S
    missing = list(want)
    while time.time() < deadline:
        missing = []
        for aid in want:
            try:
                resp = _rpc(
                    "Addons.GetAddonDetails",
                    {"addonid": aid, "properties": ["enabled"]},
                )
                if not isinstance(resp.get("result"), dict):
                    missing.append(aid)
            except Exception:
                missing.append(aid)
        if not missing:
            return APPLIED, ""
        xbmc.sleep(500)
    return TIMEOUT, "not scanned in: %s" % ", ".join(missing)


def _apply_enable(op, ctx):
    """Addons.SetAddonEnabled plus a bounded poll on Addons.GetAddonDetails -
    Kodi's own view, not the state we wrote. Works while unknownsources is
    false (measured 2026-08-30)."""
    aid = op["addon"]
    try:
        resp = _rpc(
            "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
        )
        addon = resp.get("result", {}).get("addon") if isinstance(resp.get("result"), dict) else None
    except Exception:
        addon = None
    if addon is None:
        return REFUSED, "%s is not known to Kodi" % aid
    if addon.get("enabled"):
        return ALREADY, ""
    _rpc("Addons.SetAddonEnabled", {"addonid": aid, "enabled": True})
    deadline = time.time() + _ENABLE_POLL_S
    while time.time() < deadline:
        try:
            resp = _rpc(
                "Addons.GetAddonDetails", {"addonid": aid, "properties": ["enabled"]}
            )
            if resp.get("result", {}).get("addon", {}).get("enabled"):
                return APPLIED, ""
        except Exception:
            pass
        xbmc.sleep(300)
    return TIMEOUT, "enable not confirmed within %ds" % int(_ENABLE_POLL_S)


def _apply_set(op, ctx):
    sid, text = op["id"], op["value"]
    ok, cur = _get_value(sid)
    if not ok:
        # Reachable at runtime by design: the authoring gate catches unknown
        # ids in CI, and a live catalog that has moved under a validated bundle
        # reports per-item rather than aborting the apply (plan 7.1).
        return UNKNOWN, ""
    try:
        want = coerce(text, cur)
    except (TypeError, ValueError) as e:
        return ERROR, "cannot coerce %r: %s" % (text, e)
    if cur == want:
        return ALREADY, ""
    return _bounded_set(sid, want, ctx["log"])


def _bounded_set(sid, want, log):
    """Every class A set runs bounded, through a worker thread, with the main
    thread watching for a modal - because ANY in-process set can block forever
    behind one, and the first full bench run proved it (2026-08-30): the
    original order enabled the web server before its password existed, Kodi
    posted its invalid-config OK dialog (string 36635) from OnSettingChanging,
    and the flow ate a 20 s timeout plus two cascade refusals while the dialog
    sat unanswered on screen. "Either every set carries a bound, or class A
    does not ship as a loop" was the plan's own condition (7.5); this is the
    bound.

    Three modal cases, all measured:
    - KODI'S OWN CONFIRM for a CONFIRM_GATED id, verified by localized text:
      answered YES (the user consented at the flow's one confirm; the dialog
      asks about the very thing he asked for). Measured answer path: walk
      focus to the Yes button (11), then select; there is no countdown, so a
      missed step retries on the next poll and a miss cannot corrupt anything.
    - AN OK DIALOG (a veto explanation, e.g. invalid web server config): its
      text is captured as the refusal reason and it is closed, letting the
      set return false. Outcome: refused, with Kodi's own words as detail.
    - ANY OTHER YES/NO during our in-flight set: closed unanswered (Kodi
      treats a closed confirm as decline; pre-commit ids keep their old value
      and unknownsources reverts itself). Outcome from the read-back.
    """
    box = {}

    def worker():
        try:
            box["resp"] = _rpc(
                "Settings.SetSettingValue", {"setting": sid, "value": want}
            )
        except Exception as e:  # noqa: BLE001 - reported via the outcome
            box["err"] = "%s: %s" % (type(e).__name__, e)

    expected = ""
    if sid in CONFIRM_GATED:
        try:
            expected = (xbmc.getLocalizedString(CONFIRM_GATED[sid]) or "").strip()
        except Exception:
            pass
    t = threading.Thread(target=worker)
    t.daemon = True
    t.start()
    t.join(0.05)  # the overwhelmingly common case: no dialog, instant return
    refusal_text = ""
    deadline = time.time() + _CONFIRM_TIMEOUT_S
    while t.is_alive() and time.time() < deadline:
        try:
            if xbmc.getCondVisibility("Window.IsActive(yesnodialog)"):
                shown = (xbmc.getInfoLabel("Control.GetLabel(9)") or "").strip()
                if expected and shown == expected:
                    if xbmc.getInfoLabel("System.CurrentControlId") == "11":
                        xbmc.executebuiltin("Action(select)")
                    else:
                        xbmc.executebuiltin("Action(right)")
                else:
                    refusal_text = refusal_text or shown
                    xbmc.executebuiltin("Dialog.Close(yesnodialog,true)")
            elif xbmc.getCondVisibility("Window.IsActive(okdialog)"):
                refusal_text = (
                    refusal_text
                    or (xbmc.getInfoLabel("Control.GetLabel(9)") or "").strip()
                )
                xbmc.executebuiltin("Dialog.Close(okdialog,true)")
        except Exception:
            pass
        xbmc.sleep(300)
    if t.is_alive():
        # Leave nothing modal behind for the rest of the flow to trip on.
        for close in ("Dialog.Close(yesnodialog,true)", "Dialog.Close(okdialog,true)"):
            try:
                xbmc.executebuiltin(close)
            except Exception:
                pass
        t.join(2.0)
        if t.is_alive():
            log("bounded set of %s never returned" % sid)
            return TIMEOUT, "set blocked past %ds" % int(_CONFIRM_TIMEOUT_S)
    ok, now = _get_value(sid)
    if ok and now == want:
        return APPLIED, ""
    err = box.get("err")
    if err:
        return ERROR, err
    if refusal_text:
        return REFUSED, "Kodi refused: %s" % refusal_text
    resp = box.get("resp") or {}
    if resp.get("result") is not True:
        return REFUSED, "SetSettingValue returned %r" % (
            resp.get("error") or resp.get("result"),
        )
    return REFUSED, "read-back holds %r" % (now,)


def _is_tvos():
    try:
        return bool(xbmc.getCondVisibility("System.Platform.TVOS"))
    except Exception:
        return False


def _apply_write_guisettings(op, ctx):
    """The class A file half: re-materialize from the VFS (NEVER a stub, which
    would wipe every other setting), one merged write, ONE persist_one. This is
    the wiz._apply_boot_skin shape, and it is belt-and-braces for the unclean
    kill: on Piers a JSON-RPC set triggers no Kodi save, so live values are
    memory-only until the clean-close flush (measured 2026-08-30).

    Skipped entirely when no class A set actually APPLIED this run: on a
    changed-nothing re-run a rewrite would still be a key rewrite plus a POSIX
    drop on tvOS - a real storage mutation on a run that changed nothing, the
    same argument 4.3 makes for sources."""
    if not any(
        it["kind"] == "set" and it["outcome"] == APPLIED for it in ctx["items"]
    ):
        return ALREADY, "no live set changed anything; file half untouched"
    path = xbmcvfs.translatePath("special://profile/guisettings.xml")
    if not os.path.exists(path):
        # tvOS after a vector: the POSIX copy was deliberately dropped, and
        # write_guisetting returns False on a missing file. Re-materialize
        # through the VFS, which on tvOS reads the NSUserDefaults key.
        try:
            f = xbmcvfs.File("special://profile/guisettings.xml")
            try:
                data = f.readBytes()
            finally:
                f.close()
            data = bytes(data) if data else b""
            if data:
                with open(path, "wb") as fh:
                    fh.write(data)
                ctx["log"]("re-materialized guisettings.xml from the VFS")
        except Exception as e:  # noqa: BLE001 - write_guisetting reports below
            ctx["log"]("could not re-materialize guisettings.xml (%s)" % e)
    wrote = 0
    for sid, text in op["values"]:
        if _kodisettings.write_guisetting(path, sid, text):
            wrote += 1
    vectored = nsud.persist_one("guisettings.xml", log=ctx["log"])
    if not vectored and _is_tvos():
        # tvOS ONLY: the POSIX copy stands but NSUserDefaults - the layer Kodi
        # actually reads there - was not proven to hold these bytes. Off tvOS
        # a False here is not that failure mode (plan 7.6, the
        # _apply_boot_skin precedent).
        ctx["warnings"].append("guisettings.xml: tvOS vector unconfirmed")
    if wrote == len(op["values"]):
        return APPLIED, "%d value(s) reinforced on disk" % wrote
    return ERROR, (
        "file half wrote %d of %d (live sets stand; the clean-close flush "
        "covers them)" % (wrote, len(op["values"]))
    )


def _apply_sources(op, ctx):
    """The additive class C merge, ported from the deleted
    boxsetup.add_media_sources (e52d170^) rather than re-derived. Its
    load-bearing properties: read through xbmcvfs (a plain read on tvOS can see
    a stale or dropped disk copy and make the merge clobber existing sources),
    dedupe on name AND path, consolidate same-URL duplicates (audit Finding G),
    insert the <default> stub when <files> lacks one, and gate BOTH the write
    and the vector behind `if added or renamed` - without that guard a
    changed-nothing re-run is still a key rewrite plus a POSIX drop on tvOS,
    a real storage mutation, and the idempotency claim would be false."""
    raw = b""
    try:
        f = xbmcvfs.File("special://profile/sources.xml")
        try:
            raw = bytes(f.readBytes() or b"")
        finally:
            f.close()
    except Exception:
        raw = b""
    root = None
    if raw:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            root = None
    if root is None or root.tag != "sources":
        root = ET.Element("sources")
    files = root.find("files")
    if files is None:
        files = ET.SubElement(root, "files")
    if files.find("default") is None:
        files.insert(0, ET.Element("default"))
    renamed = 0
    for name, spath in op["entries"]:
        same_url = [
            s
            for s in files.findall("source")
            if (s.findtext("path") or "").strip() == spath
        ]
        if not same_url:
            continue
        keep = same_url[0]
        nm = keep.find("name")
        if nm is None:
            nm = ET.SubElement(keep, "name")
        if (nm.text or "").strip() != name:
            nm.text = name
            renamed += 1
        for extra in same_url[1:]:
            files.remove(extra)
            renamed += 1
    have_names = {(s.findtext("name") or "").strip() for s in files.findall("source")}
    have_paths = {(s.findtext("path") or "").strip() for s in files.findall("source")}
    added = 0
    for name, spath in op["entries"]:
        if name in have_names or spath in have_paths:
            continue
        src = ET.SubElement(files, "source")
        ET.SubElement(src, "name").text = name
        pnode = ET.SubElement(src, "path")
        pnode.set("pathversion", "1")
        pnode.text = spath
        ET.SubElement(src, "allowsharing").text = "true"
        have_names.add(name)
        have_paths.add(spath)
        added += 1
    if added or renamed:
        xml_path = xbmcvfs.translatePath("special://profile/sources.xml")
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(ET.tostring(root, encoding="unicode"))
        persisted = nsud.persist_one("sources.xml", log=ctx["log"])
        if not persisted and _is_tvos():
            ctx["warnings"].append("sources.xml: tvOS vector unconfirmed")
        return APPLIED, "%d added, %d consolidated; live after the restart" % (
            added,
            renamed,
        )
    return ALREADY, ""


# --------------------------------------------------------------------------- #
# verify(): re-read live state (plan 7.6)
# --------------------------------------------------------------------------- #
def verify(ops):
    """The in-flow verification pass: class A read back through
    Settings.GetSettingValue against the sent value, class D through
    Addons.GetAddonDetails. Class C is deliberately ABSENT: Files.GetSources
    returns the in-memory list populated at startup, so a perfectly correct
    write reads back as missing and would emit a false PARTIAL - the
    Skin.HasSetting species of probe. Its in-flow check is persist_one's
    return value (already consumed in apply); the live confirmation belongs to
    the boot check (plan 7.6/7.7)."""
    out = []
    for op in ops:
        if op["kind"] == "set":
            ok, cur = _get_value(op["id"])
            if not ok:
                outcome, detail = UNKNOWN, ""
            elif values_match(cur, op["value"]):
                outcome, detail = APPLIED, ""
            else:
                outcome, detail = REFUSED, "live value is %r" % (cur,)
            out.append({"kind": "verify-set", "label": op["id"], "outcome": outcome,
                        "detail": detail})
        elif op["kind"] == "enable":
            try:
                resp = _rpc(
                    "Addons.GetAddonDetails",
                    {"addonid": op["addon"], "properties": ["enabled"]},
                )
                enabled = bool(resp.get("result", {}).get("addon", {}).get("enabled"))
            except Exception:
                enabled = False
            out.append(
                {
                    "kind": "verify-enable",
                    "label": op["addon"],
                    "outcome": APPLIED if enabled else REFUSED,
                    "detail": "" if enabled else "not enabled",
                }
            )
    return out


def summarize(items):
    """(ok_count, total, failures) where failures is the list of items whose
    outcome is not applied/already-correct. A partial result is reported as
    partial, never as complete (plan 6.2)."""
    failures = [it for it in items if it["outcome"] not in _OK_OUTCOMES]
    return len(items) - len(failures), len(items), failures
