"""The Kodi-version restore gate: may this archive's addons/ tree land here?

Add-ons and skins are built against ONE Kodi major. A full backup zips
special://home/addons wholesale, so restoring a Kodi 21 (Omega) archive onto a
Kodi 22 (Piers) box lays Omega-era add-ons and skins over every Piers-native
one - a freshly rebuilt box is quietly un-rebuilt, with no error anywhere.
Userdata carries no such hazard (Kodi migrates settings across majors), and
add-ons re-download from their repositories, so the safe degraded restore is:
userdata in full, addons/ not at all, and one plain dialog saying so.

The gate compares the ``kodi_version`` major that backup() stamps into the
manifest (MANIFEST_KEY) with the major the box is running:

  same major             addons/ extracts exactly as it always has
  different major        addons/ members are withheld
  archive UNSTAMPED      withheld too. Every backup made before this gate
                         shipped carries no stamp, and the fleet's real
                         archives are all Kodi 21 while the boxes move to
                         Kodi 22 - the exact restore this gate exists to
                         catch. Guessing a version from source_os or file
                         contents is deliberately NOT done: unknown fails
                         safe, toward the recoverable side (a skipped add-on
                         re-downloads; an overwritten install does not).
  RUNNING major unknown  the gate stands down (allow). On real Kodi,
                         System.BuildVersion is a compile-time constant and
                         can never be unknown; a 0 here means a test harness
                         or a diagnostic import, where blocking on a harness
                         artifact would gate restores the backup/restore
                         contract says must land untouched.

Self-contained ON PURPOSE (owner directive: EZM++ features stay separable
pieces a skin could absorb later): no Kodi imports, no wiz imports, no dialog
calls, no I/O. Callers pass in what they know and show what comes back.
"""

# The manifest key backup() stamps (wiz._write_manifest) and this gate reads.
MANIFEST_KEY = "kodi_version"


def major(version):
    """A Kodi MAJOR version as an int, from whatever shape the caller holds:
    wiz.get_Kodi_Version()'s float (21.9 -> 21), a manifest's int, a stray
    string. 0 means UNKNOWN - it is never a valid Kodi major."""
    try:
        n = int(float(version))
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def archive_major(manifest):
    """The Kodi major stamped into a backup manifest dict, or 0 when there is
    no manifest, no stamp (every backup made before 2026.08.30.1), or a value
    that does not parse. 0 = unknown, and evaluate() treats unknown as
    cross-major on purpose - see the module docstring."""
    if not isinstance(manifest, dict):
        return 0
    return major(manifest.get(MANIFEST_KEY))


def is_addons_member(name):
    """True when an archive member sits under the top-level addons/ tree
    (tolerant of leading slashes and backslash separators, the same
    normalization wiz's extract-side predicates use)."""
    rel = (name or "").lstrip("/").replace("\\", "/")
    return rel.split("/", 1)[0] == "addons"


class Decision(object):
    """What the gate decided, and the words to say about it. ``blocked`` is
    the only field control flow may branch on; ``message`` is the ONE
    user-facing dialog text and ``log_line`` the log record. The majors ride
    along so callers and tests can report honestly."""

    def __init__(self, blocked, archive, running, message="", log_line=""):
        self.blocked = bool(blocked)
        self.archive_major = archive
        self.running_major = running
        self.message = message
        self.log_line = log_line


def evaluate(manifest, running_version, namelist):
    """Decide whether the archive's addons/ members may extract onto this box.

    manifest        - the parsed backup manifest dict, or None
    running_version - this box's Kodi version (wiz.get_Kodi_Version() shape)
    namelist        - the archive's member names

    An archive with no addons/ members - every userdata/"kodi_settings"
    backup - never blocks: there is nothing to withhold and no reason to
    speak. That check subsumes the anchor: any member under addons/ makes
    wiz._archive_anchor call the archive home-anchored.
    """
    run = major(running_version)
    arc = archive_major(manifest)
    if not any(is_addons_member(n) for n in namelist or []):
        return Decision(False, arc, run)
    if run <= 0:
        # Harness/diagnostic disarm - impossible on a real box, see docstring.
        return Decision(False, arc, run)
    if arc == run:
        return Decision(False, arc, run)
    if arc:
        message = (
            "This backup was made on Kodi %d, but this device is running "
            "Kodi %d. Add-ons built for one version of Kodi can break "
            "another, so the add-ons stored in this backup will NOT be "
            "restored. Your settings and data will be restored as normal. "
            "When the restore finishes, reinstall your add-ons from their "
            "repositories." % (arc, run)
        )
        log_line = (
            "kodi-version gate: archive Kodi %d vs running Kodi %d - addons/ "
            "withheld, userdata restores in full" % (arc, run)
        )
    else:
        message = (
            "This backup does not say which version of Kodi it was made on, "
            "so its add-ons may have been built for an older Kodi than this "
            "device is running. To be safe, the add-ons stored in this "
            "backup will NOT be restored. Your settings and data will be "
            "restored as normal. When the restore finishes, reinstall your "
            "add-ons from their repositories."
        )
        log_line = (
            "kodi-version gate: archive unstamped (pre-2026.08.30 backup) vs "
            "running Kodi %d - addons/ withheld, userdata restores in full"
            % run
        )
    return Decision(True, arc, run, message=message, log_line=log_line)


def wrap_skip(skip_fn):
    """The gate as an extract-side predicate: the base skip, plus every
    addons/ member. This is how a blocked Decision reaches the extractor
    without the extract loop learning any version arithmetic."""

    def _gated(name):
        return is_addons_member(name) or skip_fn(name)

    return _gated
