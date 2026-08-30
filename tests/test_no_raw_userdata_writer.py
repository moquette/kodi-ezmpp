"""CHOKEPOINT LINT: nobody writes a Kodi-read userdata XML behind nsud's back.

WHY THIS EXISTS
---------------
2026-07-14. An EZ Maintenance++ restore destroyed the owner's customized Apple TV menu.
The root cause was a storage rule violated in one function. We fixed that function, wrote
a two-layer tvOS fake, and added regression tests.

Then an adversarial review found the SAME CLASS OF BUG, still shipping, in a function
nobody had thought to test: `boxsetup._write_weather_settings` wrote
`addon_data/weather.multi/settings.xml` with a plain `open(..., "w")` and never called
`nsud.persist_one` - while its sibling `_add_sources`, ninety lines earlier in the SAME
FILE, did exactly that and carried a comment explaining why. There was no test file for
`boxsetup.py` at all.

That is the lesson: **a test only protects the code someone remembered to test.** A lint
protects the code nobody thought about. This file is the chokepoint.

THE RULE
--------
On Apple TV, Kodi reads certain userdata XML through its OWN VFS, which checks
NSUserDefaults BEFORE the disk file (CTVOSFile::Exists/Open, TVOSFile.cpp:70-122). A key
SHADOWS the disk. So a plain POSIX write to such a file can be silently invisible to Kodi
forever - the write "succeeds", the setting never applies, and no error is raised.

`nsud.persist_one()` is the ONLY sanctioned way to land such a write. It decides what may
be vectored (`_should_vector`), writes THROUGH the VFS, reads back to confirm, and only
then drops the redundant POSIX copy. It is a no-op on Fire TV / desktop.

=> Any function that writes a userdata/addon_data XML MUST route through nsud.

WHAT THIS DOES NOT DO
---------------------
This is an AST check, not a proof. It resolves `open(...)`/`xbmcvfs.File(...)` writes and
looks for an nsud call in the same function. It can be defeated by enough indirection
(handing the path to a helper in another module, rebinding `open`, building the mode
string at runtime). It is a guardrail against the accident that actually happened twice,
not a security boundary. If you find yourself routing around it, that IS the review.
"""

import ast
import pathlib

import pytest

ADDON = pathlib.Path(__file__).resolve().parents[1] / "script.ezmaintenanceplusplus"

# nsud.py IS the chokepoint - it is the one module allowed to do raw I/O here.
CHOKEPOINT = {"nsud.py"}

# Signals that a function is dealing with a file Kodi reads through its VFS.
USERDATA_HINTS = (
    "special://profile",
    "special://masterprofile",
    "special://userdata",
    "userdata",
    "addon_data",
)

# The sanctioned exits.
NSUD_CALLS = {"persist_one", "rewrite_userdata_xml"}

# Calling one of these IS writing a Kodi-read userdata file, by definition -
# `write_guisetting`'s single job is rewriting guisettings.xml - so a call to
# one counts as both the write AND the userdata mention, and the calling
# function must carry its own nsud call (wiz._apply_boot_skin is the reference).
# Added 2026-08-30, before profile.py landed, because the path reaches these as
# an ARGUMENT: no string constant, no *_path() helper, nothing else fires.
WRITE_DELEGATES = {"write_guisetting"}

# Deliberate exemptions. Each MUST carry a reason. Do not add to this list to silence a
# finding - a finding here means a file Kodi reads may be silently shadowed on Apple TV.
ALLOWLIST = {
    # wiz.FIX_SPECIAL rewrites absolute paths embedded INSIDE userdata xml content before
    # a backup, gated behind the legacy BackupFixSpecialHome setting (default off). It is
    # a pre-backup content rewrite of files it does not own, not a settings write, and it
    # runs on the local box's own copies. Flagged 2026-07-14 as a genuine latent tvOS
    # shadow hazard (a stale key would hide the "fixed" bytes from Kodi). Left as-is
    # because the setting is off on the whole fleet and the correct fix is to retire
    # FIX_SPECIAL entirely; tracked, not silently blessed.
    ("wiz.py", "FIX_SPECIAL"),
    # tools._set_devicename is a deliberate both-halves write, surfaced when the
    # lint learned to see write_guisetting delegates (2026-08-30). Its durable
    # half on tvOS is the LIVE Settings.SetSettingValue (the NSUserDefaults key
    # Kodi reads tracks the live store at the clean-close flush); the
    # write_guisetting file half exists ONLY for the Fire TV / Android unclean
    # kill, is documented in the function as best-effort, and on tvOS is either
    # a no-op (POSIX copy dropped after vectoring; write_guisetting returns
    # False on a missing file) or an edit of an already-shadowed dead copy.
    # Adding persist_one here WITHOUT the re-materialize step would vector a
    # possibly stale POSIX guisettings.xml over the key Kodi actually reads -
    # the shadowing bug in reverse. The sanctioned full pattern
    # (re-materialize, edit, persist_one) is wiz._apply_boot_skin; if
    # _set_devicename ever needs file-half durability on tvOS, it adopts that
    # pattern rather than a bare persist_one.
    ("tools.py", "_set_devicename"),
}


