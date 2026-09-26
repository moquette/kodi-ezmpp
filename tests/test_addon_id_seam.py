"""The add-on id has ONE home: resources/lib/modules/addon_id.py.

Every other production module reads it from there at import. The point is
separability: a module that carries its own copy of the literal cannot be
lifted into another add-on (the POV skin is expected to absorb pieces of this
one) without a hand edit in every file, and a host that vendors the modules
needs a single switch to make them act as itself.
"""

import importlib
import re
import sys
import types
from pathlib import Path

ADDON_ROOT = Path(__file__).resolve().parent.parent / "script.ezmaintenanceplusplus"
SEAM = ADDON_ROOT / "resources" / "lib" / "modules" / "addon_id.py"
LITERAL = "script.ezmaintenanceplusplus"


def _production_modules():
    return sorted(
        p for p in ADDON_ROOT.rglob("*.py") if "_vendor" not in p.parts
    )


def test_the_literal_lives_in_exactly_one_production_module():
    carriers = {}
    for path in _production_modules():
        hits = [
            n + 1
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines())
            if LITERAL in line
        ]
        if hits:
            carriers[str(path.relative_to(ADDON_ROOT))] = hits
    assert carriers == {"resources/lib/modules/addon_id.py": carriers.get(
        "resources/lib/modules/addon_id.py"
    )}, "the add-on id literal leaked out of the seam: %r" % carriers


def test_every_former_carrier_binds_its_id_through_the_seam():
    """The twelve modules that used to carry the literal now bind it from
    addon_id.get() at import, so an override set before import reaches them."""
    expected = {
        "default.py": "AddonID = _addon_id.get()",
        "service.py": "AddonID = _addon_id.get()",
        "resources/lib/modules/control.py": "AddonID = _addon_id.get()",
        "resources/lib/modules/dropbox_remote.py": "AddonID = _addon_id.get()",
        "resources/lib/modules/logviewer.py": "AddonID = _addon_id.get()",
        "resources/lib/modules/maintenance.py": "addon_id = _addon_id.get()",
        "resources/lib/modules/nsub.py": "ADDON_ID = _addon_id.get()",
        "resources/lib/modules/nsud.py": "ADDON_ID = _addon_id.get()",
        "resources/lib/modules/onetap.py": "_ADDON_ID = _addon_id.get()",
        "resources/lib/modules/profile.py": "OWN_ID = _addon_id.get()",
        "resources/lib/modules/tools.py": "AddonID = _addon_id.get()",
        "resources/lib/modules/wiz.py": "AddonID = _addon_id.get()",
    }
    for rel, binding in expected.items():
        text = (ADDON_ROOT / rel).read_text(encoding="utf-8")
        assert binding in text, "%s no longer binds its id through the seam" % rel
        assert re.search(r"^from resources\.lib\.modules import addon_id as _addon_id$",
                         text, re.M), "%s does not import the seam" % rel


def _fresh_seam(monkeypatch):
    monkeypatch.syspath_prepend(str(ADDON_ROOT))
    for name in list(sys.modules):
        if name == "resources" or name.startswith("resources."):
            monkeypatch.delitem(sys.modules, name)
    return importlib.import_module("resources.lib.modules.addon_id")


def test_resolver_falls_back_to_the_literal_without_kodi(monkeypatch):
    monkeypatch.delitem(sys.modules, "xbmcaddon", raising=False)
    seam = _fresh_seam(monkeypatch)
    assert seam.get() == LITERAL


def test_resolver_falls_back_when_kodi_answers_with_a_non_string(monkeypatch):
    fake = types.ModuleType("xbmcaddon")

    class _Addon:
        def __init__(self, *a, **k):
            pass

        def getAddonInfo(self, key):
            return ""

    fake.Addon = _Addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake)
    seam = _fresh_seam(monkeypatch)
    assert seam.get() == LITERAL


def test_resolver_reads_the_running_addon_from_kodi(monkeypatch):
    fake = types.ModuleType("xbmcaddon")

    class _Addon:
        def __init__(self, *a, **k):
            pass

        def getAddonInfo(self, key):
            return {"id": "script.some.host"}.get(key, "")

    fake.Addon = _Addon
    monkeypatch.setitem(sys.modules, "xbmcaddon", fake)
    seam = _fresh_seam(monkeypatch)
    assert seam.get() == "script.some.host"


def test_resolver_returns_the_override_when_set(monkeypatch):
    monkeypatch.delitem(sys.modules, "xbmcaddon", raising=False)
    seam = _fresh_seam(monkeypatch)
    assert seam.get() == LITERAL
    seam.set_override("skin.estuary.pov")
    assert seam.get() == "skin.estuary.pov"
    seam.set_override(None)
    assert seam.get() == LITERAL


def test_override_set_before_import_reaches_a_former_carrier(monkeypatch):
    """The contract a host add-on relies on: set_override, then import."""
    fake_vfs = types.ModuleType("xbmcvfs")
    fake_vfs.translatePath = lambda p: p
    monkeypatch.setitem(sys.modules, "xbmcvfs", fake_vfs)
    monkeypatch.delitem(sys.modules, "xbmcaddon", raising=False)
    seam = _fresh_seam(monkeypatch)
    seam.set_override("skin.estuary.pov")
    nsud = importlib.import_module("resources.lib.modules.nsud")
    assert nsud.ADDON_ID == "skin.estuary.pov"


def test_override_rejects_an_empty_or_non_string_value(monkeypatch):
    seam = _fresh_seam(monkeypatch)
    for bad in ("", 7, b"x"):
        try:
            seam.set_override(bad)
        except ValueError:
            continue
        raise AssertionError("set_override accepted %r" % (bad,))
