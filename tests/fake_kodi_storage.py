"""A tvOS-accurate fake of Kodi's TWO-LAYER userdata storage (NSUserDefaults + POSIX).

WHY THIS EXISTS (and why it is SEPARATE from fake_kodi_sandbox_io.py)
-----------------------------------------------------------------
fake_kodi_sandbox_io.py models the App-Sandbox cross-layer READ quirk for LOCAL and
`special://temp` files (which are NOT under userdata, so CTVOSFile never touches them).
THIS fake models the other tvOS bug family: `userdata/*.xml` being vectored into
NSUserDefaults, where a key SHADOWS the disk file and the two layers can disagree. It is
the shape a plain-dict fake cannot express: "key exists, disk file gone" and "stale key
wins over newer disk content" are both first-class states here.

THE MODEL (ground truth: apple-tv SKILL.md section 8, verified against xbmc branch Omega)
---------------------------------------------------------------------------------------
tvOS (`platform="tvos"`):
  - ELIGIBILITY (CTVOSFile::WantsFile, TVOSFile.cpp:39-45): a path is vectored iff it is
    under `<home>/userdata` at ANY depth, its extension is `.xml` by last dot
    (case-insensitive), and its basename does NOT start with `customcontroller.SiriRemote`.
    There is NO settings.xml carve-out; non-xml is never eligible.
  - WRITES (TVOSFile.cpp:87-99): an eligible xbmcvfs write goes to NSUserDefaults ONLY.
    Disk is never touched. Every write() call REPLACES the whole key (SetKeyData) - a
    chunked loop leaves only the last chunk. Non-eligible paths are plain POSIX writes.
  - READS / EXISTS (TVOSFile.cpp:113-122): the KEY is checked FIRST, disk only as
    fallback. A stale key SHADOWS a newer disk file. Kodi NEVER copies a key back to disk.
  - DELETE (TVOSFile.cpp:101-111 + TVOSNSUserDefaults.mm:188-202): `xbmcvfs.delete` on an
    eligible path drops ONLY the key and returns True regardless (removeObjectForKey is a
    silent no-op when absent; the return is `synchronize() == YES`). The POSIX file is
    left on disk, silently. The POSIX-delete "fallback" is unreachable for eligible paths.
  - Plain `open()` / `os.*` see ONLY the POSIX layer (they are real files in a real tmp
    tree here), never the key store.
  - The key store is mirrored to a REAL binary plist at `plist_path()`
    (`<root>/Library/Preferences/<bundle>.plist`, keys `/userdata/<rel>`, values
    gzip-compressed - exactly Kodi's SetKeyData format), kept in sync on every mutation,
    so nsub's direct plist reader (`_find_nsud_plist` + gunzip) works against this fake
    unmodified.

android (`platform="android"`):
  - xbmcvfs is a thin passthrough to POSIX. No key store, no vectoring, no shadow.
    `plist_path()` points at a path that never exists, so nsub no-ops - the same reason
    the real capture is a pure no-op on Fire TV.

NOT modeled here (deliberately): the VFS-cannot-read-a-foreign-LOCAL-file quirk and the
ControlImage write-through rule - those live in fake_kodi_sandbox_io.py. Under userdata,
CTVOSFile's disk fallback is a normal CPosixFile read, so a disk-only userdata xml reads
fine through xbmcvfs here.

Layout over the tmp root:
  tvOS:    <root>/Library/Caches/Kodi           -> special://home
           <root>/Library/Caches/Kodi/userdata  -> special://userdata / special://profile
           <root>/Library/Preferences/<bundle>.plist -> plist_path() (real file)
  android: <root>/files/.kodi                   -> special://home (persistent app storage)
           plist_path() -> a nonexistent path
"""

import gzip
import os
import plistlib

__all__ = ["FakeKodiStorage", "make_modules"]

_USERDATA_PREFIX = "/userdata/"
_TVOS_BUNDLE_PLIST = "ca.koditvbox.kodi.tvos.21.plist"