def _py_files():
    return sorted(
        p
        for p in ADDON.rglob("*.py")
        if p.name not in CHOKEPOINT and "packages" not in p.parts
    )


def _is_write_call(node):
    """open(..., 'w'|'wb'), xbmcvfs.File(..., 'w'), any `.write(...)` attribute
    call (an ElementTree `tree.write(path)` is a full-file rewrite; a file
    object's `.write(data)` only appears inside an open() that already
    matched), or a call to a WRITE_DELEGATES member."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = (
        fn.id
        if isinstance(fn, ast.Name)
        else (fn.attr if isinstance(fn, ast.Attribute) else "")
    )
    if name in WRITE_DELEGATES:
        return True
    if name == "write" and isinstance(fn, ast.Attribute) and node.args:
        return True
    if name not in ("open", "File"):
        return False
    for arg in list(node.args[1:]) + [
        k.value for k in node.keywords if k.arg == "mode"
    ]:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if "w" in arg.value or "a" in arg.value:
                return True
    return False


def _mentions_userdata(fnode):
    for n in ast.walk(fnode):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            low = n.value.lower()
            if any(h in low for h in USERDATA_HINTS):
                return True
        if isinstance(n, ast.Call):
            fn = n.func
            nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            # a WRITE_DELEGATES call is a userdata write by definition; the path
            # arrives as an argument, so no string constant can be here to see.
            if nm in WRITE_DELEGATES:
                return True
            # a call to a *_settings_path()/_weather_settings_path() style helper
            if nm.endswith("_path") and (
                "settings" in nm or "userdata" in nm or "profile" in nm
            ):
                return True
    return False


def _calls_nsud(fnode):
    for n in ast.walk(fnode):
        if isinstance(n, ast.Call):
            fn = n.func
            nm = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if nm in NSUD_CALLS:
                return True
    return False


def _offenders():
    bad = []
    for path in _py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for fnode in ast.walk(tree):
            if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if (path.name, fnode.name) in ALLOWLIST:
                continue
            writes = any(_is_write_call(n) for n in ast.walk(fnode))
            if writes and _mentions_userdata(fnode) and not _calls_nsud(fnode):
                bad.append((path.name, fnode.name, fnode.lineno))
    return bad


def test_no_module_writes_userdata_xml_behind_nsud():
    """A raw write to a Kodi-read userdata file, with no nsud.persist_one, is a bug.

    This exact check, had it existed on 2026-07-13, would have failed on
    boxsetup._write_weather_settings before it ever reached a box.
    """
    offenders = _offenders()
    assert not offenders, (
        "These functions write a userdata/addon_data file with plain POSIX/VFS I/O and "
        "never route through nsud. On Apple TV a stale NSUserDefaults key SHADOWS the "
        "disk file, so Kodi may never see these bytes and no error is raised:\n"
        + "\n".join("  %s::%s (line %d)" % o for o in offenders)
        + "\n\nFix: call nsud.persist_one('<userdata-relative path>', log=...) after the "
        "write, as wiz._apply_boot_skin does. If the file is an add-on's PRIVATE data "
        "that only IT reads with plain open(), persist_one already leaves it on disk - "
        "call it anyway and let it decide."
    )


def test_the_known_good_pattern_is_present():
    """Guard the lint itself: wiz._apply_boot_skin is the reference implementation.

    It writes userdata/guisettings.xml with a plain open(..., "wb") and then calls
    nsud.persist_one, which is exactly the shape this lint certifies. If someone
    removes that persist_one call, the lint above must catch it; if THIS test
    fails, the lint's detection is broken, not wiz.

    Both previous reference implementations lived in boxsetup.py -
    _write_weather_settings (the function whose 2026-07-13 bug this whole lint
    exists to prevent) and add_media_sources - and the module was deleted on
    2026-07-22 with the "Set up this box" feature. The lesson outlived the code.
    """
    src = (ADDON / "resources/lib/modules/wiz.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fns = {
        n.name: n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_apply_boot_skin",):
        assert name in fns, "%s vanished - update this lint" % name
        assert _is_write_call_in(fns[name]), (
            "%s no longer writes - update this lint" % name
        )
        assert _calls_nsud(fns[name]), (
            "%s writes a userdata file but no longer calls nsud - this is the 2026-07-14 "
            "bug class returning" % name
        )


def _is_write_call_in(fnode):
    return any(_is_write_call(n) for n in ast.walk(fnode))


# --------------------------------------------------------------------------- #
# The 2026-08-30 extension: writes the ORIGINAL lint could not see.
#
# The settings-profile plan (docs/settings-profile-plan.md 7.3) found the gap
# before the code that would have fallen through it was written:
# `_kodisettings.write_guisetting` persists with `tree.write(...)` and calls no
# nsud function, so a module doing its class A file half through an ElementTree
# helper and forgetting the vector would pass this lint, pass every test, and
# fail only on Apple TV - the 2026-07-13 `boxsetup._write_weather_settings`
# shape with a different verb. Two closures, landed BEFORE profile.py:
#
#   1. `_is_write_call` now also matches an attribute call named `write` with
#      at least one argument (`tree.write(path)`, `ElementTree(root).write(p)`).
#   2. `WRITE_DELEGATES`: calling `write_guisetting` IS writing userdata by
#      definition (its one job is rewriting guisettings.xml), so such a call
#      counts as both the write and the userdata mention, and the caller must
#      carry its own nsud call - exactly what `wiz._apply_boot_skin` does.
# --------------------------------------------------------------------------- #

_TREE_WRITE_OFFENDER = """
def sneaky(values):
    import xml.etree.ElementTree as ET
    path = "/x/userdata/guisettings.xml"
    tree = ET.parse(path)
    tree.write(path)
