"""PVR share settings: keep pvr.iptvsimple's instance settings equal to the
templates the IPTV builder publishes on the mini's share.

The builder writes ``instance-settings-N.xml`` to
``nfs://<SHARE_HOST>/Users/moquette/Kodi/Share/iptv/`` and the bootstrap
copied them onto a box ONCE. Every later change to a template (a new lineup,
a moved EPG, the 2026-09-26 host move) was a hand apply per box. This step
kills that: it lists the share's iptv directory over the VFS, compares each
template with the box's copy under ``addon_data/pvr.iptvsimple/`` and, when
they differ, writes the share's copy atomically, then makes pvr.iptvsimple
reload once at the end if anything changed. It runs at boot from service.py
(deferred to the next maintenance tick while something is playing) and as a
step of Apply Settings Profile.

Guards, each a rule and not a preference:

* The share unreachable, or listing no instance files, touches NOTHING on
  the box: log and skip. An empty listing and a dead mount are the same
  case from here (xbmcvfs.listdir answers ``([], [])`` for both), and the
  box's copy is the only copy of a working lineup, so nothing is ever
  removed on the strength of a directory that failed to list.
* A template that does not parse as an XML ``<settings>`` document is never
  written. The builder's half-written file, a truncated transfer, an NFS
  read that returned garbage: all skip with a log line.
* Never while something is playing. A reload tears the live channel down,
  so a boot run that lands mid-playback defers to the service's next tick,
  and a profile run reports the deferral.
* The template's legacy-host URLs are rewritten to SHARE_HOST before the
  compare (``sharehost.migrate``), so a template the builder has not yet
  moved still lands the box on the tailnet path, and the moment the builder
  moves it the compare is a no-op.
* The reload is Kodi's own disable/enable of the client
  (``Addons.SetAddonEnabled``), the mechanism this add-on already uses for
  the restore-scoped PVR pause and the AutoCompletion rescan; Kodi 22
  exposes no PVR manager restart builtin or JSON-RPC method (the
  StartPVRManager/StopPVRManager builtins went with Kodi 17). Whether the
  client re-reads its instance files on the enable is proven per release on
  the office Fire TV (see this repo's CLAUDE.md, the share host section).
  The toggle is bracketed by the restore's pause marker, so a crash between
  the two calls is healed by ``service._maybe_resume_paused_pvr`` at the
  next boot, and it only ever runs when the client is ALREADY enabled: a
  client the owner switched off stays off.
"""

import json
import os
import re
import stat
import time
import xml.etree.ElementTree as ET

import xbmc
import xbmcvfs

from resources.lib.modules import nsud, sharehost, tailnet

PVR_ADDON_ID = "pvr.iptvsimple"

# The same shape nsud._INSTANCE_XML_RE and the restore sweep use.
_INSTANCE_RE = re.compile(r"^instance-settings-\d+\.xml$", re.IGNORECASE)

# Outcomes, the profile's vocabulary plus `skipped` (a guard fired; nothing
# was wrong and nothing was touched).
APPLIED = "applied"
ALREADY = "already-correct"
SKIPPED = "skipped"
ERROR = "error"

_RELOAD_POLL_S = 10.0


def _default_log(msg):
    xbmc.log("ezmaintenanceplus: PVR share settings: %s" % msg, level=xbmc.LOGINFO)


def _rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(req)))


def is_playing():
    try:
        return bool(xbmc.Player().isPlaying())
    except Exception:
        return False


def list_share_templates():
    """The share's instance-settings file names, sorted, or [] when the share
    is unreachable or carries none. Never raises."""
    try:
        _dirs, files = xbmcvfs.listdir(sharehost.IPTV_URL)
    except Exception:
        return []
    return sorted(f for f in (files or []) if _INSTANCE_RE.match(f))


