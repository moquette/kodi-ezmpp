"""Re-apply Kodi settings through the JSON-RPC API after a restore.

Why this exists: on iOS/tvOS, Kodi mirrors guisettings.xml in NSUserDefaults and
SHADOWS the file with that key - Kodi NEVER copies a key back to disk (corrected
2026-07-14, see nsud.py) - so a file-only restore of guisettings.xml
is silently reverted, which is why a restored Apple TV came up "empty". The official
Backup add-on (robweber/xbmcbackup) works around this by applying settings through
Settings.SetSettingValue, which updates Kodi's LIVE store so the values persist. We do
the same, reading the values from the just-restored guisettings.xml and coercing each
to the type the live setting expects (so it works with existing backups, no new format).
On Fire TV / Android this is harmless reinforcement; on tvOS it's what makes restore
actually stick.
"""

import json
import os
import xml.etree.ElementTree as ET

import xbmc


def _rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return json.loads(xbmc.executeJSONRPC(json.dumps(req)))


def _live_settings():
    """Return {id: setting_dict} for every setting, used for type + change detection."""
    resp = _rpc("Settings.GetSettings", {"level": "expert"})
    out = {}
    for s in resp.get("result", {}).get("settings", []):
        sid = s.get("id")
        if sid:
            out[sid] = s
    return out


def _coerce(raw, typ):
    """Coerce a guisettings.xml text value to the type Settings.SetSettingValue wants."""
    if typ == "boolean":
        return str(raw).lower() in ("true", "1", "yes", "on")
    if typ == "integer":
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    if typ == "number":
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return "" if raw is None else str(raw)  # string / path / addon / etc.


# Settings the archive must NEVER live-apply. Two different reasons, both load-bearing.
#
# lookandfeel.skin - live-setting it makes Kodi switch skins immediately and start its
# "keep this skin?" countdown, which during a restore is unanswerable (an EZM++ modal or
# progress dialog owns the screen), so the countdown expires and Kodi REVERTS the skin,
# overwriting the restored value (hardware-reproduced on atv2, 2026-07-17: the restored
# box came up in stock Estuary). The restored guisettings.xml already carries the skin;
# the mandatory post-restore restart boots straight into it with no countdown firing.
#
# services.devicename and filecache.memorysize - these describe the TARGET HARDWARE, not
# the backup, so the archive's value must never win. Skipping the live-apply is only half
# the fix and is INSUFFICIENT on its own, because the archive's values still sit in the
# restored guisettings.xml and win at the next boot. wiz._preserve_device_settings owns
# the other half, and the two ids DIVERGE there - same principle, different resolutions,
# and collapsing them back into one rule is a regression:
#
#   * services.devicename is PRESERVED. This box's captured live value is written back
#     over the archive's, so the box keeps the name it answers to on the network.
#   * filecache.memorysize is RESET to tools.KODI_DEFAULT_MB, never preserved and never
#     cloned. The fleet mixes device classes whose right buffer differs, so no inherited
#     number survives a restore - not the archive's, and not this box's own previous one
#     either (owner decision 2026-07-31). The per-device recommendation is offered on
#     demand under Video Cache Buffer (tools.advancedSettings); there is NO prompt.
#
# Either way both layers end up agreeing - Kodi's live memory and the file it is flushed
# to - which is the both-halves pattern proven for lookandfeel.skin (wiz._apply_boot_skin).
#
# Adding an id here without a matching write-back or reset is a silent regression: the
# archive's value survives on disk and takes effect one restart later, where nothing is
# watching.
_BOOT_STATE_ONLY = frozenset(
    ("lookandfeel.skin", "services.devicename", "filecache.memorysize")
)

# Kodi's factory-default video cache buffer (MB). CANONICAL HERE - tools.py aliases it
# as KODI_DEFAULT_MB - because two mechanisms must land on the same number or the fleet
# flip-flops between them forever: every restore/wipe RESETS filecache.memorysize to
# this (tools.reset_cache_buffer, owner decision 2026-07-31), and the Settings Profile
# PINS it to the same value on every device class (owner decision 2026-08-30, fleet
# convergence - before the pin, the id floated across restore/flush cycles: archive 64,
# box 20, measured on atv1 during the 2026-08-30 restore-cycle proof).
KODI_DEFAULT_CACHE_MB = 20

# The one _BOOT_STATE_ONLY carve-out for the Settings Profile, imported by profile.py
# rather than restated (two copies of a predicate drifting is the failure that forced
# restorecheck to import nsud._is_skin_menu_sidecar). _BOOT_STATE_ONLY forbids the
# ARCHIVE's value from winning a restore; the profile is not an archive, it is the
# owner's canonical intent - but it may pin filecache.memorysize ONLY to the exact
# value every restore resets it to, so the two mechanisms can never disagree. The
# other two ids stay forbidden everywhere: services.devicename is per-box and
# lookandfeel.skin live-applied starts the unanswerable keep-skin countdown.
_PROFILE_MAY_PIN = {"filecache.memorysize": str(KODI_DEFAULT_CACHE_MB)}


def apply_guisettings(guisettings_path):
    """Push each value from a restored guisettings.xml into Kodi's live settings via
    JSON-RPC so the restore survives (notably tvOS). Returns the count applied."""
    if not os.path.exists(guisettings_path):
        return 0
    try:
        live = _live_settings()
    except Exception:
        return 0
    try:
        root = ET.parse(guisettings_path).getroot()
    except Exception:
        return 0

    applied = 0
    for node in root.iter("setting"):
        sid = node.get("id")
        if not sid or sid not in live:
            continue
        if sid in _BOOT_STATE_ONLY:
            continue
        meta = live[sid]
        if meta.get("type") == "action":
            continue
        val = _coerce(node.text, meta.get("type"))
        if val is None or meta.get("value") == val:
            continue
        try:
            resp = _rpc("Settings.SetSettingValue", {"setting": sid, "value": val})
            if resp.get("result") is True:
                applied += 1
        except Exception:
            pass
    return applied


def write_guisetting(guisettings_path, sid, value):
    """Write a single string setting straight into guisettings.xml on disk.

    The complement of apply_guisettings, for the OTHER persistence hazard. Settings.SetSettingValue
    updates only Kodi's in-memory store, which is flushed to guisettings.xml on a CLEAN shutdown;
    on Fire TV / Android an unclean kill (power pull, task-swipe) loses it. Writing the file too
    means the value survives an unclean kill. On tvOS the key SHADOWS the file on
    boot, so this write is same-value reinforcement there (SetSettingValue is the durable path), and
    on Fire TV/Android it is the durable one. Doing BOTH covers every platform.

    Finds the <setting id="sid"> element (creating it if absent) and sets its text, clearing the
    default="true" marker Kodi uses for untouched settings so the value is treated as user-set.
    Best-effort and fully guarded: any parse/write failure returns False and changes nothing.
    Returns True iff the file was rewritten."""
    try:
        if not os.path.exists(guisettings_path):
            return False
        tree = ET.parse(guisettings_path)
        root = tree.getroot()
        node = None
        for n in root.iter("setting"):
            if n.get("id") == sid:
                node = n
                break
        if node is None:
            node = ET.SubElement(root, "setting", {"id": sid})
        node.text = "" if value is None else str(value)
        if node.get("default") is not None:
            node.attrib.pop("default", None)
        tree.write(guisettings_path, encoding="utf-8", xml_declaration=True)
        return True
    except Exception:
        return False