class FakeKodiStorage:
    """A real POSIX tree plus (on tvOS) a fake NSUserDefaults key store that shadows it.

    States a userdata-relative path can be in (see `state()`):
      "key-only"  - only the NSUserDefaults key holds it (the post-posix-drop state)
      "disk-only" - only the POSIX file holds it (a plain-extract / Fire TV state)
      "both"      - both layers hold it (the File-Manager-duplicate state; may DISAGREE)
      "absent"    - neither layer holds it
    """

    def __init__(self, root, platform="tvos"):
        if platform not in ("tvos", "android"):
            raise ValueError("platform must be 'tvos' or 'android', got %r" % platform)
        self.root = str(root)
        self.platform = platform
        if platform == "tvos":
            # Apple mandates the whole Kodi home under Library/Caches (purgeable); the
            # NSUserDefaults backing store lives in the SEPARATE Preferences domain.
            self.home = os.path.join(self.root, "Library", "Caches", "Kodi")
            self._plist = os.path.join(
                self.root, "Library", "Preferences", _TVOS_BUNDLE_PLIST
            )
        else:
            # Android: ordinary persistent app storage; no Preferences domain at all.
            self.home = os.path.join(self.root, "files", ".kodi")
            self._plist = os.path.join(self.root, "no-such-preferences", "absent.plist")
        self.userdata = os.path.join(self.home, "userdata")
        os.makedirs(self.userdata, exist_ok=True)
        self.keys = {}  # "/userdata/<rel>" -> bytes (tvOS only; always empty on android)
        self.log = []
        if platform == "tvos":
            os.makedirs(os.path.dirname(self._plist), exist_ok=True)
            self._sync_plist()

    # -- path plumbing -------------------------------------------------------------------

    def translate(self, path):
        p = str(path)
        for pfx, sub in (
            ("special://temp", "temp"),
            ("special://profile", "userdata"),
            ("special://userdata", "userdata"),
            ("special://masterprofile", "userdata"),
            ("special://home", ""),
        ):
            if p == pfx or p.startswith(pfx + "/"):
                rest = p[len(pfx) :].lstrip("/")
                base = os.path.join(self.home, sub) if sub else self.home
                return os.path.join(base, rest.replace("/", os.sep)) if rest else base
        return p

    def _userdata_rel(self, real):
        """userdata-relative forward-slash path, or None if not under userdata/."""
        ud = os.path.normpath(self.userdata)
        real = os.path.normpath(real)
        if not real.startswith(ud + os.sep):
            return None
        return real[len(ud) + 1 :].replace(os.sep, "/")

    def wants(self, path):
        """CTVOSFile::WantsFile (TVOSFile.cpp:39-45): vector iff tvOS AND under
        <home>/userdata at any depth AND `.xml` by last dot (case-insensitive) AND the
        basename does not start with `customcontroller.SiriRemote`."""
        if self.platform != "tvos":
            return False
        rel = self._userdata_rel(self.translate(path))
        if rel is None:
            return False
        base = rel.rsplit("/", 1)[-1]
        if base.lower().startswith("customcontroller.siriremote"):
            return False
        return os.path.splitext(base)[1].lower() == ".xml"

    def _key_for(self, rel):
        return _USERDATA_PREFIX + rel

    def _sync_plist(self):
        """Mirror the key store to the REAL binary plist, gzip-compressed values under
        `/userdata/<rel>` keys - Kodi's SetKeyData format, so nsub's plistlib + gunzip
        reader decodes it unmodified. Called on every key-store mutation."""
        if self.platform != "tvos":
            return
        store = {
            "UserdataMigrated": True
        }  # Kodi's bookkeeping key, not a userdata file
        for key, val in self.keys.items():
            store[key] = gzip.compress(bytes(val))
        with open(self._plist, "wb") as fh:
            plistlib.dump(store, fh)

    # -- the xbmcvfs surface (what a fake xbmcvfs module dispatches to) -------------------

    def vfs_write(self, path, data):
        real = self.translate(path)
        if self.wants(path):
            # tvOS eligible: NSUserDefaults ONLY - whole-key REPLACE, disk never touched
            # (TVOSFile.cpp:87-99: Write() goes straight to SetKeyDataFromPath).
            self.keys[self._key_for(self._userdata_rel(real))] = bytes(data)
            self._sync_plist()
            return True
        d = os.path.dirname(real)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(real, "wb") as f:
            f.write(bytes(data))
        return True

    def vfs_read(self, path):
        real = self.translate(path)
        if self.wants(path):
            key = self._key_for(self._userdata_rel(real))
            if key in self.keys:  # KEY FIRST - a stale key SHADOWS a newer disk file
                return bytearray(self.keys[key])
        if not os.path.isfile(real):
            return bytearray()
        with open(real, "rb") as f:
            return bytearray(f.read())

    def vfs_exists(self, path):
        real = self.translate(path)
        if self.wants(path) and self._key_for(self._userdata_rel(real)) in self.keys:
            return True  # can be True off a key while the disk file is stale or gone
        return os.path.exists(real)

    def vfs_delete(self, path):
        real = self.translate(path)
        if self.wants(path):
            # Bug 4: drops ONLY the key; the POSIX file is left on disk, silently; and
            # the return is True whether or not a key existed (synchronize() == YES).
            self.keys.pop(self._key_for(self._userdata_rel(real)), None)
            self._sync_plist()
            return True
        if os.path.isfile(real):
            os.remove(real)
            return True
        return False

    # -- test helpers ----------------------------------------------------------------------

    def seed_key(self, relpath, data):
        """Plant a KEY-ONLY file (no disk twin): the post-posix-drop state, or a stale
        vector-everything-era key. tvOS only - android has no key store."""
        if self.platform != "tvos":
            raise RuntimeError("seed_key is tvOS-only: android has no NSUserDefaults")
        rel = str(relpath).replace("\\", "/").lstrip("/")
        self.keys[self._key_for(rel)] = bytes(data)
        self._sync_plist()

    def seed_disk(self, relpath, data):
        """Plant a plain POSIX file under userdata/ (what a zipfile extract / a foreign
        writer produces). Never touches the key store."""
        rel = str(relpath).replace("\\", "/").lstrip("/")
        real = os.path.join(self.userdata, rel.replace("/", os.sep))
        d = os.path.dirname(real)
        if d and not os.path.isdir(d):
            os.makedirs(d)
        with open(real, "wb") as f:
            f.write(bytes(data))

    def state(self, relpath):
        """Which layer(s) hold this userdata-relative path right now:
        "key-only" | "disk-only" | "both" | "absent"."""
        rel = str(relpath).replace("\\", "/").lstrip("/")
        has_key = self._key_for(rel) in self.keys
        has_disk = os.path.isfile(os.path.join(self.userdata, rel.replace("/", os.sep)))
        if has_key and has_disk:
            return "both"
        if has_key:
            return "key-only"
        if has_disk:
            return "disk-only"
        return "absent"

    def plist_path(self):
        """The NSUserDefaults backing plist: a REAL, loadable binary plist on tvOS; a
        path that never exists on android (so nsub's capture no-ops, as on Fire TV)."""
        return self._plist


