"""Pause the PVR client around work that touches its databases.

``paused()`` disables pvr.iptvsimple with Kodi's own
``Addons.SetAddonEnabled``, runs the caller's block, then re-enables it. It is
the same mechanism the restore pause and the PVR share reload use, with the
same crash guard: the restore's pause marker (``tools.mark_pvr_paused``) is
written before the disable, so a crash or kill between the two calls is healed
by ``service._maybe_resume_paused_pvr`` at the next boot, and it is cleared
only once Kodi reports the client enabled again.

Rules:

* Only a client that is installed AND enabled is paused, and only that client
  is re-enabled. A client the owner switched off stays off.
* A disable Kodi does not acknowledge is ``FAILED``: the caller must not do
  the work, because the client is still live. The re-enable still runs.

Why not a setting: Kodi has no ``pvrmanager.enabled``. It is absent from
Omega and Piers (``xbmc/settings/Settings.h``, ``system/settings/*.xml``), and
``Settings.GetSettingValue`` on an unknown id answers ``Invalid params``
(``SettingsOperations.cpp`` ``GetSettingValue``). Clear recent channels used
it until 2026.10.15.1, so its "pause" never ran and the database write went
under a live client.
"""

import contextlib
import json
import time

import xbmc

PVR_ADDON_ID = "pvr.iptvsimple"

PAUSED = "paused"  # the client was enabled; it is disabled for the block
NOT_NEEDED = "not-needed"  # not installed or already disabled; nothing toggled
FAILED = "failed"  # Kodi did not acknowledge the disable; do not do the work

_POLL_S = 10.0


def _log(msg, level=None):
    xbmc.log(
        "ezmaintenanceplus: PVR pause: %s" % msg,
        level=xbmc.LOGINFO if level is None else level,
    )


def _rpc(method, params):
    req = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        return json.loads(xbmc.executeJSONRPC(json.dumps(req)))
    except Exception:
        return {}


def client_state():
    """(installed, enabled) from Kodi's own view of the client."""
    res = _rpc(
        "Addons.GetAddonDetails", {"addonid": PVR_ADDON_ID, "properties": ["enabled"]}
    )
    result = res.get("result") if isinstance(res, dict) else None
    if not isinstance(result, dict) or not isinstance(result.get("addon"), dict):
        return False, False
    return True, bool(result["addon"].get("enabled"))


def _set_enabled(flag):
    res = _rpc(
        "Addons.SetAddonEnabled", {"addonid": PVR_ADDON_ID, "enabled": bool(flag)}
    )
    return isinstance(res, dict) and res.get("result") == "OK"


def _wait_enabled(want):
    deadline = time.time() + _POLL_S
    while True:
        _installed, enabled = client_state()
        if enabled == want:
            return True
        if time.time() >= deadline:
            return False
        xbmc.sleep(300)


def _tools():
    try:
        from resources.lib.modules import tools

        return tools
    except Exception:
        return None


@contextlib.contextmanager
def paused(settle_ms=2000):
    """Context manager yielding PAUSED, NOT_NEEDED or FAILED. On PAUSED the
    client is re-enabled on exit, whatever the block did."""
    installed, enabled = client_state()
    if not (installed and enabled):
        yield NOT_NEEDED
        return
    tools = _tools()
    if tools is not None:
        tools.mark_pvr_paused()
    state = PAUSED
    if not (_set_enabled(False) and _wait_enabled(False)):
        state = FAILED
        _log("%s did not disable; work skipped" % PVR_ADDON_ID, xbmc.LOGWARNING)
    else:
        _log("%s disabled" % PVR_ADDON_ID)
        xbmc.sleep(settle_ms)  # let the PVR manager finish unloading the client
    try:
        yield state
    finally:
        if _set_enabled(True) and _wait_enabled(True):
            if tools is not None:
                tools.clear_pvr_pause_marker()
            _log("%s re-enabled" % PVR_ADDON_ID)
        else:
            _log(
                "%s did not report enabled; re-enabled at next boot" % PVR_ADDON_ID,
                xbmc.LOGERROR,
            )
