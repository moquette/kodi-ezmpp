"""The ONE place the mini's address lives.

Owner decision 2026-09-26: every box reaches the mini over Tailscale, never
over the home router's DHCP address. The mini's tailnet address is fixed by
Tailscale (``100.121.59.123``), so it holds away from home, across router
changes, and after a DHCP lease moves. Every artifact in this add-on that
names the mini derives from ``SHARE_HOST``: the House bundle's ``sources.xml``
and per-class overlays carry the ``@SHARE_HOST@`` token and are rendered
through :func:`render` at load, the profile migrates a box's live sources and
backup folders off any legacy host through :func:`migrate`, and the PVR share
step lists ``IPTV_URL``.

``LEGACY_HOSTS`` is the list of addresses the fleet USED to be pointed at;
``migrate`` rewrites exactly those and nothing else, so a source the owner
added by hand to some other machine is never touched. Add to the list when
the mini moves again; never remove an entry while a box may still carry it.
"""

import re

SHARE_HOST = "100.121.59.123"

# Addresses boxes were previously configured with. The LAN DHCP address was
# baked into every profile artifact and every box until 2026-09-26.
LEGACY_HOSTS = ("192.168.7.2",)

TOKEN = "@SHARE_HOST@"

KODI_ROOT = "/Users/moquette/Kodi/"

# The nfs:// URLs, port-free and slash-terminated: Kodi dedupes sources on
# the exact path string and an explicit :2049 breaks its NFS write path
# (wiz._strip_nfs_port has the history).
NFS_ROOT = "nfs://%s%s" % (SHARE_HOST, KODI_ROOT)
SHARE_URL = NFS_ROOT + "Share/"
BACKUP_URL = NFS_ROOT + "Backup/"
IPTV_URL = SHARE_URL + "iptv/"

# nfs://<legacy host>[:port]/ at the start of a path, or anywhere inside a
# document (the same expression serves a single setting value and a whole
# instance-settings template).
_LEGACY_RE = re.compile(
    r"nfs://(?:%s)(?::\d+)?/" % "|".join(re.escape(h) for h in LEGACY_HOSTS)
)


def render(text):
    """Substitute the token in a bundle document. Text in, text out."""
    return text.replace(TOKEN, SHARE_HOST)


def names_legacy_host(text):
    """True iff the text carries an nfs:// URL on a legacy host."""
    return bool(text) and _LEGACY_RE.search(text) is not None


def migrate(text):
    """Rewrite every nfs:// URL on a legacy host to SHARE_HOST. Returns
    (new_text, count). A port on the legacy form is dropped in the same
    pass, because the rendered fleet form never carries one. Text that names
    no legacy host comes back unchanged with count 0, so a second run is a
    no-op by construction."""
    if not text:
        return text, 0
    return _LEGACY_RE.subn("nfs://%s/" % SHARE_HOST, text)


def backup_url(device_class):
    """The per-class backup folder, e.g. nfs://.../Kodi/Backup/fireos/."""
    return "%s%s/" % (BACKUP_URL, device_class)