def make_modules(store):
    """Return (xbmc, xbmcvfs) stand-ins bound to `store`, matching the real API surface
    nsud/nsub/wiz use (File, exists, delete, translatePath, log, getCondVisibility)."""

    class _File:
        def __init__(self, path, mode="r"):
            self._path, self._mode = path, mode
            self._buf = bytearray()
            self._pos = 0  # read cursor - readBytes must ADVANCE or a copy loop spins
            self._vectored = "w" in mode and store.wants(path)

        def read(self):
            return bytes(store.vfs_read(self._path))

        def readBytes(self, n=None):
            data = store.vfs_read(self._path)
            if n is None:
                return data
            chunk = data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

        def write(self, data):
            if self._vectored:
                # tvOS SetKeyData: EVERY call replaces the whole key. A chunked loop
                # therefore leaves only the last chunk - the anti-chunking trap.
                return store.vfs_write(self._path, bytes(data))
            self._buf += bytearray(data)
            return True

        def close(self):
            if "w" in self._mode and not self._vectored:
                store.vfs_write(self._path, self._buf)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    class _Vfs:
        File = staticmethod(_File)
        translatePath = staticmethod(store.translate)

        @staticmethod
        def exists(p):
            return store.vfs_exists(p)

        @staticmethod
        def delete(p):
            return store.vfs_delete(p)

    class _Xbmc:
        LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3

        @staticmethod
        def log(msg, level=1):
            store.log.append(msg)

        @staticmethod
        def getCondVisibility(cond):
            # Enough platform truth for nsud._is_tvos() and friends to gate correctly.
            if cond == "System.Platform.TVOS":
                return store.platform == "tvos"
            if cond == "System.Platform.Android":
                return store.platform == "android"
            return False

    return _Xbmc, _Vfs