def read_share_template(name):
    """Bytes of one template read over the VFS, b'' on any failure."""
    try:
        f = xbmcvfs.File(sharehost.IPTV_URL + name)
        try:
            data = f.readBytes()
        finally:
            f.close()
        return bytes(data) if data else b""
    except Exception:
        return b""


def _read_box_copy(rel):
    """The box's copy, read through the VFS (on tvOS that is the layer Kodi
    reads). b'' when absent or unreadable."""
    try:
        f = xbmcvfs.File("special://profile/" + rel)
        try:
            data = f.readBytes()
        finally:
            f.close()
        return bytes(data) if data else b""
    except Exception:
        return b""


def prepare_template(raw):
    """(bytes_to_write, migrated_count) for a template, or (None, reason)
    when it must not be written: not utf-8, not XML, not a <settings>
    document. The legacy-host rewrite happens BEFORE the parse so the bytes
    validated are the bytes written."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        return None, "not utf-8: %s" % e
    text, migrated = sharehost.migrate(text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        return None, "does not parse as XML: %s" % e
    if root.tag != "settings":
        return None, "root element is <%s>, not <settings>" % root.tag
    return text.encode("utf-8"), migrated


def _write_box_copy(rel, data, log):
    """Atomic replace of the box's copy, keeping the mode bits of the file it
    replaces, then the one sanctioned persist (nsud.persist_one) so both tvOS
    layers agree. Returns True iff the bytes are on the box."""
    target = xbmcvfs.translatePath("special://profile/" + rel)
    d = os.path.dirname(target)
    if not os.path.isdir(d):
        os.makedirs(d)
    mode = None
    try:
        mode = stat.S_IMODE(os.stat(target).st_mode)
    except OSError:
        pass
    tmp = target + ".ezm-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    if mode is not None:
        try:
            os.chmod(tmp, mode)
        except OSError:
            # Android's /sdcard FUSE mount refuses chmod; the replaced file
            # keeps the mount's fixed mode bits, which is what "the same
            # permissions the box uses" means there.
            pass
    os.replace(tmp, target)
    persisted = nsud.persist_one(rel, log=log)
    if not persisted and nsud._is_tvos():
        log("%s: tvOS vector unconfirmed (POSIX copy stands)" % rel)
        return False
    return True


def _client_enabled():
    """(known, enabled) from Kodi's own view of pvr.iptvsimple."""
    try:
        resp = _rpc(
            "Addons.GetAddonDetails",
            {"addonid": PVR_ADDON_ID, "properties": ["enabled"]},
        )
    except Exception:
        return False, False
    result = resp.get("result")
    if not isinstance(result, dict):
        return False, False
    return True, bool(result.get("addon", {}).get("enabled"))


def reload_client(log=None):
    """Disable then enable pvr.iptvsimple so it re-reads its instance
    settings. Only when the client is already enabled. The pause marker
    brackets the two calls (a crash in between is healed at the next boot by
    the same recovery the restore pause relies on). Returns (outcome,
    detail)."""
    log = log or _default_log
    known, enabled = _client_enabled()
    if not known:
        return SKIPPED, "%s is not installed; nothing to reload" % PVR_ADDON_ID
    if not enabled:
        return SKIPPED, (
            "%s is disabled; left as it is (a reload never enables a client "
            "the box had off)" % PVR_ADDON_ID
        )
    tools = None
    try:
        from resources.lib.modules import tools as tools_mod

        tools = tools_mod
    except Exception:
        pass
    if tools is not None:
        tools.mark_pvr_paused()
    try:
        _rpc("Addons.SetAddonEnabled", {"addonid": PVR_ADDON_ID, "enabled": False})
        _rpc("Addons.SetAddonEnabled", {"addonid": PVR_ADDON_ID, "enabled": True})
    except Exception as e:  # noqa: BLE001 - reported; the marker heals it
        return ERROR, "reload toggle failed (%s: %s); re-enabled at next boot" % (
            type(e).__name__,
            e,
        )
    deadline = time.time() + _RELOAD_POLL_S
    while True:
        _known, now_enabled = _client_enabled()
        if now_enabled:
            if tools is not None:
                tools.clear_pvr_pause_marker()
            return APPLIED, "%s reloaded (disable/enable)" % PVR_ADDON_ID
        if time.time() >= deadline:
            break
        xbmc.sleep(300)
    return ERROR, (
        "%s did not report enabled within %ds; re-enabled at next boot"
        % (PVR_ADDON_ID, int(_RELOAD_POLL_S))
    )