"""

_DELEGATE_OFFENDER = """
def sneaky(path, sid, value):
    from resources.lib.modules import _kodisettings
    _kodisettings.write_guisetting(path, sid, value)
"""

_DELEGATE_COMPLIANT = """
def fine(path, sid, value):
    from resources.lib.modules import _kodisettings, nsud
    _kodisettings.write_guisetting(path, sid, value)
    nsud.persist_one("guisettings.xml")
"""


def _offends(src):
    tree = ast.parse(src)
    for fnode in ast.walk(tree):
        if not isinstance(fnode, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        writes = any(_is_write_call(n) for n in ast.walk(fnode))
        if writes and _mentions_userdata(fnode) and not _calls_nsud(fnode):
            return True
    return False


def test_lint_sees_an_elementtree_write():
    """tree.write(path) with a userdata path and no nsud call must be an offender.

    This exact shape is how `_kodisettings.write_guisetting` persists, and before
    the 2026-08-30 extension it was invisible to `_is_write_call` (which matched
    only callables named `open` or `File`)."""
    assert _offends(_TREE_WRITE_OFFENDER), (
        "the lint no longer sees an ElementTree .write() as a write - the "
        "settings-profile class A file half could ship without its vector again"
    )


def test_lint_sees_a_write_guisetting_delegate():
    """Calling write_guisetting without nsud must be an offender even with no
    userdata string in sight: the path arrives as an argument, so neither the
    string-constant nor the *_path() heuristic can fire, and only the delegate
    rule catches it."""
    assert _offends(_DELEGATE_OFFENDER), (
        "the lint no longer treats write_guisetting as a userdata write - "
        "callers can rewrite guisettings.xml with no vector and pass"
    )


def test_write_guisetting_plus_persist_one_is_compliant():
    """The sanctioned pattern (write_guisetting then persist_one, as
    wiz._apply_boot_skin does) must NOT be flagged."""
    assert not _offends(_DELEGATE_COMPLIANT), (
        "the lint flags the known-good write_guisetting + persist_one pattern"
    )


@pytest.mark.parametrize("mod,fn", sorted(ALLOWLIST))
def test_allowlist_entries_still_exist(mod, fn):
    """A stale allowlist entry silently widens the hole. Fail when the code is gone."""
    hits = [p for p in ADDON.rglob(mod)]
    assert hits, "allowlisted module %s no longer exists - drop it from ALLOWLIST" % mod
    tree = ast.parse(hits[0].read_text(encoding="utf-8"))
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert fn in names, (
        "%s::%s is allowlisted but no longer exists - drop it from ALLOWLIST"
        % (mod, fn)
    )
