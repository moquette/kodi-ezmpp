"""The one place this add-on's id is decided.

Every module used to carry its own copy of the literal
"script.ezmaintenanceplusplus" at import time, which is the main thing that
stops a module from being lifted into another add-on (the owner expects the POV
skin to absorb pieces of this add-on later). Now the literal lives here and only
here; a grep-based test (tests/test_addon_id_seam.py) keeps it that way.

Resolution order, decided once and cached:

  1. an explicit override (`set_override`), for tests and for a future host
     add-on that vendors these modules and wants them to read and write ITS
     addon_data, settings and paths;
  2. what Kodi says the running add-on is (`xbmcaddon.Addon().getAddonInfo("id")`),
     which is this add-on on a box, whether the caller is default.py, service.py
     or a RunScript;
  3. the literal, when there is no Kodi at all (the test suite's fakes) or Kodi
     answers with something that is not a non-empty string.

On a box every path answers "script.ezmaintenanceplusplus", so behaviour is
identical to the literal it replaced. Modules bind the value at import
(`AddonID = addon_id.get()`), so a host that wants a different id must call
`set_override` BEFORE importing them; nothing here imports Kodi at module level,
so importing this module is free.
"""

DEFAULT_ID = "script.ezmaintenanceplusplus"

_override = None
_resolved = None


def set_override(value):
    """Force the id (a non-empty string), or clear the override with None.

    Clearing also drops the cached answer so the next get() resolves again."""
    global _override, _resolved
    if value is not None and (not isinstance(value, str) or not value):
        raise ValueError("addon id override must be a non-empty string or None")
    _override = value
    _resolved = None


def _from_kodi():
    try:
        import xbmcaddon

        value = xbmcaddon.Addon().getAddonInfo("id")
    except Exception:
        return None
    if isinstance(value, str) and value:
        return value
    return None


def get():
    """Return the add-on id this process should act as."""
    global _resolved
    if _override is not None:
        return _override
    if _resolved is None:
        _resolved = _from_kodi() or DEFAULT_ID
    return _resolved