def sync(log=None, playing=None, reload=True):
    """The step. Returns a dict:

        outcome  applied | already-correct | skipped | error
        detail   one line for the result record
        changed  template names written this run
        reload   (outcome, detail) of the reload, or None when none was owed

    One log line per template that changed, one for every guard that fired,
    one for the reload. Never raises."""
    log = log or _default_log
    result = {"outcome": SKIPPED, "detail": "", "changed": [], "reload": None}
    try:
        if playing is None:
            playing = is_playing()
        if playing:
            result["detail"] = "deferred: something is playing"
            log(result["detail"])
            return result
        names = list_share_templates()
        if not names:
            # The share did not list. On Android this may be a Tailscale
            # client that is not connected; tailnet.ensure_share_reachable
            # starts it once, bounded, and re-probes. Then ONE re-list.
            reachable, _detail = tailnet.ensure_share_reachable(log)
            if reachable:
                names = list_share_templates()
        if not names:
            result["detail"] = (
                "share unreachable or carries no instance files (%s); skipped"
                % sharehost.IPTV_URL
            )
            log(result["detail"])
            return result
        changed = []
        problems = []
        for name in names:
            raw = read_share_template(name)
            if not raw:
                problems.append("%s: unreadable on the share; skipped" % name)
                log(problems[-1])
                continue
            data, info = prepare_template(raw)
            if data is None:
                problems.append("%s: %s; skipped" % (name, info))
                log(problems[-1])
                continue
            rel = "addon_data/%s/%s" % (PVR_ADDON_ID, name)
            have = _read_box_copy(rel)
            if have == data:
                continue
            try:
                ok = _write_box_copy(rel, data, log)
            except Exception as e:  # noqa: BLE001 - per file, never the step
                problems.append("%s: write failed (%s: %s)" % (name, type(e).__name__, e))
                log(problems[-1])
                continue
            if not ok:
                problems.append("%s: written but not durably persisted" % name)
                continue
            changed.append(name)
            log(
                "%s %s from the share%s"
                % (
                    name,
                    "updated" if have else "installed",
                    (" (%d legacy host url(s) migrated to %s)" % (info, sharehost.SHARE_HOST))
                    if info
                    else "",
                )
            )
        result["changed"] = changed
        if changed and reload:
            outcome, detail = reload_client(log)
            result["reload"] = (outcome, detail)
            log(detail)
            if outcome == ERROR:
                problems.append(detail)
        if problems:
            result["outcome"] = ERROR if changed or any(
                "write failed" in p or "persisted" in p or "reload" in p
                for p in problems
            ) else SKIPPED
            result["detail"] = "; ".join(problems)
            if changed:
                result["detail"] = "%s written; %s" % (", ".join(changed), result["detail"])
            return result
        if changed:
            result["outcome"] = APPLIED
            result["detail"] = "%s written; %s" % (
                ", ".join(changed),
                result["reload"][1] if result["reload"] else "no reload requested",
            )
        else:
            result["outcome"] = ALREADY
            result["detail"] = "%d instance file(s) match the share" % len(names)
        return result
    except Exception as e:  # noqa: BLE001 - never disturb boot or the flow
        result["outcome"] = ERROR
        result["detail"] = "%s: %s" % (type(e).__name__, e)
        try:
            log(result["detail"])
        except Exception:
            pass
        return result
