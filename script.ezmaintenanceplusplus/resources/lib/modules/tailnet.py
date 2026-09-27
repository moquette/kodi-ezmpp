"""Is the mini reachable over the tailnet, and if not, can this box do
anything about it?

MEASURED via the Tailscale admin API 2026-09-26: ts1 was last seen on the
tailnet 2026-09-04 and ts2 on 2026-08-31 while ts1 was up on the LAN that
minute (Kodi answered on 192.168.7.77:9090). A box can run with its
Tailscale client not connected, and once the share lives at SHARE_HOST such
a box has no IPTV and no backups until Tailscale comes back. So, on Android
only, when the share does not list, this starts the Tailscale app once, waits
a bounded time, re-tests and logs one line either way. tvOS has no way to
start another app from inside Kodi; it logs that the tailnet is down.

MEASURED on the office Fire TV 2026-09-26: the package is ``com.tailscale.ipn``
(1.102.2), its launcher AND leanback launcher activity both resolve to
``com.tailscale.ipn/.MainActivity`` (``cmd package resolve-activity``), which
is what ``xbmc.startAndroidActivity(package)`` starts. The app also exposes
``com.tailscale.ipn.CONNECT_VPN`` on ``.IPNReceiver``, but that is a broadcast
and Kodi's Python can only start activities, so the UI launch is the one
lever available. GUESS, not measured: that opening the app reconnects a
client the user left disconnected; the bounded re-test is what makes the
outcome honest either way.
"""

import time

import xbmc
import xbmcvfs

from resources.lib.modules import sharehost

TAILSCALE_PACKAGE = "com.tailscale.ipn"

# How long to give Tailscale to bring the tunnel up after the launch.
WAIT_S = 20
_POLL_S = 2

_STATE = {"nudged": False}


def _default_log(msg):
    xbmc.log("ezmaintenanceplus: tailnet: %s" % msg, level=xbmc.LOGINFO)


def _platform():
    try:
        if xbmc.getCondVisibility("System.Platform.TVOS"):
            return "tvos"
        if xbmc.getCondVisibility("System.Platform.Android"):
            return "android"
    except Exception:
        pass
    return "other"


def share_reachable():
    """True iff the share root lists at least one entry over the VFS. A dead
    mount and an empty directory both answer ([], []) from xbmcvfs.listdir;
    the share root always carries Share/ and Backup/, so empty means down."""
    try:
        dirs, files = xbmcvfs.listdir(sharehost.NFS_ROOT)
    except Exception:
        return False
    return bool(dirs or files)


def reset():
    """Test seam: allow another nudge in this process."""
    _STATE["nudged"] = False


def ensure_share_reachable(log=None, wait_s=WAIT_S, sleep=None):
    """(reachable, detail). Probes the share; when it is down and this is an
    Android box, starts the Tailscale app ONCE per process, waits up to
    `wait_s` re-probing, and reports. Exactly one log line for the whole
    decision. Never raises, never loops past the bound."""
    log = log or _default_log
    sleep = sleep or (lambda s: xbmc.sleep(int(s * 1000)))
    try:
        if share_reachable():
            return True, "share reachable at %s" % sharehost.SHARE_HOST
        platform = _platform()
        if platform == "tvos":
            detail = (
                "tailnet down: %s does not answer and tvOS cannot start "
                "Tailscale from here" % sharehost.SHARE_HOST
            )
            log(detail)
            return False, detail
        if platform != "android":
            detail = "share unreachable at %s (not Android; nothing to start)" % (
                sharehost.SHARE_HOST
            )
            log(detail)
            return False, detail
        if _STATE["nudged"]:
            detail = (
                "share unreachable at %s; Tailscale was already started once "
                "this session, not again" % sharehost.SHARE_HOST
            )
            log(detail)
            return False, detail
        _STATE["nudged"] = True
        try:
            started = bool(xbmc.startAndroidActivity(TAILSCALE_PACKAGE))
        except Exception:
            started = False
        if not started:
            detail = (
                "share unreachable at %s and Tailscale (%s) could not be started"
                % (sharehost.SHARE_HOST, TAILSCALE_PACKAGE)
            )
            log(detail)
            return False, detail
        waited = 0
        while waited < wait_s:
            sleep(_POLL_S)
            waited += _POLL_S
            if share_reachable():
                detail = (
                    "share was unreachable; started Tailscale (%s) and %s answered "
                    "after %d s" % (TAILSCALE_PACKAGE, sharehost.SHARE_HOST, waited)
                )
                log(detail)
                return True, detail
        detail = (
            "share unreachable; started Tailscale (%s) but %s still did not "
            "answer after %d s" % (TAILSCALE_PACKAGE, sharehost.SHARE_HOST, waited)
        )
        log(detail)
        return False, detail
    except Exception as e:  # noqa: BLE001 - a probe must never break a boot
        detail = "share probe failed: %s: %s" % (type(e).__name__, e)
        try:
            log(detail)
        except Exception:
            pass
        return False, detail
